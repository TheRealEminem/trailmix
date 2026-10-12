import asyncio
import json

import numpy as np
import websockets
from websockets.asyncio.server import serve

import live_gemini as lg

SR = lg.SR


def tone(seconds: float, loud: bool = True) -> bytes:
    n = int(seconds * SR)
    x = (np.sin(np.arange(n) * 0.2) * (0.3 if loud else 0.0) * 32767).astype("<i2")
    return x.tobytes()


class FakeGoogle:
    """Answers like the live model: when speech starts at stream second t it says ACTIVITY_START, an interim
    "words@t" and, when the audio goes quiet or ends, the final text and ACTIVITY_END."""

    def __init__(self):
        self.streams = []   # per connection: setup message and seconds of audio received

    async def handler(self, ws):
        info = {"setup": None, "frames": 0, "ended": False}
        self.streams.append(info)
        speaking, quiet, started_at = False, 0, 0.0
        async for raw in ws:
            msg = json.loads(raw)
            if "setup" in msg:
                info["setup"] = msg["setup"]
                await ws.send(json.dumps({"setupComplete": {}}))
                continue
            rt = msg["realtimeInput"]
            if rt.get("audioStreamEnd"):
                info["ended"] = True
                if speaking:
                    await self.finish(ws, started_at)
                await ws.close()
                return
            import base64
            pcm = np.frombuffer(base64.b64decode(rt["audio"]["data"]), dtype="<i2").astype(np.float32) / 32768
            t = info["frames"] * lg.FRAME_S
            info["frames"] += 1
            if np.sqrt(np.mean(pcm ** 2)) > 0.05:
                quiet = 0
                if not speaking:
                    speaking, started_at = True, t
                    await ws.send(json.dumps({"serverContent": {}, "voiceActivity": {
                        "type": "ACTIVITY_START", "audioOffset": f"{t:.3f}s"}}))
                    await ws.send(json.dumps({"serverContent": {"interimInputTranscription": {"text": f"words@{t:.1f}"}}}))
            elif speaking:
                quiet += 1
                if quiet >= 10:  # a second of quiet
                    speaking = False
                    await self.finish(ws, started_at)

    async def finish(self, ws, t):
        await ws.send(json.dumps({"serverContent": {"inputTranscription": {"text": f"said@{t:.1f}"}}}))
        await ws.send(json.dumps({"serverContent": {}, "voiceActivity": {"type": "ACTIVITY_END", "audioOffset": f"{t + 1:.3f}s"}}))


async def run_channel(audio_parts, **kw):
    fake = FakeGoogle()
    interims, finals, statuses = [], [], []
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ch = lg.Channel("test-key", "en", lambda t, x: interims.append((t, x)), lambda t, x: finals.append((t, x)),
                        statuses.append, url=f"ws://127.0.0.1:{port}", **kw)
        ch.start()
        for part in audio_parts:
            for i in range(0, len(part), 8192):
                ch.feed(part[i:i + 8192])
                await asyncio.sleep(0)
            await asyncio.sleep(0.05)
        await ch.stop()
    return fake, interims, finals, statuses


def test_words_come_back_at_the_right_time_though_silence_is_not_sent():
    parts = [tone(3, loud=False), tone(1), tone(6, loud=False), tone(1), tone(4, loud=False)]
    fake, interims, finals, statuses = asyncio.run(run_channel(parts))
    assert [round(t, 1) for t, _ in finals] == [3.0, 10.0]          # when each was said in the recording
    assert [x for _, x in finals] == ["said@0.4", "said@3.8"]       # on Google's clock: the quiet stretch was skipped
    assert interims and interims[0][0] == 3.0
    assert fake.streams[0]["setup"]["model"] == lg.MODEL
    assert fake.streams[0]["setup"]["inputAudioTranscription"] == {"languageCodes": ["en"]}
    sent = sum(s["frames"] for s in fake.streams) * lg.FRAME_S
    assert sent < 9   # of 15 s recorded: loud parts, their lead-in, and two seconds after each
    assert not statuses


def test_a_long_stream_is_replaced_at_a_pause(monkeypatch):
    parts = [tone(2), tone(3, loud=False), tone(2), tone(3, loud=False)]
    fake, _, finals, _ = asyncio.run(run_channel(parts, session_s=2.5))
    assert len(fake.streams) == 2
    assert fake.streams[0]["ended"]
    assert [round(t, 1) for t, _ in finals] == [0.0, 5.0]


def test_a_quiet_stretch_closes_the_stream_and_speech_reopens_it(monkeypatch):
    monkeypatch.setattr(lg, "IDLE_CLOSE_S", 4.0)
    parts = [tone(1), tone(8, loud=False), tone(1), tone(3, loud=False)]
    fake, _, finals, _ = asyncio.run(run_channel(parts))
    assert len(fake.streams) == 2
    assert [round(t, 1) for t, _ in finals] == [0.0, 9.0]


def test_unreachable_google_is_reported_not_raised():
    async def go():
        statuses = []
        ch = lg.Channel("k", None, lambda *a: None, lambda *a: None, statuses.append, url="ws://127.0.0.1:1")
        ch.start()
        ch.feed(tone(1))
        await asyncio.sleep(0.5)
        await ch.stop()
        return statuses

    statuses = asyncio.run(go())
    assert statuses and statuses[0].startswith("Live captions paused")
