"""A live recording session: audio streams in over a WebSocket and is appended straight to disk.

Nothing accumulates in memory except the few seconds awaiting a draft transcription, so a
multi-hour meeting costs no RAM, and a crash loses at most the last fraction of a second.

With "Final transcript while recording" (settings.live_final) each stretch of speech is transcribed by the
accurate model as the meeting goes, the same way the after-the-meeting job would (speech regions, then the
model), and kept per track. When you stop, only what's still missing is left to do (pipeline.py): the last few
seconds, and any chunk that failed. The transcript and summary then follow straight away. The whole recording is
transcribed again only if most of it is missing (the model wasn't downloaded yet, say).

With Gemini (your own Google key, or Trailmix Cloud at the Pro level) it works the same way, with live captions on:
clips cut at pauses go to Google as you talk, and what comes back is both the captions and the transcript you keep.
"""
import json
import logging
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

log = logging.getLogger("trailmix.live")

SR = store.SAMPLE_RATE
MIN_CHUNK_S = 2.0      # don't transcribe less than this
MAX_CHUNK_S = 5.0      # force a cut if nobody pauses: text appears within about 5 s of being said
# Gemini's near-live captions send longer clips: a model hears whole sentences better (the clips are the transcript you
# keep), and a general model's request carries instructions as well as audio, so fewer, longer ones cost less.
# Captions then trail speech by about 10-15 s.
CLIP_MIN_CHUNK_S = 8.0
CLIP_MAX_CHUNK_S = 15.0
QUIET_SEARCH_S = 1.5   # a forced cut goes at the quietest moment of the last 1.5 s (between words, not in one)
CATCH_UP_S = 60.0      # the most one chunk takes on when a track has fallen behind
CONTEXT_CHARS = 200    # what was just said on a track, given to the model with the next chunk
FRAME_S = 0.25
SILENCE_RMS = 0.006    # below this a frame counts as a pause / is not worth transcribing
LABELS = {0: "You", 1: "Them"}
# When a recording stops by itself (the auto_stop setting, end_of_meeting below).
CALL_SEEN_S = 60       # a call app has had audio open this long: a call, not a notification sound
CALL_GONE_S = 30       # and has been gone this long: the call ended, it wasn't a hiccup
QUIET_STOP_S = 600     # with no call app to go by: nobody has said anything for this long
UNLOAD_AFTER_S = 60    # no recording for this long and nothing transcribing: the speech model's memory goes back
BEHIND_NOTICE_S = 15   # captions this far behind say so

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
    `max_s`, the quietest moment just before it, so the cut falls between words rather than in one.
    A track far behind takes up to CATCH_UP_S at once: a mic with a steady background hum never shows a pause, and
    at `max_s` a round it could never catch up after one slow moment."""
    audio = audio[:int(CATCH_UP_S * SR)]  # however far behind, a chunk stays this size (the latest pause would be anywhere)
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
    end_s = min(len(audio) / SR, max(max_s, CATCH_UP_S)) if len(audio) >= 2 * max_s * SR else max_s
    lo, hi = int((end_s - QUIET_SEARCH_S) * SR), int(end_s * SR)
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


# ── The speech model: in memory only while a meeting needs it ───────────
# Nothing keeps it loaded while idle. A recording starts at once and loads it in the background; audio goes to disk and
# waits in the session's queue meanwhile, and the transcript catches up once the model is ready.

_warming = threading.Lock()
_load_error: str | None = None  # why the latest load didn't happen; such a meeting is transcribed after it ends
_last_recording = 0.0           # time.monotonic() of the latest recording's end


def live_model(cfg: dict) -> str | None:
    """The model that transcribes while recording on this Mac (None with a remote engine)."""
    if cfg["transcribe_engine"] != "local" or not local_engine.available():
        return None
    return local_engine.final_model(cfg) if cfg.get("live_final") else local_engine.LIVE_MODEL


def warm_up(cfg: dict) -> bool:
    """A recording just started: loads its model in the background. True if it started loading. No memory gate: captions
    during a call are what this is for, macOS swaps if it must, and a load that fails leaves the job to the pipeline."""
    global _load_error
    if not _warming.locked():
        _load_error = None  # an earlier recording's failed load doesn't condemn this one (the model may be loaded by now)
    model = live_model(cfg)
    if not model or not models.installed(model):  # (a model already loaded is for warm() to see, under the engine lock)
        return False
    if not _warming.acquire(blocking=False):
        return False

    def go():
        global _load_error
        try:
            local_engine.warm(model)
        except Exception as e:
            log.exception("Loading the speech model failed")
            _load_error = str(e) or type(e).__name__
            try:
                local_engine.unload()  # weights that did load before the failure aren't kept for the whole meeting
            except Exception:
                log.exception("Unloading after a failed load failed")
        finally:
            _warming.release()

    threading.Thread(target=go, daemon=True, name="warm-up").start()
    return True


def unload_if_idle(now: float | None = None) -> bool:
    """Frees the speech model once nothing needs it. Normally the pipeline already has; this catches every other way
    out of a meeting (one that waits for your go-ahead, a discarded recording, a load that finished after the stop).
    The wait lets a stopped meeting's last seconds use the model still in memory. True if it unloaded."""
    now = time.monotonic() if now is None else now

    def idle() -> bool:
        return not (active_count() or _warming.locked() or now - _last_recording < UNLOAD_AFTER_S
                    or db.meetings_with_status("transcribing"))

    if not local_engine.loaded() or not idle():
        return False
    return bool(local_engine.unload(idle))  # checked again under the engine lock: a recording may have just started


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
        self.final = self.near_live or (bool(cfg.get("live_final")) and (
            cfg["transcribe_engine"] == "remote" or (cfg["transcribe_engine"] == "local" and local_engine.available())))
        self.final_segments: dict[int, list[dict]] = {0: [], 1: []}
        self.final_complete = True  # False once it stopped for good partway (the month's live hours ran out)
        self.missed: dict[int, list[list[int]]] = {0: [], 1: []}  # samples of chunks that failed: the pipeline redoes these
        self._step_lock = threading.Lock()  # one draft_step at a time; finish() waits for the running one
        self._context = {0: "", 1: ""}  # the latest words on each track, for the next chunk's model call
        self._vocab = questions.vocabulary_prompt()[:300]  # names and terms you confirmed: spelled right
        self._no_model = False  # the model couldn't load: the whole transcript is made after the meeting
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
        self.stop_reason: str | None = None                     # set when it stopped by itself: what to tell you
        self._last_speech: float | None = None                  # where in the recording words were last heard
        self._call: dict = {}                                   # the call app seen during this recording
        # Set by the audio WebSocket, so the other app can mark or stop this recording too.
        self.request_stop = lambda: None
        self.notify = None  # async: pushes a message to the recording app
        self._local = cfg["transcribe_engine"] == "local" and self.polls_drafts
        with _registry_lock:
            _active[meeting_id] = self  # before the warm-up: from here the idle unloader leaves the model alone
        if self._local:
            warm_up(cfg)  # the recording doesn't wait for it: _draft_step holds its audio back until it's ready
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
            f = self._files[ch] = open(store.pcm_path(self.folder, store.TRACKS[ch]), "ab")  # noqa: SIM115  (open for the whole recording)
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
        return not self._no_model and (
            (self.draft and self.cfg["transcribe_engine"] not in ("gemini", "trailmix")) or self.final)

    @property
    def model_status(self) -> str | None:
        """Why captions are late, if they are, for the recording window."""
        if not self.draft or not self._local:
            return None
        if self._no_model or (_load_error and not _warming.locked()):
            return "The transcription model couldn't load. Your audio is safe: the transcript is made after you stop."
        if _warming.locked():
            return "Loading transcription model… Recording has started; captions follow when it's ready."
        with self._lock:
            behind = max((self._written[ch] - self._pending_start[ch] for ch in (0, 1)), default=0) / SR
        return "Catching up on the transcript…" if behind >= BEHIND_NOTICE_S else None

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
            "drafts": self.drafts[-recent:], "bookmarks": list(self.bookmarks), "model_status": self.model_status,
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
        global _last_recording
        for f in self._files.values():
            f.close()
        with _registry_lock:
            _active.pop(self.meeting_id, None)
            _last_recording = time.monotonic()
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
                "missed": {store.TRACKS[ch]: spans for ch, spans in self.missed.items()},
                "engine": self.cfg["transcribe_engine"],
            }))

    def draft_step(self) -> list[dict]:
        """Transcribe whatever is ready. Runs in a worker thread; returns draft lines for the UI."""
        with self._step_lock:
            return self._draft_step()

    def _draft_step(self) -> list[dict]:
        out = []
        if self._local:
            if _warming.locked():  # the backlog waits for the model, in order
                return out
            if _load_error and not self._no_model:
                log.warning("Meeting %s: no speech model (%s); transcribing after the meeting", self.meeting_id, _load_error)
                self._no_model, self.final, self.final_complete = True, False, False
                with self._lock:
                    self._pending = {0: [], 1: []}
                return out
        for ch in (0, 1):
            with self._lock:
                if not self._pending[ch]:
                    continue
                buf = np.concatenate(self._pending[ch])
                audio = buf[:int(CATCH_UP_S * SR)].astype(np.float32) / 32768.0  # a long backlog doesn't hold up write()
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
                self._missed(ch, start, cut)
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
                self._missed(ch, start, cut)
                out.append({"type": "status", "message": f"Live captions paused: {e}"})
                continue
            except models.NotReady as e:
                self._missed(ch, start, cut)
                out.append({"type": "status", "message": str(e)})
                continue
            except Exception:
                if not self.final:
                    raise
                log.exception("Meeting %s: a chunk of the live transcript failed; it's redone after the meeting", self.meeting_id)
                self._missed(ch, start, cut)
                continue
            if segs is None:
                self._last_speech = start / SR
                out.append({"type": "status", "message": "Live draft paused while another job uses the transcriber"})
                continue
            text = " ".join(s["text"] for s in segs).strip()
            if not text:
                continue
            self._last_speech = start / SR
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

    def _missed(self, ch: int, start: int, length: int) -> None:
        """This chunk failed: the pipeline transcribes it after the meeting."""
        self.missed[ch].append([start, start + length])
        self._last_speech = start / SR  # it may well have been speech: no sign that the meeting is over

    def end_of_meeting(self, recorder_status: dict, now: float | None = None) -> str | None:
        """Why this recording should stop by itself now, if it should; called every few seconds. The call app you were
        in has gone quiet, or (with no call app in the picture, such as a call in a browser or a meeting in a room)
        nobody has said anything for a long while. Before anyone has spoken it waits: you may be early."""
        now = time.monotonic() if now is None else now
        calling = recorder_status.get("call_app")
        call = self._call
        if calling:
            call.update(name=calling, seen=now)
            call.setdefault("since", now)
            return None
        if call and recorder_status.get("available") and now - call["seen"] >= CALL_GONE_S:
            if call["seen"] - call["since"] >= CALL_SEEN_S:
                return f"The {call['name']} call ended"
            call.clear()  # a notification sound, not a call
        if call or self._last_speech is None:
            return None
        return ("Nobody has spoken for 10 minutes"
                if self.transcribed_until - self._last_speech >= QUIET_STOP_S else None)

    def _is_echo(self, t: float, text: str) -> bool:
        """The mic can hear the speakers; drop mic text that duplicates recent system text."""
        return any(abs(t - ts) < 30 and SequenceMatcher(None, text.lower(), other.lower()).ratio() > 0.6
                   for ts, other in self._recent_them)
