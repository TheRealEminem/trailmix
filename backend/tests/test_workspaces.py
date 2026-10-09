import time

import pytest

import archive
import database as db
import llm_engine
import workspaces

CFG = {"auto_workspace": True}


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
    """A stand-in summary AI: answers whatever the test sets, and records the prompts it was given."""
    state = {"answer": "None", "prompts": []}
    monkeypatch.setattr(llm_engine, "chain", lambda choice, cfg: ["ollama"])
    monkeypatch.setattr(llm_engine, "unload", lambda pid, cfg: None)

    def generate(pid, cfg, prompt, system=None, keep_alive=None, exact=False):
        state["prompts"].append(prompt)
        return state["answer"]

    monkeypatch.setattr(llm_engine, "generate", generate)
    return state


def meeting(title: str, summary: str = "## Overview\nWe talked.") -> int:
    mid = db.create_meeting(title, title_auto=False)
    db.update_meeting(mid, summary=summary, transcript="x", status="done")
    return mid


def space(name: str) -> int:
    return db.workspace_by_name(name)["id"]


def test_a_workspace_named_in_the_title_wins_without_asking(fresh_db, ai):
    mid = meeting("CEPAC Study Session 1/28")
    workspaces.auto_sort(mid, CFG)
    assert db.get_meeting(mid)["workspace_id"] == space("CEPAC")
    assert ai["prompts"] == []


def test_otherwise_the_ai_picks_from_the_descriptions(fresh_db, ai):
    mid = meeting("Pitch refinement with Eli", "## Overview\nWe refined the investor deck for the cold-chain device.")
    ai["answer"] = "Workspace: Cold Connect.\nFit: good"
    workspaces.auto_sort(mid, CFG)
    m = db.get_meeting(mid)
    assert m["workspace_id"] == space("Cold Connect") and m["workspace_auto"] == 1
    assert "vaccine cold-chain hardware" in ai["prompts"][0]  # it saw what you wrote about each


def test_none_or_a_made_up_answer_leaves_it_unsorted(fresh_db, ai):
    for answer in ("Workspace: Personal\nFit: poor", "Workspace: Personal\nFit: partial", "Workspace: Book club\nFit: good",
                   "No idea"):
        mid = meeting("Something else")
        ai["answer"] = answer
        workspaces.auto_sort(mid, CFG)
        assert db.get_meeting(mid)["workspace_id"] is None


def test_meetings_placed_by_hand_are_never_moved(fresh_db, ai):
    mid = meeting("CEPAC prep over coffee")
    db.update_meeting(mid, workspace_id=space("Personal"))
    workspaces.auto_sort(mid, CFG)
    assert db.get_meeting(mid)["workspace_id"] == space("Personal")
    assert workspaces.sort_all(CFG)
    while workspaces.job_status().get("active"):
        time.sleep(0.05)
    assert db.get_meeting(mid)["workspace_id"] == space("Personal")


def test_sorting_everything(fresh_db, ai):
    ids = [meeting("CEPAC Meeting - 5 pm"), meeting("Cold Connect weekly check-in"), meeting("Dinner plans")]
    ai["answer"] = "None"
    assert workspaces.sort_all(CFG)
    while workspaces.job_status().get("active"):
        time.sleep(0.05)
    assert workspaces.job_status() == {"active": False, "total": 3, "done": 3, "sorted": 2}
    assert [db.get_meeting(i)["workspace_id"] for i in ids] == [space("CEPAC"), space("Cold Connect"), None]


def test_deleting_a_workspace_keeps_its_meetings(fresh_db):
    mid = meeting("Budget")
    db.update_meeting(mid, workspace_id=space("Personal"))
    db.delete_workspace(space("Personal"))
    assert db.get_meeting(mid)["workspace_id"] is None


def test_export_and_import_keep_the_workspace(fresh_db, tmp_path, monkeypatch):
    mid = meeting("Board prep")
    db.update_meeting(mid, workspace_id=space("Cold Connect"))
    record = archive.record(db.get_meeting(mid), None)
    assert record["workspace"] == "Cold Connect"

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "other.db")  # a fresh install with no workspaces
    db.init_db()
    new_id = archive.restore(record, None)
    assert db.get_workspace(db.get_meeting(new_id)["workspace_id"])["name"] == "Cold Connect"


def test_meetings_list_by_when_they_happened(fresh_db):
    late = db.create_imported_meeting("Later", "2026-03-01T10:00:00.000Z", "granola", "a")
    early = db.create_imported_meeting("Earlier", "2025-11-01T10:00:00.000Z", "granola", "b")
    middle = db.create_imported_meeting("Middle", "2026-01-01T10:00:00.000Z", "granola", "c")
    listed = [m["id"] for m in db.list_meetings() if m["id"] in (late, early, middle)]
    assert listed == [late, middle, early]


def test_the_ai_sees_meetings_already_in_each_workspace(fresh_db, ai):
    placed = meeting("D&D session: prison break")
    db.update_meeting(placed, workspace_id=space("Personal"))  # by hand
    mid = meeting("Cyberpunk Red: night market")
    ai["answer"] = "Workspace: Personal\nFit: good"
    workspaces.auto_sort(mid, CFG)
    assert db.get_meeting(mid)["workspace_id"] == space("Personal")
    assert '"D&D session: prison break"' in ai["prompts"][0]
