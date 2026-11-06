from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import ask_scope
import database as db
import llm_engine
import main
import settings

NOW = datetime.now(UTC)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    settings.update({"your_name": "Dale"})
    return tmp_path


@pytest.fixture
def ai(monkeypatch):
    """A pretend AI that answers "ok" and keeps the prompts it was sent."""
    prompts = []
    monkeypatch.setattr(llm_engine, "chain", lambda choice, cfg: ["ollama"])

    def generate(pid, cfg, prompt, system=None, **kw):
        prompts.append(prompt)
        return "ok"

    monkeypatch.setattr(llm_engine, "generate", generate)
    return prompts


async def _never():
    pass


@pytest.fixture
def client(fresh_db, ai, monkeypatch):
    monkeypatch.setattr(main, "_unload_loop", lambda: _never())
    return TestClient(main.app)


def meeting(title: str, days_ago: float, summary: str, source: str | None = None, tasks: list[str] = ()) -> int:
    when = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if source:
        mid = db.create_imported_meeting(title, when, source, None)
    else:
        mid = db.create_meeting(title, title_auto=False)
        with db.connect() as conn:
            conn.execute("UPDATE meetings SET created_at = ? WHERE id = ?", (when, mid))
    db.update_meeting(mid, summary=summary, transcript=summary, status="done")
    if tasks:
        db.replace_tasks(mid, list(tasks))
    return mid


@pytest.mark.parametrize("question,days", [
    ("What did I promise to do recently?", 14),
    ("What came up this week?", 7),
    ("What did we decide last week?", 14),
    ("Anything from the last 3 days?", 3),
    ("What happened in the past 2 months?", 62),
    ("What did we agree yesterday?", 2),
    ("When did we last talk about pricing?", None),  # "last" alone isn't a window
])
def test_time_words_in_the_question_set_the_window(question, days):
    assert ask_scope.days_in(question) == days


def test_promises_and_open_items_read_the_tasks():
    assert ask_scope.about_tasks("What did I promise to do recently?")
    assert ask_scope.about_tasks("What's still unresolved across my meetings?")
    assert not ask_scope.about_tasks("When did we last talk about pricing?")


def test_everything_in_scope_is_read_when_it_fits(fresh_db):
    old = meeting("Kickoff", 200, "We promised a roadmap.")
    new = meeting("Sync", 3, "Budget talk.")
    assert ask_scope.pick("roadmap", room=5) == [new, old]  # newest first, matching or not
    assert ask_scope.pick("roadmap", room=5, days=14) == [new]  # the window leaves the old one out


def test_too_many_reads_the_best_matches_newest_first(fresh_db):
    for i in range(6):
        meeting(f"Standup {i}", 1 + i, "Nothing much.")
    old = meeting("Pricing review", 300, "Pricing tiers and pricing pages.")
    newer = meeting("Pricing check-in", 40, "A note on pricing.")
    assert ask_scope.pick("When did we last talk about pricing?", room=3) == [newer, old]


def test_a_window_fills_up_with_its_newest_meetings(fresh_db):
    ids = [meeting(f"Standup {i}", 1 + i, "Nothing much.") for i in range(5)]
    meeting("Old promise", 100, "I promise to send the deck.")
    assert ask_scope.pick("What did I promise?", room=3, days=14) == ids[:3]


def test_sources_narrow_the_meetings(fresh_db):
    here = meeting("Recorded", 2, "Notes.")
    gran = meeting("From Granola", 2, "Notes.", source="granola")
    assert ask_scope.pick("notes", room=5, sources=["granola"]) == [gran]
    assert ask_scope.pick("notes", room=5, sources=["trailmix"]) == [here]
    assert ask_scope.pick("notes", room=5, sources=[]) == []
    assert db.meeting_sources() == {"trailmix": 1, "granola": 1}


def test_promises_are_answered_from_recent_tasks_with_todays_date(client, ai):
    meeting("Last spring", 200, "Old notes.", tasks=["Dale: send the 2025 budget"])
    recent = meeting("Client call", 4, "Notes.", tasks=["Dale: send the Cooper quote", "Dana: book the venue"])
    r = client.post("/api/ask", json={"question": "What did I promise to do recently?"}).json()
    prompt = ai[-1]
    assert "Dale: send the Cooper quote" in prompt and "2025 budget" not in prompt
    assert "The user is Dale" in prompt and f"{NOW.astimezone():%Y}" in prompt
    assert r["scope"] == {"days": 14, "days_from_question": True, "sources": None, "meetings": 1, "tasks": True}
    assert [s["id"] for s in r["sources"]] == [recent]


def test_an_explicit_window_and_sources_win(client, ai):
    meeting("Last spring", 200, "Pricing talk.")
    gran = meeting("Granola pricing", 5, "Pricing talk.", source="granola")
    r = client.post("/api/ask", json={"question": "What about pricing recently?", "days": 0, "sources": ["granola"]}).json()
    assert r["scope"]["days"] is None and [s["id"] for s in r["sources"]] == [gran]
    sources = client.get("/api/ask/sources").json()
    assert sorted(s["label"] for s in sources) == ["Granola", "Trailmix"] and {s["count"] for s in sources} == {1}


def test_an_empty_window_says_so(client):
    meeting("Last spring", 200, "Old notes.")
    r = client.post("/api/ask", json={"question": "Anything?", "days": 7})
    assert r.status_code == 409 and "last 7 days" in r.json()["detail"]
