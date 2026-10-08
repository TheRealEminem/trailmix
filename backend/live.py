"""A live recording session: audio streams in over a WebSocket and is appended straight to disk.

Nothing accumulates in memory except the few seconds awaiting a draft transcription, so a
multi-hour meeting costs no RAM, and a crash loses at most the last fraction of a second.

With "Final transcript while recording" (settings.live_final) each stretch of speech is transcribed by the
accurate model as the meeting goes, the same way the after-the-meeting job would (speech regions, then the
model), and kept per track. When you stop, only the last few seconds are left to do (pipeline.py), so the
transcript and summary follow straight away. If anything was missed (a chunk failed, the model wasn't
downloaded yet), the pipeline falls back to transcribing the whole recording afterwards.
"""
import json
import threading
import time
from difflib import SequenceMatcher

import numpy as np

import audio_store as store
import database as db
import mlx_engine
import models
import transcribe_remote
import vad

SR = store.SAMPLE_RATE
MIN_CHUNK_S = 3.0      # don't transcribe less than this
MAX_CHUNK_S = 12.0     # force a cut if nobody pauses
FINAL_MAX_CHUNK_S = 28.0  # final transcript: wait longer for a pause (the model's window is 30 s), fewer split sentences
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


def find_cut(audio: np.ndarray, max_s: float = MAX_CHUNK_S) -> int | None:
    """Sample index to cut at: the end of the latest pause (>= MIN_CHUNK_S in), else a hard cap."""
    frame = int(FRAME_S * SR)
    n_frames = len(audio) // frame
    for i in range(n_frames - 1, -1, -1):
        end = (i + 1) * frame
        if end < MIN_CHUNK_S * SR:
            break
        if rms(audio[i * frame:end]) < SILENCE_RMS:
            return end
    return int(max_s * SR) if len(audio) >= max_s * SR else None


class LiveSession:
    def __init__(self, meeting_id: int, folder, draft: bool, cfg: dict, client: str = "web"):
        self.meeting_id = meeting_id
        self.client = client        # which app is capturing: "web" or "helper" (the menu bar app)
        self.started_at = time.time()
        self.draft = draft
        self.cfg = cfg  # settings snapshot: which engine drafts the live transcript
        # The accurate model, transcribing for keeps while the meeting runs (see the module docstring).
        self.final = bool(cfg.get("live_final")) and (cfg["transcribe_engine"] == "remote" or mlx_engine.available())
        self.final_segments: dict[int, list[dict]] = {0: [], 1: []}
        self.final_complete = True  # False once any chunk was skipped: then the pipeline redoes it all
        self._step_lock = threading.Lock()  # one draft_step at a time; finish() waits for the running one
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

    @property
    def has_system(self) -> bool:
        return self._written[1] > 0

    @property
    def duration(self) -> float:
        return max(self._written.values()) / SR

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
            if self.draft or self.final:
                self._pending[ch].append(samples)

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

    def _final_text(self, chunk: np.ndarray) -> list[dict]:
        """The accurate transcript of a chunk: speech regions first, like the after-the-meeting job."""
        cfg, out = self.cfg, []
        for a, b in vad.speech_regions(chunk):
            if cfg["transcribe_engine"] == "remote":
                segs = transcribe_remote.transcribe(chunk[a:b], cfg["transcribe_url"], cfg["transcribe_api_key"],
                                                    cfg["transcribe_model"], mlx_engine.LANGUAGE, timeout=60)
            else:
                models.require(mlx_engine.FINAL_MODEL)
                segs = mlx_engine.transcribe_final(chunk[a:b])
            out += [{**seg, "start": seg["start"] + a / SR, "end": seg["end"] + a / SR} for seg in segs]
        return out

    def _draft_text(self, chunk: np.ndarray) -> list[dict] | None:
        if self.cfg["transcribe_engine"] == "remote":
            cfg = self.cfg
            segs = transcribe_remote.transcribe(
                chunk, cfg["transcribe_url"], cfg["transcribe_api_key"],
                cfg["transcribe_live_model"] or cfg["transcribe_model"], mlx_engine.LANGUAGE, timeout=30,
            )
            return mlx_engine.filter_hallucinations(segs)
        models.require(mlx_engine.LIVE_MODEL)  # first run: the model may still be downloading
        return mlx_engine.transcribe_live(chunk)

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        with _registry_lock:
            _active.pop(self.meeting_id, None)

    def finish(self) -> None:
        """After the recording stops: waits for the chunk being transcribed, then saves the final-mode
        transcript so far (the pipeline picks it up and does the rest)."""
        if not self.final:
            return
        with self._step_lock:
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
                cut = find_cut(audio, FINAL_MAX_CHUNK_S if self.final else MAX_CHUNK_S)
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
                    found = self._final_text(chunk)
                    off = start / SR
                    self.final_segments[ch] += [{**s, "start": s["start"] + off, "end": s["end"] + off} for s in found]
                    segs = mlx_engine.filter_hallucinations([{**s} for s in found])
                else:
                    segs = self._draft_text(chunk)
            except transcribe_remote.RemoteError as e:
                self.final_complete = False
                out.append({"type": "status", "message": f"Live draft paused: {e}"})
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
