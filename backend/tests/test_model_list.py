"""The model picker's list for a provider (llm_engine.list_models): only models that can write notes."""
from fastapi.testclient import TestClient

import database as db
import hardware
import llm_engine
import main
import preflight
import settings


class _Reply:
    status_code = 200

    def __init__(self, models):
        self._models = models

    def json(self):
        return {"models": self._models}


def test_gemini_list_leaves_out_models_that_dont_write_text(monkeypatch):
    listed = [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/gemini-3.1-pro-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemma-4-31b-it", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.1-flash-image", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/nano-banana-pro-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/lyria-3.5", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-transcribe", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-robotics-er-2-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-computer-use-preview-10-2025", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/deep-research-preview-04-2026", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/antigravity-preview-latest", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/veo-3.1", "supportedGenerationMethods": ["predictLongRunning"]},
    ]
    monkeypatch.setattr(llm_engine.httpx, "get", lambda url, **kw: _Reply(listed))
    assert llm_engine.list_models("gemini", {"gemini_api_key": "k", "cloud_token": ""}) == [
        "gemini-2.5-flash", "gemini-3.1-pro-preview", "gemma-4-31b-it",
    ]


def test_ollama_list_leaves_out_embedding_models(monkeypatch):
    monkeypatch.setattr(llm_engine, "_ollama_tags", lambda cfg: [{"name": "qwen2.5:7b"}, {"name": "nomic-embed-text:latest"}])
    assert llm_engine.list_models("ollama", {"ollama_url": "http://localhost:11434"}) == ["qwen2.5:7b"]


# ── An Ollama model bigger than the graphics card ──

GB = 1024**3
_SIX_GB_CARD = [{"name": "AMD graphics", "vendor": "amd", "vram_gb": 6.0, "unified": False}]
_INSTALLED = [{"name": "qwen3.5:4b", "size": 3.3 * GB}, {"name": "gemma4:e2b", "size": 4.6 * GB},
              {"name": "gemma4:12b", "size": 8.0 * GB}]
_HERE = {"ollama_url": "http://localhost:11434", "ollama_model": ""}


def _computer(monkeypatch, gpus, ai_memory_gb=6.0):
    monkeypatch.setattr(hardware, "profile", lambda: {"gpus": gpus, "ai_memory_gb": ai_memory_gb})
    monkeypatch.setattr(llm_engine, "_ollama_tags", lambda cfg: _INSTALLED)


def test_a_model_bigger_than_the_graphics_card_gets_a_warning_and_one_that_fits_doesnt(monkeypatch):
    _computer(monkeypatch, _SIX_GB_CARD)
    assert llm_engine.ollama_card_warning(_HERE, "qwen3.5:4b") is None   # about 5.1 GB with its context window
    why = llm_engine.ollama_card_warning(_HERE, "gemma4:e2b")            # about 6.6 GB
    assert why.startswith("gemma4:e2b needs about 6.6 GB and the graphics card has 6 GB")
    assert "unexpected EOF" in why
    assert llm_engine.ollama_card_warning(_HERE | {"ollama_model": "gemma4:12b"}).startswith("gemma4:12b needs about 10.3 GB")


def test_no_card_warning_on_a_mac_without_a_card_on_another_machine_or_for_an_unknown_model(monkeypatch):
    _computer(monkeypatch, [{"name": "Apple M4", "vendor": "apple", "vram_gb": None, "unified": True}], 11.2)
    assert llm_engine.ollama_card_warning(_HERE, "gemma4:12b") is None
    _computer(monkeypatch, [], 9.6)
    assert llm_engine.ollama_card_warning(_HERE, "gemma4:12b") is None
    _computer(monkeypatch, [{"name": "Intel graphics", "vendor": "intel", "vram_gb": 16.0, "unified": False}], 9.6)
    assert llm_engine.ollama_card_warning(_HERE, "gemma4:12b") is None   # Ollama doesn't run models on it
    _computer(monkeypatch, _SIX_GB_CARD)
    assert llm_engine.ollama_card_warning(_HERE | {"ollama_url": "http://my-server:11434"}, "gemma4:12b") is None
    assert llm_engine.ollama_card_warning(_HERE, "not-installed:7b") is None


def test_two_cards_hold_a_model_together(monkeypatch):
    assert hardware.cards_gb(_SIX_GB_CARD * 2) == 12.0
    assert hardware.cards_gb([*_SIX_GB_CARD, {"name": "small", "vendor": "nvidia", "vram_gb": 2.0, "unified": False}]) == 6.0
    _computer(monkeypatch, _SIX_GB_CARD * 2, 6.0)
    assert llm_engine.ollama_card_warning(_HERE, "gemma4:e2b") is None
    assert llm_engine.ollama_card_warning(_HERE, "gemma4:12b") is None   # 10.3 GB on 12


def test_the_model_list_says_which_models_dont_fit_and_what_automatic_picks(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    _computer(monkeypatch, _SIX_GB_CARD)
    listed = TestClient(main.app).get("/api/providers/ollama/models").json()
    assert listed["models"] == ["qwen3.5:4b", "gemma4:e2b", "gemma4:12b"]
    assert sorted(listed["warnings"]) == ["gemma4:12b", "gemma4:e2b"]
    assert listed["automatic"] in listed["models"]
    plan = preflight.notes_plan([], "ollama", settings.get_all() | {"ollama_model": "gemma4:e2b"})
    assert plan["card_warning"] == listed["warnings"]["gemma4:e2b"]
    assert preflight.notes_plan([], "ollama", settings.get_all() | {"ollama_model": "qwen3.5:4b"})["card_warning"] is None
    assert preflight.notes_plan([], "gemini", settings.get_all())["card_warning"] is None
