"""A live recording session: audio streams in over a WebSocket and is appended straight to disk.

Nothing accumulates in memory except the few seconds awaiting a draft transcription, so a
multi-hour meeting costs no RAM, and a crash loses at most the last fraction of a second.
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

SR = store.SAMPLE_RATE
MIN_CHUNK_S = 3.0      # don't transcribe less than this
MAX_CHUNK_S = 12.0     # force a cut if nobody pauses
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


def find_cut(audio: np.ndarray) -> int | None:
    """Sample index to cut at: the end of the latest pause (>= MIN_CHUNK_S in), else a hard cap."""
    frame = int(FRAME_S * SR)
    n_frames = len(audio) // frame
    for i in range(n_frames - 1, -1, -1):
        end = (i + 1) * frame
        if end < MIN_CHUNK_S * SR:
            break
        if rms(audio[i * frame:end]) < SILENCE_RMS:
            return end
    return int(MAX_CHUNK_S * SR) if len(audio) >= MAX_CHUNK_S * SR else None


class LiveSession:
    def __init__(self, meeting_id: int, folder, draft: bool, cfg: dict, client: str = "web"):
        self.meeting_id = meeting_id
        self.client = client        # which app is capturing: "web" or "helper" (the menu bar app)
        self.started_at = time.time()
        self.draft = draft
        self.cfg = cfg  # settings snapshot: which engine drafts the live transcript
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
            if self.draft:
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

    def draft_step(self) -> list[dict]:
        """Transcribe whatever is ready. Runs in a worker thread; returns draft lines for the UI."""
        out = []
        for ch in (0, 1):
            with self._lock:
                if not self._pending[ch]:
                    continue
                buf = np.concatenate(self._pending[ch])
                audio = buf.astype(np.float32) / 32768.0
                cut = find_cut(audio)
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
                segs = self._draft_text(chunk)
            except transcribe_remote.RemoteError as e:
                out.append({"type": "status", "message": f"Live draft paused: {e}"})
                continue
            except models.NotReady as e:
                out.append({"type": "status", "message": str(e)})
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
