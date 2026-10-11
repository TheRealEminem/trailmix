"""Post-recording jobs, run one at a time on a single worker thread.

  compress audio -> [approve] -> [RAM gate] -> final transcript (local: large model in its own process;
                                                                    remote: an OpenAI-compatible endpoint)
                 (or, when it was transcribed while recording: just the last few seconds, no approval or gate)
                 -> [approve] -> [RAM gate] -> summary + title + action items (any provider) -> export

[approve]: automatic when the matching "auto" setting is on; otherwise the meeting parks as
'ready_transcribe' / 'ready_summarize' until you press the button. [RAM gate]: only for models that
run on this machine; parks as 'waiting_confirm' when free RAM is short, regardless of auto mode.
Each stage is resumable: state lives in the database, so a parked job or a server restart
just re-enters at the first unfinished stage.
"""
import json
import logging
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import audio_store as store
import cleanup
import database as db
import exporter
import live
import live_notes
import questions
import llm_engine
import meeting_text
import mlx_engine
import models
import resources
import settings
import telemetry
import transcribe_remote
import vad
import workspaces

log = logging.getLogger("trailmix.pipeline")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")


NONE: frozenset[str] = frozenset()

# The notes being written right now, by meeting, so the window can show them as they come. Only in memory:
# the summary column gets the finished notes, and a restart simply writes them again.
_drafts: dict[int, str] = {}


def draft(meeting_id: int) -> str | None:
    return _drafts.get(meeting_id)


def enqueue(meeting_id: int, go: frozenset[str] = NONE, force: frozenset[str] = NONE) -> None:
    """go: stages the user has just approved by hand. force: stages whose low-RAM warning they overrode."""
    _executor.submit(_run, meeting_id, go, force)


def recover_unfinished() -> None:
    """After a crash or restart: pick every interrupted meeting back up where it left off."""
    for m in db.meetings_with_status("recording", "queued", "transcribing", "summarizing", "waiting_confirm"):
        if m["status"] == "recording":  # server died mid-recording; the .pcm on disk is intact
            folder = store.track_dir(m)
            db.update_meeting(m["id"], duration_sec=store.duration_of_pcm(folder) if folder and folder.exists() else 0,
                              has_system=int(bool(folder and (folder / "system.pcm").exists())))
        db.update_meeting(m["id"], status="queued", wait_reason=None)
        enqueue(m["id"])


def _run(meeting_id: int, go: frozenset[str], force: frozenset[str]) -> None:
    try:
        _run_stages(meeting_id, go, force)
    except Exception as e:  # never let one bad meeting kill the worker
        log.exception("Pipeline failed for meeting %s", meeting_id)
        stage = (db.get_meeting(meeting_id) or {}).get("status")
        db.update_meeting(meeting_id, status="error", wait_reason=None, error=str(e))
        telemetry.send("processing_failed", {"stage": stage, "error_type": type(e).__name__})  # never the message


def _wait_for_recording_to_end(meeting_id: int) -> None:
    """Never run a heavy model while a meeting is being recorded."""
    if live.active_count():
        db.update_meeting(meeting_id, status="queued", wait_reason="Waiting for the current recording to finish")
        while live.active_count():
            time.sleep(3)
        db.update_meeting(meeting_id, wait_reason=None)


# Meetings whose low-memory wait you cut short with "Proceed anyway" (checked by the waiting job).
_proceed: set[int] = set()
RAM_POLL_S = 10


def proceed_now(meeting_id: int) -> bool:
    """'Proceed anyway' for a meeting whose job is waiting for memory. False if no job is waiting."""
    if meeting_id in _waiting:
        _proceed.add(meeting_id)
        return True
    return False


_waiting: set[int] = set()


def _gate(meeting_id: int, override: bool, required_gb: float, what: str) -> None:
    """Waits until there's enough free memory for a heavy model (checking every few seconds), or until you
    say to go ahead anyway. Meanwhile the meeting shows why it's waiting."""
    if override:
        return
    _waiting.add(meeting_id)
    try:
        while meeting_id not in _proceed:
            reason = resources.check(required_gb, what)
            if reason is None:
                break
            db.update_meeting(meeting_id, status="waiting_confirm", wait_reason=reason)
            for _ in range(RAM_POLL_S):
                if meeting_id in _proceed:
                    break
                time.sleep(1)
    finally:
        _waiting.discard(meeting_id)
        _proceed.discard(meeting_id)
    _wait_for_recording_to_end(meeting_id)  # a recording may have started while it waited


