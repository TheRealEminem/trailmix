"""Live captions while recording, through Google's gemini-3.5-transcribe-live (a WebSocket per audio channel).

Audio goes up as it is recorded; text comes back about a second behind speech:
  - interim text: the words of the current stretch of speech so far, which can still change;
  - final text: sent when the speaker pauses.
The live model is a little less accurate than the batch one (and can drop a word where it cuts speech into
stretches), so this is for watching the meeting and for notes during it. The transcript you keep is made after
the recording by transcribe_gemini.py, which also tells the speakers apart.

Two things keep it cheap and within Google's limits:
  - silence isn't sent. A map from "seconds into this stream" to "seconds into the recording" keeps the times right;
  - a stream lasts at most 10 minutes, so a new one is opened at a pause just before that.
"""
import asyncio
import base64
import collections
import json
import logging
import time

import numpy as np
import websockets

log = logging.getLogger("trailmix.live_gemini")

SR = 16000
MODEL = "models/gemini-3.5-transcribe-live"
URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
FRAME_S = 0.1            # Google suggests about 100 ms per message
SILENCE_RMS = 0.006      # below this a frame is quiet
HANG_S = 2.0             # keep sending this long after the last sound, so Google sees the speaker has paused
PREROLL_S = 0.4          # ...and this much from just before the sound, so the first word isn't clipped
IDLE_CLOSE_S = 20.0      # nothing said for this long: close the stream (it's reopened at the next words)
SESSION_S = 9 * 60       # Google ends a stream after 10 minutes; open a new one at a pause before that
SESSION_FORCE_S = 9.5 * 60
FINISH_WAIT_S = 6.0      # after the recording stops, wait this long for the last words to come back
RETRY_S = 5.0            # between attempts to reconnect


def _level(frame: bytes) -> float:
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


class _Stream:
    """One WebSocket session. `map` turns seconds into this stream into seconds into the recording."""

    def __init__(self, ws, meeting_pos: int):
        self.ws = ws
        self.sent = 0                       # samples sent on this stream
        self.map = [(0, meeting_pos)]       # (stream sample, recording sample) wherever audio was skipped
        self.gap = False                    # audio was skipped since the last frame sent
        self.dead = False
        self.receiver: asyncio.Task | None = None

    def recording_seconds(self, stream_seconds: float) -> float:
        at = stream_seconds * SR
        s_pos, m_pos = next(entry for entry in reversed(self.map) if entry[0] <= at or entry is self.map[0])
        return (m_pos + at - s_pos) / SR


