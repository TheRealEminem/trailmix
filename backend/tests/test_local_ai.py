import httpx

import llm_engine
import local_ai

REAL_CLIENT = httpx.Client


def _serve(monkeypatch, routes: dict):
    """Fakes this computer's ports: {(port, path): JSON answer, text, or an int status}. Anything else refuses."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = routes.get((request.url.port, request.url.path))
        if answer is None:
            raise httpx.ConnectError("Connection refused", request=request)
        if isinstance(answer, int):
            return httpx.Response(answer, text="<html>Not here</html>")
        if isinstance(answer, str):
            return httpx.Response(200, text=answer)
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(local_ai.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_finds_each_server_with_its_models_and_context(monkeypatch):
    _serve(monkeypatch, {
        (11434, "/api/tags"): {"models": [{"name": "qwen2.5:7b"}, {"name": "nomic-embed-text:latest"}]},
        (1234, "/v1/models"): {"data": [{"id": "qwen3-14b", "owned_by": "organization_owner"},
                                        {"id": "text-embedding-nomic-embed-text-v1.5"}]},
        (1234, "/api/v0/models"): {"data": [{"id": "qwen3-14b", "type": "llm", "max_context_length": 40960,
                                             "loaded_context_length": 16384}]},
        (8000, "/v1/models"): {"data": [{"id": "Qwen/Qwen3-32B", "owned_by": "vllm", "max_model_len": 32768}]},
        (8080, "/v1/models"): {"data": [{"id": "gemma-3-12b-it-Q4_K_M.gguf", "owned_by": "llamacpp"}]},
        (8080, "/props"): {"default_generation_settings": {"n_ctx": 8192}},
        (1337, "/v1/models"): "<html>a web app, not an AI server</html>",
    })
    found = {s["name"]: s for s in local_ai.scan(fresh=True)}
    assert set(found) == {"Ollama", "LM Studio", "vLLM", "llama.cpp"}
    assert found["Ollama"] == {"name": "Ollama", "kind": "ollama", "port": 11434, "url": "http://localhost:11434",
                               "models": [{"id": "qwen2.5:7b", "context": None}]}
    assert found["LM Studio"]["url"] == "http://localhost:1234/v1"
    assert found["LM Studio"]["models"] == [{"id": "qwen3-14b", "context": 16384}]  # what it's loaded with
    assert found["vLLM"]["models"] == [{"id": "Qwen/Qwen3-32B", "context": 32768}]
    assert found["llama.cpp"]["models"][0]["context"] == 8192


def test_a_port_answering_something_else_is_not_an_ai_server(monkeypatch):
    _serve(monkeypatch, {(8080, "/v1/models"): 404, (8000, "/v1/models"): {"data": []},
                         (1234, "/v1/models"): {"error": "nope"}})
    assert local_ai.scan(fresh=True) == []
    # LocalAI and friends say nothing about themselves on a shared port.
    _serve(monkeypatch, {(8080, "/v1/models"): {"object": "list", "data": [{"id": "mistral", "object": "model"}]}})
    assert [s["name"] for s in local_ai.scan(fresh=True)] == [local_ai.UNKNOWN]


def test_a_scan_is_remembered_for_a_few_seconds(monkeypatch):
    _serve(monkeypatch, {(1337, "/v1/models"): {"data": [{"id": "llama3.2-3b-instruct"}]}})
    assert [s["name"] for s in local_ai.scan(fresh=True)] == ["Jan"]
    _serve(monkeypatch, {})
    assert [s["name"] for s in local_ai.scan()] == ["Jan"]
    assert local_ai.scan(fresh=True) == []


def test_the_server_in_use_is_marked_and_installed_apps_with_their_server_off_get_a_hint(monkeypatch, tmp_path):
    _serve(monkeypatch, {(1234, "/v1/models"): {"data": [{"id": "qwen3-8b"}]},
                         (11434, "/api/tags"): {"models": [{"name": "qwen2.5:7b"}]}})
    cfg = {"summary_provider": "custom", "summary_fallback": "none", "custom_base_url": "http://127.0.0.1:1234/v1",
           "ollama_url": "http://localhost:11434"}
    got = local_ai.status(cfg, fresh=True)
    assert {s["name"]: s["in_use"] for s in got["servers"]} == {"LM Studio": True, "Ollama": False}
    assert got["installed"] == []

    jan = tmp_path / "Jan.app"
    jan.mkdir()
    monkeypatch.setattr(local_ai, "_app_paths", lambda: {"LM Studio": [tmp_path / "LM Studio.app"], "Jan": [jan]})
    monkeypatch.setattr(local_ai.Path, "home", lambda: tmp_path)
    assert [a["name"] for a in local_ai.installed_not_running([])] == ["Jan"]
    (tmp_path / ".lmstudio").mkdir()  # LM Studio's own folder, on any OS
    assert [a["name"] for a in local_ai.installed_not_running([])] == ["LM Studio", "Jan"]
    assert local_ai.installed_not_running([{"name": "LM Studio"}, {"name": "Jan"}]) == []


def test_requests_to_a_local_server_are_sized_to_its_context():
    base = {"summary_provider": "custom", "summary_fallback": "none"}
    unknown = llm_engine.context_chars("custom", {**base, "custom_context_tokens": 0})
    big = llm_engine.context_chars("custom", {**base, "custom_context_tokens": 32768})
    tiny = llm_engine.context_chars("custom", {**base, "custom_context_tokens": 4096})
    assert big > unknown > tiny > 0
    assert tiny == int((llm_engine.MIN_CONTEXT_TOKENS - llm_engine.RESERVED_TOKENS) * llm_engine.CHARS_PER_TOKEN)