def _export(meeting_id: int, cfg: dict) -> None:
    """Auto-export if enabled. A failure is recorded on the meeting, never raised."""
    if not cfg["auto_export"]:
        return
    m = db.get_meeting(meeting_id)
    try:
        paths = exporter.export_meeting(m, cfg)
        db.update_meeting(meeting_id, exported_paths=json.dumps(paths), export_error=None)
    except Exception as e:
        log.exception("Export failed for meeting %s", meeting_id)
        db.update_meeting(meeting_id, export_error=f"Export failed: {e}")


def _run_stages(meeting_id: int, go: frozenset[str], force: frozenset[str]) -> None:
    m = db.get_meeting(meeting_id)
    if not m:
        return
    cfg = settings.get_all()
    stats: dict = {}  # for the anonymous usage stats (telemetry.py): durations and choices, nothing said
    started = time.monotonic()

    if not m["transcribed"]:
        if m["audio_deleted"] or not m["audio_dir"]:
            raise RuntimeError("Audio has been deleted, so this meeting can't be transcribed")
        store.compress_pending(m)  # cheap: shrinks ~10x on disk before anything heavy happens
        had_live = bool(m.get("live_json"))
        done_live = _finish_live(m, cfg)
        if done_live:  # transcribed while recording: the accurate model ran once, during the meeting
            _save_transcript(m, *done_live, made_with=f"{_speech_model(cfg)}, while recording")
            stats.update(transcribed="while recording", transcribe_seconds=round(time.monotonic() - started))
            m = db.get_meeting(meeting_id)
        elif had_live:
            log.warning("Meeting %s: the transcript made while recording missed a part; transcribing it all again",
                        meeting_id)
    if not m["transcribed"]:
        if "transcribe" not in go and not cfg["auto_transcribe"]:
            db.update_meeting(meeting_id, status="ready_transcribe", wait_reason=None)
            return
        local = cfg["transcribe_engine"] == "local"
        if local:
            if not mlx_engine.available():
                raise RuntimeError("Local transcription needs a Mac with MLX; set a remote endpoint in Settings → Transcription")
            _wait_for_recording_to_end(meeting_id)
            _gate(meeting_id, "transcribe" in force, mlx_engine.FINAL_MODEL_RAM_GB, "The transcription model")
        db.update_meeting(meeting_id, status="transcribing", wait_reason=None)
        began = time.monotonic()
        segments, labeled, duration = _transcribe_local(m) if local else _transcribe_remote(m, cfg)
        _save_transcript(m, segments, labeled, duration, made_with=f"{_speech_model(cfg)}, after the meeting")
        stats.update(transcribed="after the meeting", transcribe_seconds=round(time.monotonic() - began))
        m = db.get_meeting(meeting_id)
        if store.RETENTION_DAYS == 0:
            store.delete_audio(m)

    # summarize (also the entry point for "Regenerate")
    if m["transcript"] and not m["summary"]:
        if "summarize" not in go and not cfg["auto_summarize"]:
            db.update_meeting(meeting_id, status="ready_summarize", wait_reason=None)
            _export(meeting_id, cfg)  # save the transcript now; re-exported with the summary later
            return
        live_notes.wait_until_idle(meeting_id)  # notes being written as the meeting ended: the summary uses them
        m = db.get_meeting(meeting_id)
        choice = m["requested_provider"]
        first = llm_engine.chain(choice, cfg)[0]
        if llm_engine.is_local(first, cfg):
            _wait_for_recording_to_end(meeting_id)
            _gate(meeting_id, "summarize" in force, llm_engine.ollama_ram_needed_gb(cfg), "The summarization model")
        db.update_meeting(meeting_id, status="summarizing", wait_reason=None, summary_error=None)
        began = time.monotonic()
        early = live_notes.usable(m)
        try:
            minutes = (m["duration_sec"] or 0) / 60
            summary, used, title = llm_engine.summarize(
                meeting_text.transcript_text(m, cfg), cfg, choice,
                _template(m, cfg), meeting_text.moments(m, cfg),
                want_title=bool(cfg["auto_title"] and m["title_auto"]),
                about=f"This meeting lasted {minutes:.0f} minutes. {meeting_text.speakers_note(m, cfg)} "
                      f"{questions.vocabulary_prompt().replace('Names and terms:', 'Names and terms that may come up, spelled correctly:')}".strip(),
                minutes=minutes,
                on_progress=lambda what: db.update_meeting(meeting_id, wait_reason=what),
                on_draft=lambda text: _drafts.__setitem__(meeting_id, text),
                early_notes=early,
                mine=meeting_text.my_notes_lines(m),
            )
            db.update_meeting(meeting_id, summary=summary, summary_provider=used,
                              summary_model=llm_engine.model_for(used, cfg))
            _drafts.pop(meeting_id, None)
            stats.update(notes="written", notes_seconds=round(time.monotonic() - began), notes_provider=used,
                         notes_model=llm_engine.model_for(used, cfg) if used != "custom" else "custom",
                         notes_from_meeting_notes=bool(early) and len(m["transcript"] or "") > llm_engine.context_chars(used, cfg))
            db.replace_tasks(meeting_id, meeting_text.action_items(summary))
            if title and db.get_meeting(meeting_id)["title_auto"]:  # you may have renamed it meanwhile
                db.update_meeting(meeting_id, title=title)
            try:
                workspaces.after_notes(meeting_id, cfg, used)  # tags, and a workspace if it's in Default
            except Exception:
                log.exception("Couldn't tag or sort meeting %s", meeting_id)
        except llm_engine.LLMError as e:
            db.update_meeting(meeting_id, summary_error=str(e))
            stats.update(notes="failed", notes_provider=llm_engine.chain(choice, cfg)[0])
        finally:
            _drafts.pop(meeting_id, None)
    _export(meeting_id, cfg)
    db.update_meeting(meeting_id, status="done", wait_reason=None)
    if stats:
        m = db.get_meeting(meeting_id) or m
        telemetry.send("meeting_processed", {
            **stats, "minutes": int(round((m["duration_sec"] or 0) / 60 / 5) * 5),
            "source": "imported" if m.get("source") or m.get("imported_at") else "recorded",
        }, cfg)


