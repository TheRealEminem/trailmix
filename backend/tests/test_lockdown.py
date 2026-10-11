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
    monkeypatch.setattr(models.mlx_engine, "available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda repo: False)
    started = []
    monkeypatch.setattr(models.threading, "Thread", lambda **kw: started.append(kw))
    models.start("mlx-community/whisper-large-v3-turbo")
    assert not started
    with pytest.raises(models.NotReady, match="Lockdown"):
        models.require("mlx-community/whisper-large-v3-turbo")
    with pytest.raises(RuntimeError, match="Lockdown"):
        models.ensure("mlx-community/whisper-large-v3-turbo")