class Channel:
    """Live captions for one audio channel (the microphone, or the meeting's audio)."""

    def __init__(self, api_key: str, language: str | None, on_interim, on_final, on_status=None,
                 url: str = URL, session_s: float = SESSION_S, force_s: float = SESSION_FORCE_S):
        self.api_key, self.language = api_key, language
        self.on_interim, self.on_final, self.on_status = on_interim, on_final, on_status or (lambda msg: None)
        self.url, self.session_s, self.force_s = url, session_s, force_s
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closing: list[_Stream] = []   # streams that were told the audio ended and are still sending words back
        self._paused_reported = False
        self.sent_samples = 0               # audio sent to Google so far, all streams (what's billed)

    # -- public -------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def feed(self, pcm: bytes) -> None:
        """int16 PCM, as it is recorded (call from the event loop)."""
        self._queue.put_nowait(pcm)

    async def stop(self) -> None:
        """No more audio: wait briefly for the last words, then hang up."""
        self._queue.put_nowait(None)
        if self._task:
            try:
                await asyncio.wait_for(self._task, FINISH_WAIT_S + 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    # -- internals ----------------------------------------------------------

    async def _connect(self, meeting_pos: int) -> _Stream:
        ws = await websockets.connect(self.url, additional_headers={"x-goog-api-key": self.api_key}, max_size=None,
                                      open_timeout=15)  # the key goes in a header: addresses end up in logs and errors
        config = {"languageCodes": [self.language] if self.language else []}
        await ws.send(json.dumps({"setup": {"model": MODEL, "generationConfig": {"responseModalities": ["TEXT"]},
                                            "inputAudioTranscription": config}}))
        stream = _Stream(ws, meeting_pos)
        stream.receiver = asyncio.create_task(self._receive(stream))
        return stream

    async def _send_audio(self, stream: _Stream, frame: bytes) -> None:
        await stream.ws.send(json.dumps({"realtimeInput": {"audio": {
            "data": base64.b64encode(frame).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
        stream.sent += len(frame) // 2
        self.sent_samples += len(frame) // 2

    async def _end(self, stream: _Stream) -> None:
        """Tells Google the audio is over (it then sends the last words); the stream closes when it's done."""
        try:
            await stream.ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
        except Exception:
            stream.dead = True
        self._closing.append(stream)

    async def _receive(self, stream: _Stream) -> None:
        start = 0.0
        try:
            async for raw in stream.ws:
                msg = json.loads(raw)
                activity = msg.get("voiceActivity") or {}
                if activity.get("type") == "ACTIVITY_START":
                    start = stream.recording_seconds(float(str(activity.get("audioOffset", "0")).rstrip("s")))
                content = msg.get("serverContent") or {}
                interim = (content.get("interimInputTranscription") or {}).get("text")
                final = (content.get("inputTranscription") or {}).get("text")
                if interim and interim.strip():
                    self.on_interim(start, interim.strip())
                if final and final.strip():
                    self.on_final(start, final.strip())
                if self._paused_reported and (interim or final):
                    self._paused_reported = False
                    self.on_status("")
        except websockets.ConnectionClosed as e:
            if e.rcvd and e.rcvd.code not in (1000, 1001):
                self.on_status(f"Live captions stopped: {e.rcvd.reason or e.rcvd.code}")
        except Exception as e:
            log.exception("Live captions stream failed")
            self.on_status(f"Live captions stopped: {e}")
        finally:
            stream.dead = True

    async def _run(self) -> None:
        frame_bytes = int(FRAME_S * SR) * 2
        buffer = bytearray()
        preroll: collections.deque[bytes] = collections.deque(maxlen=int(PREROLL_S / FRAME_S))
        stream: _Stream | None = None
        position = 0                    # samples taken in so far (the recording's clock)
        last_voice = -10 ** 12          # recording sample of the last loud frame
        last_sent = 0
        retry_at = 0.0
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            buffer += chunk[:len(chunk) // 2 * 2]
            while len(buffer) >= frame_bytes:
                frame = bytes(buffer[:frame_bytes])
                del buffer[:frame_bytes]
                frame_start = position
                position += frame_bytes // 2
                if _level(frame) >= SILENCE_RMS:
                    last_voice = position
                since_voice = (position - last_voice) / SR
                if since_voice > HANG_S:  # quiet: don't send it, keep it as the lead-in to whatever comes next
                    preroll.append(frame)
                    if stream:
                        idle = (position - last_sent) / SR > IDLE_CLOSE_S
                        if idle or stream.sent / SR >= self.session_s:  # nothing to say lately, or time for a new stream
                            await self._end(stream)
                            stream = None
                        else:
                            stream.gap = True
                    continue
                if stream and (stream.dead or stream.sent / SR >= self.force_s):
                    if not stream.dead:
                        await self._end(stream)
                    stream = None
                if stream is None:
                    if time.monotonic() < retry_at:
                        continue
                    try:
                        first = position - (len(preroll) + 1) * (frame_bytes // 2)
                        stream = await self._connect(first)
                    except Exception as e:
                        retry_at = time.monotonic() + RETRY_S
                        self._paused_reported = True
                        self.on_status(f"Live captions paused: couldn't reach Google ({e})")
                        preroll.clear()
                        continue
                if stream.gap and stream.sent:
                    stream.map.append((stream.sent, frame_start - len(preroll) * (frame_bytes // 2)))
                stream.gap = False
                try:
                    while preroll:
                        await self._send_audio(stream, preroll.popleft())
                    await self._send_audio(stream, frame)
                    last_sent = position
                except Exception:
                    stream.dead = True
        # recording over: let Google finish the words in flight
        if stream and not stream.dead:
            await self._end(stream)
        streams = [s for s in self._closing if s.receiver]
        if streams:
            await asyncio.wait([s.receiver for s in streams], timeout=FINISH_WAIT_S)
        for s in streams:
            s.receiver.cancel()
            try:
                await s.ws.close()
            except Exception:
                pass
