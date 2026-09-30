"""Post-recording jobs, run one at a time on a single worker thread.

  compress audio -> [approve] -> [RAM gate] -> final transcript (local: large model in its own process;
                                                                    remote: an OpenAI-compatible endpoint)
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
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

import audio_store as store
import database as db
import exporter
import live
import llm_engine
import meeting_text
import mlx_engine
import models
import resources
import settings
import transcribe_remote
import vad

log = logging.getLogger("trailmix.pipeline")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")


NONE: frozenset[str] = frozenset()


def enqueue(meeting_id: int, go: frozenset[str] = NONE, force: frozenset[str] = NONE) -> None:
    """go: stages the user has just approved by hand. force: stages whose low-RAM warning they overrode."""
    _executor.submit(_run, meeting_id, go, force)


def recover_unfinished() -> None:
    """After a crash or restart: pick every interrupted meeting back up where it left off."""
    for m in db.meetings_with_status("recording", "queued", "transcribing", "summarizing"):
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
        db.update_meeting(meeting_id, status="error", wait_reason=None, error=str(e))


def _wait_for_recording_to_end(meeting_id: int) -> None:
    """Never run a heavy model while a meeting is being recorded."""
    if live.active_count():
        db.update_meeting(meeting_id, status="queued", wait_reason="Waiting for the current recording to finish")
        while live.active_count():
            time.sleep(3)
        db.update_meeting(meeting_id, wait_reason=None)


def _gate(meeting_id: int, override: bool, required_gb: float, what: str) -> bool:
    """True if we may proceed. Otherwise parks the meeting as 'waiting_confirm'."""
    if override:
        return True
    reason = resources.check(required_gb, what)
    if reason is None:
        return True
    db.update_meeting(meeting_id, status="waiting_confirm", wait_reason=reason)
    return False


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

    if not m["transcribed"]:
        if m["audio_deleted"] or not m["audio_dir"]:
            raise RuntimeError("Audio has been deleted, so this meeting can't be transcribed")
        store.compress_pending(m)  # cheap: shrinks ~10x on disk before anything heavy happens
        if "transcribe" not in go and not cfg["auto_transcribe"]:
            db.update_meeting(meeting_id, status="ready_transcribe", wait_reason=None)
            return
        local = cfg["transcribe_engine"] == "local"
        if local:
            if not mlx_engine.available():
                raise RuntimeError("Local transcription needs a Mac with MLX; set a remote endpoint in Settings → Transcription")
            _wait_for_recording_to_end(meeting_id)
            if not _gate(meeting_id, "transcribe" in force, mlx_engine.FINAL_MODEL_RAM_GB, "The transcription model"):
                return
        db.update_meeting(meeting_id, status="transcribing", wait_reason=None)
        segments, labeled, duration = _transcribe_local(m) if local else _transcribe_remote(m, cfg)
        _save_transcript(m, segments, labeled, duration)
        m = db.get_meeting(meeting_id)
        if store.RETENTION_DAYS == 0:
            store.delete_audio(m)

    # summarize (also the entry point for "Regenerate")
    if m["transcript"] and not m["summary"]:
        if "summarize" not in go and not cfg["auto_summarize"]:
            db.update_meeting(meeting_id, status="ready_summarize", wait_reason=None)
            _export(meeting_id, cfg)  # save the transcript now; re-exported with the summary later
            return
        choice = m["requested_provider"]
        first = llm_engine.chain(choice, cfg)[0]
        if llm_engine.is_local(first, cfg):
            _wait_for_recording_to_end(meeting_id)
            if not _gate(meeting_id, "summarize" in force, llm_engine.ollama_ram_needed_gb(cfg), "The summarization model"):
                return
        db.update_meeting(meeting_id, status="summarizing", wait_reason=None, summary_error=None)
        try:
            summary, used, title = llm_engine.summarize(
                meeting_text.transcript_text(m, cfg), cfg, choice,
                m["requested_template"] or cfg["summary_template"], meeting_text.moments(m, cfg),
                want_title=bool(cfg["auto_title"] and m["title_auto"]),
            )
            db.update_meeting(meeting_id, summary=summary, summary_provider=used)
            db.replace_tasks(meeting_id, meeting_text.action_items(summary))
            if title and db.get_meeting(meeting_id)["title_auto"]:  # you may have renamed it meanwhile
                db.update_meeting(meeting_id, title=title)
        except llm_engine.LLMError as e:
            db.update_meeting(meeting_id, summary_error=str(e))
    _export(meeting_id, cfg)
    db.update_meeting(meeting_id, status="done", wait_reason=None)


def _transcribe_local(m: dict) -> tuple[list[dict], bool, float]:
    """Runs the large model in a subprocess; when it exits all of its memory is back with macOS."""
    mlx_engine.unload()  # drop the small live model from this process too
    models.ensure(  # first run: the speech model may still be downloading
        mlx_engine.FINAL_MODEL,
        lambda p: db.update_meeting(m["id"], wait_reason="Downloading the speech model (first time only)"
                                    + (f": {p:.0%}" if p is not None else "…")),
    )
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("transcribe_worker.py")), str(store.track_dir(m))],
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
    segments = []
    for name, segs in per_track.items():
        speaker = {"mic": "You", "system": "Them"}[name] if labeled else None
        segments += [{**s, "speaker": speaker, "track": name} for s in segs]
    return _merge(segments)


def _save_transcript(m: dict, segments: list[dict], labeled: bool, duration: float) -> None:
    text = "\n".join(_line(s) for s in segments)
    db.update_meeting(
        m["id"], transcript=text, segments_json=json.dumps(segments), transcribed=1,
        has_system=int(labeled), duration_sec=duration, wait_reason=None,
    )


def _merge(segments: list[dict]) -> list[dict]:
    """Interleave both tracks by time, dropping mic text that is just the speakers leaking into the mic."""
    system = [s for s in segments if s["track"] == "system"]

    def is_echo(s: dict) -> bool:
        return s["track"] == "mic" and any(
            o["start"] < s["end"] and s["start"] < o["end"]
            and SequenceMatcher(None, s["text"].lower(), o["text"].lower()).ratio() > 0.6
            for o in system
        )

    kept = [s for s in segments if not is_echo(s)]
    kept.sort(key=lambda s: s["start"])
    return [{k: v for k, v in s.items() if k != "track"} for s in kept]


def _fmt(t: float) -> str:
    s = int(t)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _line(s: dict) -> str:
    who = f"{s['speaker']}: " if s.get("speaker") else ""
    return f"[{_fmt(s['start'])}] {who}{s['text']}"
