import time

import pytest
from fastapi.testclient import TestClient

import archive
import database as db
import llm_engine
import workspaces

CFG = {"auto_workspace": True, "ollama_model": "qwen2.5:7b", "summary_provider": "ollama", "summary_fallback": "none"}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    for name, about in [("Cold Connect", "my startup: vaccine cold-chain hardware"),
                        ("CEPAC", "city climate & environment commission"), ("Personal", "")]:
        db.create_workspace(name, about=about)
    return tmp_path


@pytest.fixture
def ai(monkeypatch):
    """A stand-in summary AI: answers whatever the test sets (by kind of question), records the prompts."""
    state = {"answer": "None", "tags": "investor pitch, hardware", "prompts": []}
    monkeypatch.setattr(llm_engine, "chain", lambda choice, cfg: ["ollama"])
    monkeypatch.setattr(llm_engine, "unload", lambda pid, cfg: None)
    monkeypatch.setattr(llm_engine, "model_for", lambda pid, cfg: cfg.get("ollama_model", "qwen2.5:7b"))

    def generate(pid, cfg, prompt, system=None, keep_alive=None, exact=False):
        state["prompts"].append(prompt)
        if "topic tags" in prompt:
            return state["tags"]
        if "Group these meetings into" in prompt:
            return state["suggest"]
        return state["answer"]

    monkeypatch.setattr(llm_engine, "generate", generate)
    return state


def meeting(title: str, summary: str = "## Overview\nWe talked.") -> int:
    mid = db.create_meeting(title, title_auto=False)
    db.update_meeting(mid, summary=summary, transcript="x", status="done")
    return mid


def space(name: str) -> int:
    return db.workspace_by_name(name)["id"]


def wait() -> dict:
    while workspaces.job_status().get("active"):
        time.sleep(0.02)
    return workspaces.job_status()


def test_a_workspace_named_in_the_title_wins_without_asking(fresh_db, ai):
    mid = meeting("CEPAC Study Session 1/28")
    workspaces.after_notes(mid, CFG)
    m = db.get_meeting(mid)
    assert m["workspace_id"] == space("CEPAC") and m["workspace_model"] is None  # the title decided
    assert not any("WORKSPACES:" in p for p in ai["prompts"])


def test_otherwise_the_ai_picks_from_the_descriptions(fresh_db, ai):
    mid = meeting("Pitch refinement with Eli", "## Overview\nWe refined the investor deck for the cold-chain device.")
    ai["answer"] = "Workspace: Cold Connect.\nFit: good"
    workspaces.after_notes(mid, CFG)
    m = db.get_meeting(mid)
    assert m["workspace_id"] == space("Cold Connect") and m["workspace_auto"] == 1
    assert m["workspace_model"] == "qwen2.5:7b"
    sort_prompt = next(p for p in ai["prompts"] if "WORKSPACES:" in p)
    assert "vaccine cold-chain hardware" in sort_prompt  # it saw what you wrote about each
    assert "Topics: investor pitch, hardware" in sort_prompt  # and the meeting's tags


def test_none_or_a_made_up_answer_leaves_it_in_default(fresh_db, ai):
    for answer in ("Workspace: Personal\nFit: poor", "Workspace: Personal\nFit: partial", "Workspace: Book club\nFit: good",
                   "No idea"):
        mid = meeting("Something else")
        ai["answer"] = answer
        workspaces.after_notes(mid, CFG)
        assert db.get_meeting(mid)["workspace_id"] is None


def test_meetings_placed_by_hand_are_never_moved(fresh_db, ai):
    mid = meeting("CEPAC prep over coffee")
    db.update_meeting(mid, workspace_id=space("Personal"))
    workspaces.after_notes(mid, CFG)
    for kind in ("sort", "resort"):
        assert workspaces.start(kind, CFG)
        wait()
        assert db.get_meeting(mid)["workspace_id"] == space("Personal")


def test_the_ai_sees_meetings_already_in_each_workspace(fresh_db, ai):
    placed = meeting("D&D session: prison break")
    db.update_meeting(placed, workspace_id=space("Personal"))  # by hand
    mid = meeting("Cyberpunk Red: night market")
    ai["answer"] = "Workspace: Personal\nFit: good"
    workspaces.after_notes(mid, CFG)
    assert db.get_meeting(mid)["workspace_id"] == space("Personal")
    assert '"D&D session: prison break"' in next(p for p in ai["prompts"] if "WORKSPACES:" in p)


def test_sorting_and_resorting(fresh_db, ai):
    ids = [meeting("CEPAC Meeting - 5 pm"), meeting("Cold Connect weekly check-in"), meeting("Dinner plans")]
    ai["answer"] = "Workspace: Personal\nFit: good"
    assert workspaces.start("sort", CFG)
    assert wait() | {"model": None} == {"active": False, "kind": "sort", "total": 3, "done": 3, "sorted": 3,
                                         "model": None, "suggestions": None}
    assert [db.get_meeting(i)["workspace_id"] for i in ids] == [space("CEPAC"), space("Cold Connect"), space("Personal")]
    # A better model thinks dinner plans fit nowhere: re-sorting puts it back in Default.
    ai["answer"] = "Workspace: Personal\nFit: poor"
    assert workspaces.start("resort", {**CFG, "ollama_model": "claude-opus-5-5"})
    wait()
    assert [db.get_meeting(i)["workspace_id"] for i in ids] == [space("CEPAC"), space("Cold Connect"), None]


def test_tags(fresh_db, ai):
    mid = meeting("Board prep")
    ai["tags"] = "Tags: Investor Pitch, #fundraising, cold_chain hardware, investor pitch"
    workspaces.tag(mid, CFG)
    m = db.get_meeting(mid)
    assert workspaces.tags_of(m) == ["investor pitch", "fundraising", "cold chain hardware"]
    assert m["tags_model"] == "qwen2.5:7b"
    assert [h["id"] for h in db.search("fundraising")] == [mid]  # tags are searchable