def _template(m: dict, cfg: dict) -> str:
    """The note style: the one picked for this summary, else its workspace's, else the default in Settings."""
    space = db.get_workspace(m["workspace_id"]) if m.get("workspace_id") else None
    return m["requested_template"] or (space or {}).get("template") or cfg["summary_template"]


def _transcribe_local(m: dict) -> tuple[list[dict], bool, float]:
    """Runs the large model in a subprocess; when it exits all of its memory is back with macOS."""
    mlx_engine.unload()  # drop the small live model from this process too
    models.ensure(  # first run: the speech model may still be downloading
        mlx_engine.FINAL_MODEL,
        lambda p: db.update_meeting(m["id"], wait_reason="Downloading the speech model (first time only)"
                                    + (f": {p:.0%}" if p is not None else "…")),
    )
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("transcribe_worker.py")), str(store.track_dir(m)),
         questions.vocabulary_prompt()[:300]],  # names and terms you confirmed, so they're spelled right
        stdout=subprocess.PIPE, text=True, cwd=Path(__file__).parent,  # stderr: model download progress -> server log
    )
    tracks, duration, per_track, done = [], 0.0, {}, False
    for line in proc.stdout:
        if not line.startswith("TRAILMIX:"):
            continue
        msg = json.loads(line[len("TRAILMIX:"):])
        if "tracks" in msg:
            tracks, duration = msg["tracks"], msg["duration"]
        elif "progress" in msg:
            name, i, n = msg["progress"]
            db.update_meeting(m["id"], wait_reason=f"Transcribing {name} track: {i}/{n}")
        elif "track" in msg:
            per_track[msg["track"]] = msg["segments"]
        elif msg.get("done"):
            done = True
    proc.wait()
    if proc.returncode != 0 or not done:
        raise RuntimeError(
            f"The transcription process exited unexpectedly (code {proc.returncode}); "
            "check the server log. It may have run out of memory or disk space."
        )
    return _label(per_track, tracks), "system" in tracks and "mic" in tracks, duration


LIVE_TAIL_MAX_S = 90  # more left over than this (the live job fell behind or stopped): transcribe it all afterwards


