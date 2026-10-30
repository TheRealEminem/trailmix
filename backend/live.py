"""A live recording session: audio streams in over a WebSocket and is appended straight to disk.

Nothing accumulates in memory except the few seconds awaiting a draft transcription, so a
multi-hour meeting costs no RAM, and a crash loses at most the last fraction of a second.

With "Final transcript while recording" (settings.live_final) each stretch of speech is transcribed by the
accurate model as the meeting goes, the same way the after-the-meeting job would (speech regions, then the
model), and kept per track. When you stop, only the last few seconds are left to do (pipeline.py), so the
transcript and summary follow straight away. If anything was missed (a chunk failed, the model wasn't
downloaded yet), the pipeline falls back to transcribing the whole recording afterwards.

With Gemini (your own Google key, or Trailmix Cloud at the Pro level) it works the same way, with live captions on:
clips cut at pauses go to Google as you talk, and what comes back is both the captions and the transcript you keep.
"""
import json
import threading
import time
from difflib import SequenceMatcher

import numpy as np

import audio_store as store
import cloud
import database as db
import events
import gemini_client
import local_engine
import models
import pricing
import questions
import transcribe_gemini
import transcribe_remote
import vad

SR = store.SAMPLE_RATE
MIN_CHUNK_S = 2.0      # don't transcribe less than this
MAX_CHUNK_S = 5.0      # force a cut if nobody pauses: text appears within about 5 s of being said
# Gemini's near-live captions send longer clips: a model hears whole sentences better (the clips are the transcript you
# keep), and a general model's request carries instructions as well as audio, so fewer, longer ones cost less.
# Captions then trail speech by about 10-15 s.
CLIP_MIN_CHUNK_S = 8.0
CLIP_MAX_CHUNK_S = 15.0
QUIET_SEARCH_S = 1.5   # a forced cut goes at the quietest moment of the last 1.5 s (between words, not in one)
CONTEXT_CHARS = 200    # what was just said on a track, given to the model with the next chunk
FRAME_S = 0.25
SILENCE_RMS = 0.006    # below this a frame counts as a pause / is not worth transcribing
LABELS = {0: "You", 1: "Them"}

_active: dict[int, "LiveSession"] = {}
_registry_lock = threading.Lock()


def active_count() -> int:
    with _registry_lock:
        return len(_active)


def get(meeting_id: int) -> "LiveSession | None":
    with _registry_lock:
        return _active.get(meeting_id)


def sessions() -> list["LiveSession"]:
    with _registry_lock:
        return list(_active.values())


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


def find_cut(audio: np.ndarray, max_s: float = MAX_CHUNK_S, min_s: float = MIN_CHUNK_S) -> int | None:
    """Sample index to cut at: the end of the latest pause (>= `min_s` in); if nobody has paused by
    `max_s`, the quietest moment just before it, so the cut falls between words rather than in one."""
    frame = int(FRAME_S * SR)
    n_frames = len(audio) // frame
    for i in range(n_frames - 1, -1, -1):
        end = (i + 1) * frame
        if end < min_s * SR:
            break
        if rms(audio[i * frame:end]) < SILENCE_RMS:
            return end
    if len(audio) < max_s * SR:
        return None
    small = frame // 5  # 50 ms steps
    lo, hi = int((max_s - QUIET_SEARCH_S) * SR), int(max_s * SR)
    quietest = min(range(lo, hi - small + 1, small), key=lambda a: rms(audio[a:a + small]))
    return quietest + small // 2


def transcribe_clip(cfg: dict, audio: np.ndarray, meeting_id: int, track: str, context: str = "",
                    usage: dict | None = None) -> list[dict]:
    """[{start, end, text}] for a clip of one track ("mic" or "call") from Gemini: Trailmix Cloud's near-live model, or
    with your own key the speech model picked in Settings. Also finishes the last seconds after you stop (pipeline.py).
    `usage` gets the tokens a general model reported."""
    if cfg["transcribe_engine"] == "trailmix":
        cred = gemini_client.credential(cfg, "trailmix")
        return transcribe_gemini.transcribe_clip(audio, cred, local_engine.LANGUAGE, "transcribe_chunk", context,
                                                 tags=gemini_client.meeting_headers(cred, meeting_id, track))
    cred, model = gemini_client.credential(cfg), cfg["gemini_transcribe_model"]
    if transcribe_gemini.uses_transcribe_api(model):  # it takes no instructions, so no context either
        return transcribe_gemini.transcribe_inline(audio, cred, local_engine.LANGUAGE, model)
    return transcribe_gemini.transcribe_clip(audio, cred, local_engine.LANGUAGE, model, context, usage=usage)