def test_suggesting_workspaces_from_tags(fresh_db, ai):
    for title in ("Pitch practice", "Board prep", "Commission meeting"):
        meeting(title)
    ai["suggest"] = ("1. **Oregon AI Accelerator** | the accelerator cohort and its workshops\n"
                     "CEPAC | already have it\nSchool | Comm 485 class sessions\nAcme Robotics | the example\n"
                     "not a suggestion")
    assert workspaces.start("suggest", CFG)
    job = wait()
    assert job["done"] == 3  # tagged them first
    assert job["suggestions"] == [
        {"name": "Oregon AI Accelerator", "about": "the accelerator cohort and its workshops"},
        {"name": "School", "about": "Comm 485 class sessions"},
    ]


def test_deleting_a_workspace_keeps_its_meetings(fresh_db):
    mid = meeting("Budget")
    db.update_meeting(mid, workspace_id=space("Personal"))
    db.delete_workspace(space("Personal"))
    assert db.get_meeting(mid)["workspace_id"] is None


def test_export_and_import_keep_the_workspace_and_provenance(fresh_db, tmp_path, monkeypatch):
    mid = meeting("Board prep")
    db.update_meeting(mid, workspace_id=space("Cold Connect"), summary_model="qwen2.5:7b", tags_json=["fundraising"],
                      transcribed_with="whisper-large-v3-turbo, while recording")
    record = archive.record(db.get_meeting(mid), None)
    assert record["workspace"] == "Cold Connect" and record["tags"] == ["fundraising"]

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "other.db")  # a fresh install with no workspaces
    db.init_db()
    m = db.get_meeting(archive.restore(record, None))
    assert db.get_workspace(m["workspace_id"])["name"] == "Cold Connect"
    assert (m["summary_model"], m["transcribed_with"]) == ("qwen2.5:7b", "whisper-large-v3-turbo, while recording")


def test_meetings_list_by_when_they_happened(fresh_db):
    late = db.create_imported_meeting("Later", "2026-03-01T10:00:00.000Z", "granola", "a")
    early = db.create_imported_meeting("Earlier", "2025-11-01T10:00:00.000Z", "granola", "b")
    middle = db.create_imported_meeting("Middle", "2026-01-01T10:00:00.000Z", "granola", "c")
    listed = [m["id"] for m in db.list_meetings() if m["id"] in (late, early, middle)]
    assert listed == [late, middle, early]


def test_a_better_model_offers_to_redo_older_work(fresh_db, ai, monkeypatch):
    import main
    import settings

    monkeypatch.setattr(settings, "get_all", lambda: {**settings.DEFAULTS, **CFG})
    old = meeting("Old notes")
    db.update_meeting(old, summary_provider="ollama", summary_model="qwen2.5:3b", tags_json=["x"], tags_model="qwen2.5:3b")
    same = meeting("Recent notes")
    db.update_meeting(same, summary_provider="ollama", summary_model="qwen2.5:7b")
    granola = meeting("From Granola")
    db.update_meeting(granola, summary_provider="granola", summary_model="Granola")  # not ours to rewrite
    client = TestClient(main.app)
    got = client.get("/api/upgrades").json()
    assert got["model"] == "qwen2.5:7b"
    assert got["notes"] == {"count": 1, "from": {"qwen2.5:3b": 1}}
    assert got["tags"]["count"] == 1


def test_regenerating_a_title_from_the_notes(fresh_db, ai, monkeypatch):
    import main
    import settings

    monkeypatch.setattr(settings, "get_all", lambda: {**settings.DEFAULTS, **CFG})
    mid = meeting("Meeting Oct 08, 16:59", "## Overview\nThe commission postponed the housing code amendment.")
    ai["answer"] = "Title: Housing code amendment postponed"
    got = TestClient(main.app).post(f"/api/meetings/{mid}/retitle").json()
    assert got["title"] == db.get_meeting(mid)["title"] == "Housing code amendment postponed"


def test_moving_a_meeting_to_another_date(fresh_db, ai, monkeypatch):
    import main
    import settings

    monkeypatch.setattr(settings, "get_all", lambda: {**settings.DEFAULTS, **CFG})
    mid = meeting("CEPAC work plan", "## Overview\nWork plan for 2026.")
    arrived = db.get_meeting(mid)["created_at"]
    db.update_meeting(mid, transcript="[0:42] a study session on December 15th\n[1:10] the work plan for 2026")
    client = TestClient(main.app)

    assert client.patch(f"/api/meetings/{mid}", json={"created_at": "2025-11-17T18:30:00-08:00"}).status_code == 200
    assert db.get_meeting(mid)["created_at"] == "2025-11-18T02:30:00.000Z"
    assert db.get_meeting(mid)["title"] == "CEPAC work plan"  # a date change leaves the title alone
    assert db.get_meeting(mid)["imported_at"] == arrived  # its audio's 30 days don't restart from the new date
    assert client.patch(f"/api/meetings/{mid}", json={"created_at": "2099-01-01"}).status_code == 422
    assert client.patch(f"/api/meetings/{mid}", json={"created_at": "last Tuesday"}).status_code == 422

    ai["answer"] = '{"date": "2025-11-17", "sure_of": "month", "why": "A December 15th study session is coming up."}'
    got = client.post(f"/api/meetings/{mid}/guess-date").json()
    assert got["date"] == "2025-11-17" and got["sure_of"] == "month"
    assert "December 15th" in ai["prompts"][-1]
    assert db.get_meeting(mid)["created_at"] == "2025-11-18T02:30:00.000Z"  # only a suggestion