def _finish_live(m: dict, cfg: dict) -> tuple[list[dict], bool, float] | None:
    """Completes a transcript made while recording (live.py): transcribes the few seconds after the last
    chunk, then cleans up and labels it like any other. None if there isn't a usable one."""
    data = json.loads(m["live_json"]) if m.get("live_json") else None
    if not data or not data.get("complete") or data.get("engine") != cfg["transcribe_engine"]:
        return None
    local = cfg["transcribe_engine"] == "local"
    folder = store.track_dir(m)
    try:
        tracks, per_track = [], {}
        duration = 0.0
        for name in store.TRACKS.values():
            audio = store.load_track(folder, name)
            if audio is None:
                continue
            duration = max(duration, len(audio) / store.SAMPLE_RATE)
            if np.max(np.abs(audio), initial=0) <= 0.01:  # silent track, as the worker skips it
                continue
            tracks.append(name)
            covered = int(data["covered"].get(name, 0))
            tail = audio[covered:]
            if len(tail) > LIVE_TAIL_MAX_S * store.SAMPLE_RATE:
                return None
            segs = list(data["tracks"].get(name, []))
            db.update_meeting(m["id"], status="transcribing", wait_reason="Finishing the transcript")
            off = covered / store.SAMPLE_RATE
            for a, b in vad.speech_regions(tail):
                if local:
                    found = mlx_engine.transcribe_final(tail[a:b], questions.vocabulary_prompt()[:300] or None)
                else:
                    found = transcribe_remote.transcribe(tail[a:b], cfg["transcribe_url"], cfg["transcribe_api_key"],
                                                         cfg["transcribe_model"], mlx_engine.LANGUAGE)
                start = off + a / store.SAMPLE_RATE
                segs += [{**seg, "start": seg["start"] + start, "end": seg["end"] + start} for seg in found]
            per_track[name] = segs
        return _label(per_track, tracks), "system" in tracks and "mic" in tracks, duration
    except Exception:
        log.exception("Couldn't finish the live transcript of meeting %s; transcribing it all instead", m["id"])
        return None
    finally:
        if local:
            mlx_engine.unload()  # the accurate model's memory goes back before the summary model loads
        db.update_meeting(m["id"], live_json=None)


def _transcribe_remote(m: dict, cfg: dict) -> tuple[list[dict], bool, float]:
    """Same speech-region approach as the local worker, but each region goes to the remote endpoint."""
    folder = store.track_dir(m)
    tracks = {}
    for name in store.TRACKS.values():
        audio = store.load_track(folder, name)
        if audio is not None and np.max(np.abs(audio), initial=0) > 0.01:
            tracks[name] = audio
    duration = max((len(a) for a in tracks.values()), default=0) / store.SAMPLE_RATE
    per_track = {}
    for name, audio in tracks.items():
        regions = vad.speech_regions(audio)
        segs = []
        for i, (a, b) in enumerate(regions):
            try:
                found = transcribe_remote.transcribe(
                    audio[a:b], cfg["transcribe_url"], cfg["transcribe_api_key"], cfg["transcribe_model"],
                    mlx_engine.LANGUAGE,
                )
            except transcribe_remote.RemoteError as e:
                raise RuntimeError(str(e)) from e
            off = a / store.SAMPLE_RATE
            segs += [{**s, "start": s["start"] + off, "end": s["end"] + off} for s in found]
            db.update_meeting(m["id"], wait_reason=f"Transcribing {name} track: {i + 1}/{len(regions)}")
        per_track[name] = segs
    return _label(per_track, list(tracks)), "system" in tracks and "mic" in tracks, duration


def _label(per_track: dict, tracks: list[str]) -> list[dict]:
    if not tracks:
        raise RuntimeError("No audio was captured (both tracks are silent or missing)")
    labeled = "system" in tracks and "mic" in tracks  # me-vs-them only makes sense with two live tracks
    clean = {name: cleanup.drop_hallucinations(segs) for name, segs in per_track.items()}
    if labeled:  # your mic hears the speakers: drop their words coming back as yours
        clean["mic"] = cleanup.remove_echo(clean.get("mic", []), clean.get("system", []))
    segments = []
    for name, segs in clean.items():
        speaker = {"mic": "You", "system": "Them"}[name] if labeled else None
        segments += [{**s, "speaker": speaker} for s in segs]
    segments.sort(key=lambda s: s["start"])
    return cleanup.strip_metrics(segments)


def _speech_model(cfg: dict) -> str:
    """The speech model's short name, e.g. "whisper-large-v3-turbo"."""
    model = mlx_engine.FINAL_MODEL if cfg["transcribe_engine"] == "local" else cfg["transcribe_model"]
    return model.rsplit("/", 1)[-1].removesuffix("-mlx")


def _save_transcript(m: dict, segments: list[dict], labeled: bool, duration: float, made_with: str | None = None) -> None:
    text = "\n".join(_line(s) for s in segments)
    db.update_meeting(
        m["id"], transcript=text, segments_json=json.dumps(segments), transcribed=1,
        has_system=int(labeled), duration_sec=duration, wait_reason=None, transcribed_with=made_with,
    )


def _fmt(t: float) -> str:
    s = int(t)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _line(s: dict) -> str:
    who = f"{s['speaker']}: " if s.get("speaker") else ""
    return f"[{_fmt(s['start'])}] {who}{s['text']}"
