"""The model picker's list for a provider (llm_engine.list_models): only models that can write notes."""
import llm_engine


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
