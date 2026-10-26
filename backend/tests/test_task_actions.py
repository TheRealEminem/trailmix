import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import archive
import database as db
import exporter
import main
import meeting_text
import settings
import trackers

SUMMARY = ("## Overview\nWe set the beta for Tuesday.\n\n## Action Items\n- Priya: fix the login bug\n"
           "- Alex: update docs\n- Sam: book the room\n\n## Decisions\n- Ship Tuesday")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    return tmp_path


async def _never():
    pass


@pytest.fixture
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_warm_loop", lambda: _never())
    monkeypatch.setattr(main.pipeline, "reexport", lambda mid: None)
    return TestClient(main.app)


def make_meeting(summary=SUMMARY) -> int:
    mid = db.create_meeting("Launch sync", title_auto=False)
    db.update_meeting(mid, transcript="x", transcribed=1, summary=summary, status="done")
    db.replace_tasks(mid, meeting_text.action_items(summary))
    return mid


def texts(tasks):
    return [t["text"] for t in tasks]


# ── Deleting ────────────────────────────────────────────────────────────

def test_a_deleted_task_is_hidden_everywhere_and_undo_brings_it_back(client):
    mid = make_meeting()
    docs = db.tasks_for(mid)[1]
    assert client.delete(f"/api/tasks/{docs['id']}").json() == {"ok": True}

    assert "Alex: update docs" not in texts(client.get("/api/tasks").json())
    m = client.get(f"/api/meetings/{mid}").json()
    assert texts(m["tasks"]) == ["Priya: fix the login bug", "Sam: book the room"] and m["tasks_deleted"] == 1
    assert db.get_task(docs["id"])["done"] is False  # deleted, not ticked off

    client.patch(f"/api/tasks/{docs['id']}", json={"deleted": False})
    assert texts(client.get(f"/api/meetings/{mid}").json()["tasks"])[1] == "Alex: update docs"
    assert client.delete("/api/tasks/9999").status_code == 404


def test_ticking_still_works_on_its_own(client):
    mid = make_meeting()
    first = db.tasks_for(mid)[0]["id"]
    client.patch(f"/api/tasks/{first}", json={"done": True})
    assert db.get_task(first)["done"] is True and db.get_task(first)["deleted"] is False


def test_a_deletion_survives_the_notes_being_rewritten(fresh_db):
    mid = make_meeting()
    db.update_task(db.tasks_for(mid)[1]["id"], deleted=True)
    db.update_task(db.tasks_for(mid)[0]["id"], done=True)
    # a regenerate: the same items, reworded a little and in another order, plus a new one
    rewritten = ["Sam: book the room", "Alex: update docs.", "Priya: Fix the login bug", "Dana: send the deck"]
    db.replace_tasks(mid, rewritten)
    assert texts(db.tasks_for(mid)) == ["Sam: book the room", "Priya: Fix the login bug", "Dana: send the deck"]
    assert db.tasks_for(mid)[1]["done"] is True
    assert [t["deleted"] for t in db.tasks_for(mid, deleted=True)] == [False, True, False, False]


def test_exports_leave_a_deleted_task_out_and_meeting_json_keeps_it_deleted(fresh_db, tmp_path, monkeypatch):
    mid = make_meeting()
    db.update_task(db.tasks_for(mid)[1]["id"], deleted=True)
    db.update_task(db.tasks_for(mid)[1]["id"], done=True)  # Sam's, now second of the ones showing

    notes = meeting_text.summary_with_tasks(SUMMARY, db.tasks_for(mid, deleted=True))
    assert "update docs" not in notes
    assert "- [ ] Priya: fix the login bug\n- [x] Sam: book the room\n\n## Decisions\n- Ship Tuesday" in notes
    assert meeting_text.action_items(notes) == ["Priya: fix the login bug", "Sam: book the room"]  # a Markdown re-import

    cfg = {"export_dir": str(tmp_path / "Exports"), "export_formats": ["md"], "export_summary": True,
           "export_transcript": False, "export_separate_files": False, "export_audio": False, "your_name": ""}
    paths = [Path(p) for p in exporter.export_meeting(db.get_meeting(mid), cfg)]
    md = next(p for p in paths if p.suffix == ".md").read_text()
    assert "fix the login bug" in md and "update docs" not in md
    record = json.loads(next(p for p in paths if p.name == "meeting.json").read_text())
    assert record["tasks"][1] == {"text": "Alex: update docs", "done": False, "deleted": True}

    # into a brand-new Trailmix, and then its notes rewritten there
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "new")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "new" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "new" / "t.db")
    db.init_db()
    new_id = archive.restore(record, paths[0].parent)
    assert texts(db.tasks_for(new_id)) == ["Priya: fix the login bug", "Sam: book the room"]
    assert db.tasks_for(new_id)[1]["done"] is True
    db.replace_tasks(new_id, meeting_text.action_items(SUMMARY))
    assert texts(db.tasks_for(new_id)) == ["Priya: fix the login bug", "Sam: book the room"]


