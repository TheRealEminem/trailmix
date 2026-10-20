from datetime import datetime, timedelta, timezone

import pytest

import database as db
import importer
import pipeline
import settings
import sources


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(pipeline, "enqueue", lambda *a, **kw: None)
    monkeypatch.setattr(importer, "_REQUEST_GAP_S", 0)
    settings.update({"granola_api_key": "grn_test"})


class FakeGranola:
    """Granola's list and import, as sources.py sees them: notes made at the given times, and what was asked."""

    def __init__(self, monkeypatch, made: dict[str, datetime]):
        self.made, self.asked_after, self.imported, self.fail = made, [], [], set()
        monkeypatch.setitem(sources.SOURCES, "granola", sources.Source(
            "granola", "Granola", "granola_api_key", self.notes, self.import_note))

    def notes(self, key, after):
        self.asked_after.append(after)
        have = set(self.imported)
        return [{"id": i, "created_at": importer._iso(t), "imported": i in have}
                for i, t in self.made.items() if not after or t >= after]

    def import_note(self, key, note_id, keep):
        if note_id in self.fail:
            raise importer.ImportError_("Granola answered 500")
        self.imported.append(note_id)
        return len(self.imported)


def ago(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


def test_turned_on_from_now_brings_in_only_new_meetings(fresh_db, monkeypatch):
    g = FakeGranola(monkeypatch, {"old": ago(days=3), "new": ago(days=0) + timedelta(minutes=5)})
    sources.set_auto("granola", True, "now")
    assert settings.get_all()["auto_import"] == ["granola"]
    assert sources.sync("granola") == 1 and g.imported == ["new"]
    state = sources.status()[0]
    assert state["auto"] and state["last_count"] == 1 and state["total"] == 1 and state["error"] is None
    assert sources.sync("granola") == 0 and state["total"] == sources.status()[0]["total"]  # not twice


def test_a_week_back_or_everything(fresh_db, monkeypatch):
    g = FakeGranola(monkeypatch, {"a": ago(days=30), "b": ago(days=3)})
    sources.set_auto("granola", True, "week")
    sources.sync("granola")
    assert g.imported == ["b"]
    sources.set_auto("granola", True, "all")
    sources.sync("granola")
    assert g.imported == ["b", "a"] and g.asked_after[-1] is None


def test_later_syncs_look_back_a_day_from_the_last_one(fresh_db, monkeypatch):
    g = FakeGranola(monkeypatch, {})
    sources.set_auto("granola", True, "all")
    sources.sync("granola")
    sources.sync("granola")
    assert g.asked_after[0] is None
    assert timedelta(hours=23) < datetime.now(timezone.utc) - g.asked_after[1] < timedelta(hours=25)


def test_turned_on_in_settings_without_a_start_begins_now(fresh_db, monkeypatch):
    g = FakeGranola(monkeypatch, {"old": ago(hours=2)})
    settings.update({"auto_import": ["granola"]})
    sources.sync_all()
    assert g.imported == [] and sources.status()[0]["since"]


def test_one_failing_note_doesnt_stop_the_rest(fresh_db, monkeypatch):
    g = FakeGranola(monkeypatch, {"bad": ago(hours=1), "good": ago(hours=2)})
    g.fail.add("bad")
    sources.set_auto("granola", True, "week")
    assert sources.sync("granola") == 1
    assert "500" in sources.status()[0]["error"]
    g.fail.clear()
    assert sources.sync("granola") == 1 and sources.status()[0]["error"] is None  # tried again next time


def test_an_unreachable_source_is_reported_and_tried_again(fresh_db, monkeypatch):
    g = FakeGranola(monkeypatch, {"a": ago(hours=1)})
    sources.set_auto("granola", True, "week")
    monkeypatch.setattr(g, "made", None)  # listing blows up
    assert sources.sync("granola") == 0 and sources.status()[0]["error"]
    assert sources.status()[0]["last_ok"] is None


def test_nothing_is_fetched_in_lockdown_or_without_a_key(fresh_db, monkeypatch):
    g = FakeGranola(monkeypatch, {"a": ago(hours=1)})
    sources.set_auto("granola", True, "week")
    settings.update({"lockdown": True})
    sources.sync_all()
    assert sources.sync("granola") == 0 and not g.asked_after
    settings.update({"lockdown": False, "granola_api_key": ""})
    sources.sync_all()
    assert not g.asked_after


def test_turned_off_isnt_synced(fresh_db, monkeypatch):
    g = FakeGranola(monkeypatch, {"a": ago(hours=1)})
    sources.set_auto("granola", True, "week")
    sources.set_auto("granola", False)
    assert settings.get_all()["auto_import"] == []
    sources.sync_all()
    assert not g.asked_after


def test_unknown_sources_are_refused():
    with pytest.raises(ValueError, match="auto_import"):
        settings.update({"auto_import": ["otter"]})


def test_a_recording_pulls_in_notes_made_around_it(fresh_db, monkeypatch):
    rec = db.create_meeting("Standup", title_auto=False)
    start = importer._parse_time(db.get_meeting(rec)["created_at"])
    db.update_meeting(rec, duration_sec=1200)
    g = FakeGranola(monkeypatch, {"same": start + timedelta(minutes=1), "later": start + timedelta(hours=3)})
    assert sources.pull_for("granola", rec, "grn_test") == 1 and g.imported == ["same"]


def test_the_api_turns_a_source_on_and_reports_it(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    FakeGranola(monkeypatch, {})
    monkeypatch.setattr(sources, "sync_soon", lambda name: None)
    client = TestClient(main.app)  # no `with`: the background loops don't start
    assert client.put("/api/import/sources/otter", json={"auto": True}).status_code == 404
    assert client.put("/api/import/sources/granola", json={"auto": True, "start": "soon"}).status_code == 422
    r = client.put("/api/import/sources/granola", json={"auto": True, "start": "week"})
    assert r.status_code == 200 and r.json()["sources"][0]["auto"] is True
    assert client.post("/api/import/sources/granola/sync").status_code == 202
    settings.update({"lockdown": True})
    assert client.put("/api/import/sources/granola", json={"auto": True}).status_code == 409
    assert client.put("/api/import/sources/granola", json={"auto": False}).status_code == 200  # off is fine
    settings.update({"lockdown": False, "granola_api_key": ""})
    assert "API key" in client.put("/api/import/sources/granola", json={"auto": True}).json()["detail"]


def test_connections_list_services_and_folders_alike(fresh_db, monkeypatch, tmp_path):
    import watched

    FakeGranola(monkeypatch, {})
    monkeypatch.setattr(watched, "_suggestions", lambda watching: [])
    folder = tmp_path / "Zoomish"
    folder.mkdir()
    watched.add(str(folder))
    rows = sources.connections()["connections"]
    assert [(r["kind"], r["label"]) for r in rows] == [("service", "Granola"), ("folder", "Zoomish")]
    assert set(rows[0]) == set(rows[1])  # one shape for the page
    sources.disconnect("granola")
    out = sources.connections()
    assert [r["kind"] for r in out["connections"]] == ["folder"]
    assert out["available"]["services"] == [{"name": "granola", "label": "Granola"}]


def test_a_key_that_doesnt_work_is_refused_when_connecting(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    def refuse(key, after):
        raise importer.ImportError_("Granola didn't accept that API key.")

    monkeypatch.setitem(sources.SOURCES, "granola", sources.Source("granola", "Granola", "granola_api_key", refuse,
                                                                   lambda *a: None))
    r = TestClient(main.app).put("/api/import/sources/granola", json={"auto": True})
    assert r.status_code == 422 and "didn't accept" in r.json()["detail"]
    assert settings.get_all()["auto_import"] == []
