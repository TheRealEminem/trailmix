import pytest

import gemini_batch
import gemini_client
import live_gemini
import transcribe_gemini


def test_your_own_key_goes_to_google_signed_with_the_key():
    assert gemini_client.base("AIza-key") == "https://generativelanguage.googleapis.com"
    assert gemini_client.headers("AIza-key") == {"x-goog-api-key": "AIza-key"}
    assert gemini_client.live_url("AIza-key").startswith("wss://generativelanguage.googleapis.com/ws/")


def test_a_cloud_token_goes_to_trailmix_cloud_as_a_bearer_token(monkeypatch):
    monkeypatch.setenv("TRAILMIX_CLOUD_URL", "http://127.0.0.1:8787/")
    assert gemini_client.base("tmc_abc") == "http://127.0.0.1:8787"
    assert gemini_client.headers("tmc_abc") == {"Authorization": "Bearer tmc_abc"}
    assert gemini_client.live_url("tmc_abc") == "ws://127.0.0.1:8787" + gemini_client.LIVE_PATH


def test_a_cloud_token_without_an_address_says_so(monkeypatch):
    monkeypatch.delenv("TRAILMIX_CLOUD_URL", raising=False)
    with pytest.raises(gemini_client.NotConfigured):
        gemini_client.base("tmc_abc")


def test_ready_is_whether_there_is_a_credential():
    assert gemini_client.ready({"gemini_api_key": "AIza-key"})
    assert not gemini_client.ready({"gemini_api_key": ""})
    assert not gemini_client.ready({})


def test_every_module_sends_to_the_same_place(monkeypatch):
    monkeypatch.setenv("TRAILMIX_CLOUD_URL", "https://cloud.example")
    seen = []

    class Reply:
        status_code, text, content = 200, "{}", b"{}"
        headers = {}

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    def request(method, url, headers=None, **kw):
        seen.append((url, headers))
        return Reply()

    monkeypatch.setattr(transcribe_gemini.httpx, "request", request)
    transcribe_gemini.delete_file({"name": "files/1"}, "tmc_abc")
    gemini_batch.status("batches/1", "tmc_abc")
    assert [u for u, _ in seen] == ["https://cloud.example/v1beta/files/1", "https://cloud.example/v1beta/batches/1"]
    assert all(h["Authorization"] == "Bearer tmc_abc" for _, h in seen)
    channel = live_gemini.Channel("tmc_abc", None, lambda *a: None, lambda *a: None)
    assert channel.url.startswith("wss://cloud.example/ws/")