def test_an_older_database_gets_the_new_columns(fresh_db):
    with db.connect() as conn:
        conn.execute("DROP TABLE action_items")
        conn.execute("CREATE TABLE action_items (id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER NOT NULL, "
                     "position INTEGER NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
    db.init_db()
    mid = make_meeting()
    assert db.tasks_for(mid)[0] | {"id": 0} == {"id": 0, "text": "Priya: fix the login bug", "done": False,
                                                 "deleted": False, "ticket": None}


# ── Sending to Traverse ─────────────────────────────────────────────────

class FakeTraverse:
    """Traverse's API, as much of it as Trailmix uses."""

    def __init__(self, status=201, token=""):
        self.made, self.status, self.token = [], status, token

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"detail": "Sign in"})
        if request.method == "GET" and request.url.path == "/api/tasks/projects":
            return httpx.Response(200, json=[{"id": "p1", "name": "Riverbend relaunch", "archived": False},
                                             {"id": "p2", "name": "Old", "archived": True}])
        if request.method == "POST" and request.url.path == "/api/tasks":
            if self.status >= 400:
                return httpx.Response(self.status, json={"detail": "Project not found"})
            self.made.append(json.loads(request.content))
            return httpx.Response(201, json={"id": f"t{len(self.made)}"})
        return httpx.Response(404)


@pytest.fixture
def traverse(client, monkeypatch):
    fake = FakeTraverse()
    monkeypatch.setattr(trackers, "_transport", httpx.MockTransport(lambda r: fake(r)))
    settings.update({"task_tracker": "traverse", "traverse_project": "p1"})
    return fake


def test_off_until_a_tracker_is_picked(client):
    mid = make_meeting()
    assert client.get("/api/trackers").json()["active"] == ""
    r = client.post(f"/api/tasks/{db.tasks_for(mid)[0]['id']}/send")
    assert r.status_code == 502 and "Settings → Tasks" in r.json()["detail"]
    assert db.tasks_for(mid)[0]["ticket"] is None


def test_the_picker_lists_traverse_and_what_it_needs(client):
    out = client.get("/api/trackers").json()
    assert [t["id"] for t in out["trackers"]] == ["traverse"]
    assert [f["kind"] for f in out["trackers"][0]["fields"]] == ["url", "secret", "project"]
    with pytest.raises(ValueError):
        settings.update({"task_tracker": "jira"})


def test_send_makes_a_ticket_once_and_remembers_it(client, traverse):
    mid = make_meeting()
    task = db.tasks_for(mid)[0]
    r = client.post(f"/api/tasks/{task['id']}/send")
    assert r.status_code == 200
    ticket = r.json()["ticket"]
    assert ticket | {"at": ""} == {"tracker": "traverse", "name": "Traverse", "id": "t1",
                                   "url": "http://127.0.0.1:8793/#/tasks/t/t1", "at": ""}
    sent = traverse.made[0]
    assert sent["text"] == "Priya: fix the login bug" and sent["project_id"] == "p1"
    assert f"[Launch sync](http://testserver/#meeting-{mid})" in sent["notes"]

    assert client.post(f"/api/tasks/{task['id']}/send").status_code == 409  # not twice
    assert len(traverse.made) == 1
    assert client.get("/api/tasks").json()[0]["ticket"]["id"] == "t1"
    assert client.get(f"/api/meetings/{mid}").json()["tasks"][0]["ticket"]["url"].endswith("/t1")

    # the notes rewritten: still sent, so still not twice
    db.replace_tasks(mid, ["Priya: fix the login bug.", "Alex: update docs"])
    assert db.tasks_for(mid)[0]["ticket"]["id"] == "t1"
    assert client.post(f"/api/tasks/{db.tasks_for(mid)[0]['id']}/send").status_code == 409
    assert archive.record(db.get_meeting(mid), None)["tasks"][0]["ticket"]["id"] == "t1"


def test_no_project_sends_none(client, traverse):
    settings.update({"traverse_project": ""})
    client.post(f"/api/tasks/{db.tasks_for(make_meeting())[0]['id']}/send")
    assert "project_id" not in traverse.made[0]


def test_projects_for_the_picker(client, traverse):
    assert client.get("/api/trackers/traverse/projects").json() == [{"id": "p1", "name": "Riverbend relaunch"}]
    assert client.get("/api/trackers/nope/projects").status_code == 404


def test_a_token_is_sent_when_traverse_asks_for_a_sign_in(client, traverse):
    traverse.token = "s3cret-token"
    mid = make_meeting()
    r = client.post(f"/api/tasks/{db.tasks_for(mid)[0]['id']}/send")
    assert r.status_code == 502 and "access token" in r.json()["detail"]
    settings.update({"traverse_api_key": "s3cret-token"})
    assert client.post(f"/api/tasks/{db.tasks_for(mid)[0]['id']}/send").status_code == 200
    assert settings.public()["traverse_api_key"] == ""  # never shown back


def test_when_traverse_says_no_or_is_closed_nothing_is_remembered(client, traverse, monkeypatch):
    mid = make_meeting()
    task = db.tasks_for(mid)[0]["id"]
    traverse.status = 404
    r = client.post(f"/api/tasks/{task}/send")
    assert r.status_code == 502 and "Project not found" in r.json()["detail"]

    def closed(request):
        raise httpx.ConnectError("Connection refused")
    monkeypatch.setattr(trackers, "_transport", httpx.MockTransport(closed))
    r = client.post(f"/api/tasks/{task}/send")
    assert r.status_code == 502 and "Is it open?" in r.json()["detail"]
    assert db.get_task(task)["ticket"] is None


def test_lockdown_keeps_tasks_on_this_computer(client, traverse):
    settings.update({"lockdown": True, "traverse_url": "https://traverse.example.com"})
    r = client.post(f"/api/tasks/{db.tasks_for(make_meeting())[0]['id']}/send")
    assert r.status_code == 502 and "Lockdown" in r.json()["detail"] and traverse.made == []
    settings.update({"traverse_url": "http://127.0.0.1:8793/"})
    assert settings.get_all()["traverse_url"] == "http://127.0.0.1:8793"
