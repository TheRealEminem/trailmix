from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import database as db
import llm_engine
import main
import settings


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()


def cfg(**kw):
    return {**settings.DEFAULTS, "gemini_api_key": "k", "gemini_model": "gemini-flash-latest",
            "openai_api_key": "k", "openai_model": "gpt-x", "anthropic_api_key": "k", "anthropic_model": "claude-x", **kw}


def gemini_says(monkeypatch, tokens_in, tokens_out):
    reply = {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}],
             "usageMetadata": {"promptTokenCount": tokens_in, "candidatesTokenCount": tokens_out}}
    monkeypatch.setattr(llm_engine, "_post", lambda url, **kw: SimpleNamespace(json=lambda: reply))


def test_a_cloud_call_is_counted_under_what_it_was_for(fresh_db, monkeypatch):
    gemini_says(monkeypatch, 1000, 200)
    with llm_engine.purpose("notes", 7):
        llm_engine.generate("gemini", cfg(), "q")
    llm_engine.generate("gemini", cfg(), "q")  # no purpose given
    rows = {r["kind"]: r for r in db.usage_since("2000-01-01")}
    assert rows["notes"]["tokens_in"] == 1000 and rows["notes"]["tokens_out"] == 200 and rows["notes"]["cost"] > 0
    assert rows["other"]["n"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT meeting_id FROM usage WHERE kind = 'notes'").fetchone()[0] == 7


def test_a_caller_that_keeps_its_own_tally_is_not_counted_twice(fresh_db, monkeypatch):
    gemini_says(monkeypatch, 1000, 200)
    usage = {}
    llm_engine.generate("gemini", cfg(), "q", usage=usage)
    assert usage == {"in": 1000, "out": 200} and db.usage_since("2000-01-01") == []


def test_other_providers_tokens_are_counted_but_not_priced(fresh_db, monkeypatch):
    reply = {"choices": [{"message": {"content": "Hi"}}], "usage": {"prompt_tokens": 500, "completion_tokens": 50}}
    monkeypatch.setattr(llm_engine, "_post", lambda url, **kw: SimpleNamespace(json=lambda: reply))
    with llm_engine.purpose("questions"):
        llm_engine.generate("openai", cfg(), "q")
    [row] = db.usage_since("2000-01-01")
    assert (row["kind"], row["tokens_in"], row["tokens_out"], row["cost"], row["unpriced"]) == ("questions", 500, 50, None, 1)


def test_a_streamed_reply_asks_for_its_token_count(fresh_db, monkeypatch):
    sent = {}
    events = [{"choices": [{"delta": {"content": "Hi"}}]}, {"choices": [], "usage": {"prompt_tokens": 90, "completion_tokens": 9}}]

    def sse(url, **kw):
        sent.update(kw["json"])
        return iter(events)

    monkeypatch.setattr(llm_engine, "_sse", sse)
    assert llm_engine.generate("openai", cfg(), "q", on_text=lambda t: None) == "Hi"
    assert sent["stream_options"] == {"include_usage": True}
    assert db.usage_since("2000-01-01")[0]["tokens_in"] == 90


def test_a_model_of_yours_costs_nothing_so_nothing_is_counted(fresh_db, monkeypatch):
    reply = {"choices": [{"message": {"content": "Hi"}}], "usage": {"prompt_tokens": 500, "completion_tokens": 50}}
    monkeypatch.setattr(llm_engine, "_post", lambda url, **kw: SimpleNamespace(json=lambda: reply))
    local = cfg(custom_base_url="http://localhost:1234/v1", custom_model="m")
    llm_engine.generate("custom", local, "q")
    assert db.usage_since("2000-01-01") == []


def test_the_usage_endpoint_adds_it_up(fresh_db):
    db.log_usage(1, "transcript", "gemini", "gemini-3.5-transcribe", 600, 0.05)
    db.log_usage(2, "notes", "openai", "gpt-x", 0, None, 500, 50)
    out = TestClient(main.app).get("/api/usage?days=30").json()
    assert out["cost"] == pytest.approx(0.05) and out["unpriced"] == 1
    assert {r["kind"] for r in out["rows"]} == {"transcript", "notes"}


def test_an_older_usage_table_gains_the_token_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    with db.connect() as conn:
        conn.executescript("DROP TABLE usage; CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                           "at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), meeting_id INTEGER, "
                           "kind TEXT NOT NULL, engine TEXT NOT NULL, model TEXT, audio_seconds REAL NOT NULL, est_cost REAL);"
                           "INSERT INTO usage (kind, engine, audio_seconds, est_cost) VALUES ('transcript', 'gemini', 60, 0.01);")
    db.init_db()
    db.log_usage(None, "notes", "openai", "gpt-x", 0, None, 5, 1)
    assert {r["kind"] for r in db.usage_since("2000-01-01")} == {"transcript", "notes"}
