import numpy as np
import pytest

import llm_engine
import models
import settings
import telemetry
import transcribe_remote

CLOUD = {"lockdown": True, "anthropic_api_key": "sk-x", "ollama_url": "http://localhost:11434",
         "custom_base_url": "http://127.0.0.1:1234/v1", "custom_model": "local"}


def test_cloud_ai_is_blocked_and_ai_on_this_computer_isnt(monkeypatch):
    monkeypatch.setattr(llm_engine, "_ollama_tags", lambda cfg: [{"name": "qwen2.5:7b"}])
    ok, why = llm_engine.configured("anthropic", CLOUD)
    assert not ok and "Lockdown mode is on" in why
    assert llm_engine.configured("ollama", CLOUD)[0]
    assert llm_engine.configured("custom", CLOUD)[0]  # an OpenAI-compatible server on this computer
    assert not llm_engine.configured("ollama", {**CLOUD, "ollama_url": "http://studio.local:11434"})[0]
    assert llm_engine.configured("anthropic", {**CLOUD, "lockdown": False})[0]


def test_recordings_dont_go_to_a_server_elsewhere(monkeypatch):
    monkeypatch.setattr(settings, "locked", lambda cfg=None: True)
    with pytest.raises(transcribe_remote.RemoteError, match="Lockdown"):
        transcribe_remote.transcribe(np.zeros(1600, dtype=np.float32), "https://api.example.com/v1", "", "m", "en")


def test_no_usage_stats(monkeypatch):
    monkeypatch.setattr(telemetry, "KEY", "phc_test")
    cfg = {"share_stats": True, "stats_notice_seen": True, "lockdown": True}
    assert not telemetry.enabled(cfg) and telemetry.enabled({**cfg, "lockdown": False})


def test_no_model_downloads(monkeypatch):
    monkeypatch.setattr(models, "_locked", lambda: True)
    monkeypatch.setattr(models.local_engine, "available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda repo: False)
    started = []
    monkeypatch.setattr(models.threading, "Thread", lambda **kw: started.append(kw))
    models.start("mlx-community/whisper-large-v3-turbo")
    assert not started
    with pytest.raises(models.NotReady, match="Lockdown"):
        models.require("mlx-community/whisper-large-v3-turbo")
    with pytest.raises(RuntimeError, match="Lockdown"):
        models.ensure("mlx-community/whisper-large-v3-turbo")


# ── Google Gemini: transcription, live captions, batch jobs ──

def test_nothing_goes_to_google_for_transcripts(monkeypatch):
    import transcribe_gemini

    monkeypatch.setattr(settings, "locked", lambda cfg=None: True)
    with pytest.raises(transcribe_gemini.GeminiError, match="Lockdown"):
        transcribe_gemini.transcribe(np.zeros(16000, dtype=np.float32), "key")


def test_no_live_captions_from_google(tmp_path, monkeypatch):
    import database as db
    import live

    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()

    cfg = {**settings.DEFAULTS, "transcribe_engine": "gemini", "gemini_api_key": "k", "lockdown": True, "live_final": False}
    session = live.LiveSession(1, tmp_path / "a", draft=True, cfg=cfg)
    try:
        assert not session.gemini_live            # so write() never opens a connection
        session.write(b"\x00" + b"\x01\x00" * 1600)
        assert session._channels == {}
    finally:
        session.close()


def test_batch_jobs_are_not_sent_or_checked(monkeypatch):
    import batch_jobs
    import gemini_batch

    cfg = {**settings.DEFAULTS, "gemini_api_key": "k", "lockdown": True}
    calls = []
    monkeypatch.setattr(gemini_batch, "status", lambda *a: calls.append(a))
    monkeypatch.setattr(batch_jobs.db, "batch_jobs", lambda open_only=False, limit=50: [{"id": 1}])
    with pytest.raises(batch_jobs.NotReady, match="Lockdown"):
        batch_jobs.submit_notes([1], cfg)
    with pytest.raises(batch_jobs.NotReady, match="Lockdown"):
        batch_jobs.submit_transcripts([1], cfg, "gemini-3.5-flash-lite")
    assert batch_jobs.check(cfg) == 0 and calls == []


def test_the_api_refuses_google_engines_in_lockdown(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import database as db
    import main

    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(main.settings, "get_all", lambda: {**settings.DEFAULTS, "gemini_api_key": "k", "lockdown": True})
    client = TestClient(main.app)
    r = client.post("/api/meetings", json={"title": "", "engine": "gemini"})
    assert r.status_code == 409 and "Lockdown" in r.json()["detail"]
    assert client.post("/api/upgrades/notes/batch", json={}).status_code == 409
    assert client.get("/api/health").json()["transcription"]["gemini_ready"] is False
    assert client.get("/api/upgrades").json()["notes_batch"]["ready"] is False