# ── Keeping the live model ready ────────────────────────────────────────

_warming = threading.Lock()


def live_model(cfg: dict) -> str | None:
    """The model that transcribes while recording on this Mac (None with a remote engine)."""
    if cfg["transcribe_engine"] != "local" or not local_engine.available():
        return None
    return local_engine.final_model(cfg) if cfg.get("live_final") else local_engine.LIVE_MODEL


def warm_up(cfg: dict, now: bool = False) -> bool:
    """Loads the live model in the background if it isn't in memory, so a recording's first words appear
    within seconds. Not while a recording or the pipeline is using the transcriber, unless `now` (Record
    was just pressed). True if it started loading."""
    model = live_model(cfg)
    if not model or local_engine.loaded() == model or not models.installed(model):
        return False
    if not now and (active_count() or db.meetings_with_status("queued", "transcribing", "summarizing")):
        return False
    if not _warming.acquire(blocking=False):
        return False

    def go():
        try:
            local_engine.warm(model)
        finally:
            _warming.release()

    threading.Thread(target=go, daemon=True, name="warm-up").start()
    return True


class LiveSession:
    def __init__(self, meeting_id: int, folder, draft: bool, cfg: dict, client: str = "web"):
        self.meeting_id = meeting_id
        self.client = client        # which app is capturing: "web" or "helper" (the menu bar app)
        self.started_at = time.time()
        self.draft = draft
        self.cfg = cfg  # settings snapshot: which engine drafts the live transcript
        # With Gemini (your own key, or Trailmix Cloud at the Pro level), clips cut at pauses go to Google as you talk,
        # and what comes back is both the captions and the transcript you keep: one pass, like the final transcript on
        # this Mac. Trailmix Cloud at the Basic level has no live captions; there, and without a key or in Lockdown
        # mode, the transcript is made right after you stop.
        engine = cfg["transcribe_engine"]
        self.near_live = (draft and engine in ("gemini", "trailmix") and gemini_client.ready(cfg, engine)
                          and not cfg.get("lockdown")
                          and (engine == "gemini" or "live_captions" not in cloud.features_off(cfg)))
        self.clip_seconds = 0.0  # audio sent for near-live captions
        self._clip_usage: dict = {}  # tokens a general model reported for them
        # The accurate model, transcribing for keeps while the meeting runs (see the module docstring).
        self.final = self.near_live or bool(cfg.get("live_final")) and (
            cfg["transcribe_engine"] == "remote" or (cfg["transcribe_engine"] == "local" and local_engine.available()))
        self.final_segments: dict[int, list[dict]] = {0: [], 1: []}
        self.final_complete = True  # False once any chunk was skipped: then the pipeline redoes it all
        self._step_lock = threading.Lock()  # one draft_step at a time; finish() waits for the running one
        self._context = {0: "", 1: ""}  # the latest words on each track, for the next chunk's model call
        self._vocab = questions.vocabulary_prompt()[:300]  # names and terms you confirmed: spelled right
        warm_up(cfg, now=True)  # if it isn't loaded yet, start now rather than at the first words
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self._files = {}
        self._written = {0: 0, 1: 0}                            # samples written per channel
        self._pending: dict[int, list[np.ndarray]] = {0: [], 1: []}
        self._pending_start = {0: 0, 1: 0}                      # sample index where pending audio begins
        self._lock = threading.Lock()
        self._recent_them: list[tuple[float, str]] = []         # for suppressing speaker->mic echo
        self.drafts: list[dict] = []                            # kept for the draft-vs-final comparison
        self.bookmarks: list[dict] = []                         # moments you flagged while recording
        # Set by the audio WebSocket, so the other app can mark or stop this recording too.
        self.request_stop = lambda: None
        self.notify = None  # async: pushes a message to the recording app
        with _registry_lock:
            _active[meeting_id] = self
        events.notify()  # the windows and the menu bar app hear of it now

    @property
    def has_system(self) -> bool:
        return self._written[1] > 0

    @property
    def duration(self) -> float:
        return max(self._written.values()) / SR

    @property
    def transcribed_until(self) -> float:
        """How far into the recording the live transcript is complete, in seconds (0 without one)."""
        if not (self.draft or self.final):
            return 0.0
        with self._lock:
            done = [self._pending_start[ch] for ch in (0, 1) if self._written[ch]]
        return min(done) / SR if done else 0.0

    def write(self, frame: bytes) -> None:
        """frame = 1 channel byte + little-endian int16 PCM."""
        if len(frame) < 3:
            return
        ch = frame[0]
        pcm = frame[1:1 + (len(frame) - 1) // 2 * 2]
        if ch not in store.TRACKS:
            return
        f = self._files.get(ch)
        if f is None:
            f = self._files[ch] = open(store.pcm_path(self.folder, store.TRACKS[ch]), "ab")
        f.write(pcm)
        f.flush()  # hand it to the OS now; a crash of this process then loses nothing
        samples = np.frombuffer(pcm, dtype=np.int16)
        with self._lock:
            self._written[ch] += len(samples)
            if self.polls_drafts:
                self._pending[ch].append(samples)

    @property
    def polls_drafts(self) -> bool:
        """Whether draft_step() has chunks to transcribe here (with Gemini only when they're sent as you talk)."""
        return (self.draft and self.cfg["transcribe_engine"] not in ("gemini", "trailmix")) or self.final

    @property
    def captions_off_reason(self) -> str | None:
        """Why live captions aren't running although the engine is Gemini or Trailmix Cloud and captions were asked for."""
        if self.draft and self.cfg["transcribe_engine"] == "trailmix" and not self.near_live:
            if self.cfg.get("lockdown"):
                return "Live captions are off: Lockdown mode keeps your audio on this computer."
            off = cloud.features_off(self.cfg).get("live_captions")
            return (f"Live captions are off: {off[0].lower()}{off[1:]}." if off
                    else "Live captions are off: sign in to Trailmix Cloud in Settings → AI providers.")
        if not self.draft or self.cfg["transcribe_engine"] != "gemini" or self.near_live:
            return None
        if self.cfg.get("lockdown"):
            return "Live captions are off: Lockdown mode keeps your audio on this computer. The transcript is made after you stop."
        return "Live captions are off: add a Google API key in Settings → Transcription. The transcript is made after you stop."

    def describe(self, title: str, recent: int = 40) -> dict:
        """What another app needs to show this recording live."""
        return {
            "id": self.meeting_id, "title": title, "client": self.client, "started_at": self.started_at,
            "duration": round(self.duration, 2), "has_system": self.has_system, "draft": self.draft,
            "drafts": self.drafts[-recent:], "bookmarks": list(self.bookmarks),
        }

    def mark(self, note: str = "") -> dict:
        """Flags the current moment. Stored right away so a crash doesn't lose it."""
        b = {"t": round(self.duration, 2), "note": note[:200]}
        self.bookmarks.append(b)
        db.update_meeting(self.meeting_id, bookmarks_json=self.bookmarks)
        return b

    def _final_text(self, chunk: np.ndarray, prompt: str = "", ch: int = 0) -> list[dict]:
        """The accurate transcript of a chunk: speech regions first, like the after-the-meeting job."""
        cfg, out = self.cfg, []
        if self.near_live:  # the clip was cut at a pause: one request for all of it
            if not vad.speech_regions(chunk):
                return []
            self.clip_seconds += len(chunk) / SR
            return transcribe_clip(cfg, chunk, self.meeting_id, "mic" if ch == 0 else "call", prompt, self._clip_usage)
        for a, b in vad.speech_regions(chunk):
            if cfg["transcribe_engine"] == "remote":
                segs = transcribe_remote.transcribe(chunk[a:b], cfg["transcribe_url"], cfg["transcribe_api_key"],
                                                    cfg["transcribe_model"], local_engine.LANGUAGE, timeout=60)
            else:
                model = local_engine.final_model(cfg)
                models.require(model)
                segs = local_engine.transcribe_final(chunk[a:b], prompt, model)
            out += [{**seg, "start": seg["start"] + a / SR, "end": seg["end"] + a / SR} for seg in segs]
        return out

    def _draft_text(self, chunk: np.ndarray, prompt: str = "") -> list[dict] | None:
        if self.cfg["transcribe_engine"] == "remote":
            cfg = self.cfg
            segs = transcribe_remote.transcribe(
                chunk, cfg["transcribe_url"], cfg["transcribe_api_key"],
                cfg["transcribe_live_model"] or cfg["transcribe_model"], local_engine.LANGUAGE, timeout=30,
            )
            return local_engine.filter_hallucinations(segs)
        models.require(local_engine.LIVE_MODEL)  # first run: the model may still be downloading
        return local_engine.transcribe_live(chunk, prompt)

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        with _registry_lock:
            _active.pop(self.meeting_id, None)
        events.notify()

    def finish(self) -> None:
        """After the recording stops: waits for the chunk being transcribed, then saves the final-mode
        transcript so far (the pipeline picks it up and does the rest)."""
        with self._step_lock:
            if self.clip_seconds and self.cfg["transcribe_engine"] == "trailmix":  # part of the plan: nothing per use
                db.log_usage(self.meeting_id, "live captions", "trailmix", "transcribe_chunk", self.clip_seconds, 0.0)
            elif self.clip_seconds:  # the transcript itself, unless a clip was missed and it's all done again afterwards
                model = self.cfg["gemini_transcribe_model"]
                db.log_usage(self.meeting_id, "transcript" if self.final_complete else "live captions", "gemini", model,
                             self.clip_seconds, pricing.transcript_cost(model, self.clip_seconds, self._clip_usage))
            self.clip_seconds = 0.0
            if not self.final:
                return
            covered = {store.TRACKS[ch]: self._pending_start[ch] for ch in (0, 1)}
            db.update_meeting(self.meeting_id, live_json=json.dumps({
                "tracks": {store.TRACKS[ch]: segs for ch, segs in self.final_segments.items()},
                "covered": covered, "complete": self.final_complete,
                "engine": self.cfg["transcribe_engine"],
            }))

    def draft_step(self) -> list[dict]:
        """Transcribe whatever is ready. Runs in a worker thread; returns draft lines for the UI."""
        with self._step_lock:
            return self._draft_step()

    def _draft_step(self) -> list[dict]:
        out = []
        for ch in (0, 1):
            with self._lock:
                if not self._pending[ch]:
                    continue
                buf = np.concatenate(self._pending[ch])
                audio = buf.astype(np.float32) / 32768.0
                cut = (find_cut(audio, CLIP_MAX_CHUNK_S, CLIP_MIN_CHUNK_S) if self.near_live
                       else find_cut(audio))
                if cut is None:
                    self._pending[ch] = [buf]
                    continue
                start = self._pending_start[ch]
                self._pending_start[ch] += cut
                self._pending[ch] = [buf[cut:]]
            chunk = audio[:cut]
            if rms(chunk) < SILENCE_RMS:
                continue
            try:
                if self.final:
                    found = self._final_text(chunk, f"{self._vocab} {self._context[ch]}".strip(), ch)
                    off = start / SR
                    self.final_segments[ch] += [{**s, "start": s["start"] + off, "end": s["end"] + off} for s in found]
                    segs = local_engine.filter_hallucinations([{**s} for s in found])
                else:
                    segs = self._draft_text(chunk, f"{self._vocab} {self._context[ch]}".strip())
            except transcribe_remote.RemoteError as e:
                self.final_complete = False
                out.append({"type": "status", "message": f"Live draft paused: {e}"})
                continue
            except transcribe_gemini.BasicLevel as e:  # the month's live hours ran out during this meeting
                self.final = self.near_live = self.final_complete = False
                with self._lock:
                    self._pending = {0: [], 1: []}
                out.append({"type": "status", "message": f"{str(e).rstrip('.')}. Live captions stop here; the transcript is made "
                                                          "right after you stop."})
                break
            except transcribe_gemini.GeminiError as e:
                self.final_complete = False  # the whole recording is transcribed afterwards instead
                out.append({"type": "status", "message": f"Live captions paused: {e}"})
                continue
            except models.NotReady as e:
                self.final_complete = False
                out.append({"type": "status", "message": str(e)})
                continue
            except Exception:
                if not self.final:
                    raise
                self.final_complete = False  # the whole recording is transcribed afterwards instead
                continue
            if segs is None:
                out.append({"type": "status", "message": "Live draft paused while another job uses the transcriber"})
                continue
            text = " ".join(s["text"] for s in segs).strip()
            if not text:
                continue
            self._context[ch] = (self._context[ch] + " " + text)[-CONTEXT_CHARS:].lstrip()
            t = start / SR
            if ch == 1:
                self._recent_them = [(t, text)] + [x for x in self._recent_them if t - x[0] < 30]
            elif self._is_echo(t, text):
                continue
            line = {"type": "draft", "start": t, "speaker": LABELS[ch] if self.has_system else None, "text": text}
            out.append(line)
            self.drafts.append({k: v for k, v in line.items() if k != "type"})
        if any(x["type"] == "draft" for x in out):
            db.update_meeting(self.meeting_id, draft_json=json.dumps(self.drafts))
        return out

    def _is_echo(self, t: float, text: str) -> bool:
        """The mic can hear the speakers; drop mic text that duplicates recent system text."""
        return any(abs(t - ts) < 30 and SequenceMatcher(None, text.lower(), other.lower()).ratio() > 0.6
                   for ts, other in self._recent_them)
