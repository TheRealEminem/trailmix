from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import database as db
import importer
import llm_engine
import main
import pipeline
import preflight
import settings


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    queued = []
    monkeypatch.setattr(pipeline, "enqueue", lambda mid, go=frozenset(), force=frozenset(): queued.append((mid, go)))
    monkeypatch.setattr(llm_engine, "_ollama_tags", lambda cfg: [{"name": "qwen2.5:7b", "size": 4.7 * 1024**3}])
    return queued


def pasted(minutes: int) -> int:
    """An imported transcript about this many minutes long (about 1,000 characters a minute)."""
    segments = [{"start": i * 60, "end": i * 60 + 59, "speaker": "Them", "text": "word " * 196} for i in range(minutes)]
    return importer.save_imported("Planning", datetime.now(timezone.utc), segments, "paste", None, None)


def test_an_import_waits_for_a_choice_when_asked_to(fresh_db):
    mid = pasted(5)
    assert db.get_meeting(mid)["ask_first"] == 1
    pipeline._run_stages(mid, frozenset(), frozenset())
    assert db.get_meeting(mid)["status"] == "ready_summarize"  # nothing ran


def test_an_import_runs_with_the_defaults_when_not_asked_to(fresh_db):
    settings.update({"ask_before_ai": False})
    assert db.get_meeting(pasted(5))["ask_first"] == 0


def test_a_long_transcript_is_read_in_parts_by_the_local_model_but_not_the_cloud():
    cfg = settings.DEFAULTS | {"ollama_model": "qwen2.5:7b"}
    assert preflight.notes_passes(20_000, "ollama", cfg) == 1
    assert preflight.notes_passes(63_298, "ollama", cfg) == 2   # a 66-minute meeting: two parts, then the write-up
    assert preflight.notes_passes(63_298, "gemini", cfg) == 1


def test_preflight_shows_the_passes_and_the_cost(fresh_db):
    mid = pasted(66)
    client = TestClient(main.app)
    local = client.post("/api/preflight", json={"meeting_ids": [mid], "provider": "ollama"}).json()
    plan = local["notes"]["plan"]
    assert plan["local"] and plan["meetings"][0]["parts"] == 2 and plan["calls"] == 3 + preflight.EXTRA_CALLS
    assert plan["cost"] == 0 and plan["ram_needed_gb"] > 4
    assert local["transcribe"] is None  # it has a transcript already
    cloud = client.post("/api/preflight", json={"meeting_ids": [mid], "provider": "gemini",
                                                "model": "gemini-3.6-flash"}).json()["notes"]["plan"]
    assert cloud["meetings"][0]["parts"] == 1 and cloud["cost"] > 0 and cloud["model"] == "gemini-3.6-flash"


def test_process_uses_the_picked_ai_and_can_remember_it(fresh_db):
    mid = pasted(5)
    settings.update({"gemini_api_key": "test-key-not-real"})
    client = TestClient(main.app)
    r = client.post("/api/process", json={"meeting_ids": [mid], "provider": "gemini", "model": "gemini-3.6-flash",
                                          "remember": True})
    assert r.status_code == 202, r.text
    m = db.get_meeting(mid)
    assert (m["requested_provider"], m["requested_model"], m["ask_first"], m["status"]) == \
        ("gemini", "gemini-3.6-flash", 0, "queued")
    assert fresh_db[-1] == (mid, frozenset({"transcribe", "summarize"}))
    cfg = settings.get_all()
    assert (cfg["summary_provider"], cfg["gemini_model"]) == ("gemini", "gemini-3.6-flash")


def test_process_refuses_an_ai_that_cant_be_used(fresh_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # (a key in the environment counts as one in Settings)
    mid = pasted(5)
    r = TestClient(main.app).post("/api/process", json={"meeting_ids": [mid], "provider": "anthropic"})
    assert r.status_code == 409 and "API key" in r.json()["detail"]
    assert db.get_meeting(mid)["ask_first"] == 1  # still waiting for a choice


def test_the_picked_model_writes_the_notes(fresh_db, monkeypatch):
    mid = pasted(5)
    db.update_meeting(mid, ask_first=0, requested_provider="ollama", requested_model="llama3.1:8b")
    used = {}

    def fake_summarize(transcript, cfg, choice, *a, **kw):
        used.update(choice=choice, model=llm_engine.model_for(choice, cfg))
        return "## Action Items\n- None", choice, None

    monkeypatch.setattr(pipeline, "_gate", lambda *a, **kw: None)
    monkeypatch.setattr(llm_engine, "summarize", fake_summarize)
    monkeypatch.setattr(pipeline.workspaces, "after_notes", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline.telemetry, "send", lambda *a, **kw: None)
    pipeline._run_stages(mid, frozenset({"summarize"}), frozenset())
    assert used == {"choice": "ollama", "model": "llama3.1:8b"}
    assert db.get_meeting(mid)["summary_model"] == "llama3.1:8b"
