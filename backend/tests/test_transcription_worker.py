"""The transcription worker (./trailmix worker): an OpenAI-compatible endpoint a server install sends audio to.
The last test runs it on real speech with whisper.cpp's tiny model: run with TRAILMIX_ENGINE_TEST=1 where pywhispercpp
is installed (requirements-whispercpp.txt). It downloads the model (78 MB) the first time."""
import importlib.util
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import local_engine
import transcription_worker as worker

SPEECH = Path(__file__).resolve().parents[2] / "scripts" / "fixtures" / "them.wav"
TINY = "ggerganov/whisper.cpp:ggml-tiny.bin"


@pytest.fixture
def client():
    with TestClient(worker.app) as c:
        yield c
    if worker._pool is not None:  # the child process that held the model
        worker._pool.shutdown(wait=True)
        worker._pool = None


def test_the_old_name_is_the_same_worker():
    import mac_worker

    assert mac_worker.app is worker.app and mac_worker.main is worker.main


def test_a_token_keeps_others_out(client, monkeypatch):
    monkeypatch.setattr(worker, "TOKEN", "s3cret")
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    listed = client.get("/v1/models", headers={"Authorization": "Bearer s3cret"}).json()
    assert {"large", "live", "whisper-1"} <= {m["id"] for m in listed["data"]}


def test_it_says_which_engine_it_uses(client):
    assert client.get("/health").json() == {"ok": local_engine.available(), "engine": local_engine.NAME}


def test_no_engine_is_a_clear_error(client, monkeypatch):
    monkeypatch.setattr(local_engine, "available", lambda: False)
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"RIFF")}, data={"model": "large"})
    assert r.status_code == 503 and "No speech engine" in r.json()["detail"]


@pytest.mark.skipif(os.getenv("TRAILMIX_ENGINE_TEST") != "1", reason="set TRAILMIX_ENGINE_TEST=1")
@pytest.mark.skipif(importlib.util.find_spec("pywhispercpp") is None, reason="needs pywhispercpp")
def test_it_transcribes_speech_with_whisper_cpp(client):
    assert local_engine.NAME == "whisper.cpp", "set TRAILMIX_LOCAL_ENGINE=whispercpp on an Apple silicon Mac"
    assert worker.ffmpeg(), "needs ffmpeg, or pip install imageio-ffmpeg"
    with SPEECH.open("rb") as f:
        r = client.post("/v1/audio/transcriptions", files={"file": ("them.wav", f, "audio/wav")},
                        data={"model": TINY, "response_format": "verbose_json", "language": "en"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert all(w in body["text"].lower() for w in ("login", "wednesday", "pricing")), body["text"]
    assert 5 < body["duration"] < 6 and body["segments"] and all(s["end"] > s["start"] for s in body["segments"])
