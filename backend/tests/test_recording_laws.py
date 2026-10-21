import json
from pathlib import Path

import pytest

import audio_store as store
import database as db
import recording_laws as laws
import settings

DOCS = Path(__file__).resolve().parent.parent.parent / "docs" / "recording-laws.json"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(laws, "_table", None)
    monkeypatch.setattr(laws, "_fetched_at", 0.0)


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


def built_in():
    return json.loads(laws.BUILT_IN.read_text())


@pytest.mark.parametrize("path", [laws.BUILT_IN, DOCS], ids=["built-in", "published"])
def test_every_entry_can_be_shown(path):
    table = json.loads(path.read_text())
    assert laws.valid(table)
    ids = [r["id"] for r in table["regions"]]
    assert len([i for i in ids if i.startswith("US-")]) == 51  # the 50 states and DC
    assert all(r.get("reviewed") for r in table["regions"])


def test_the_published_copy_matches_the_one_that_ships():
    assert json.loads(DOCS.read_text()) == built_in()


def test_a_region_without_an_entry_gets_the_general_one(fresh_db):
    assert laws.entry("US-CA")["consent"] == "all"
    assert laws.entry("XX")["id"] == "OTHER"


def test_a_newer_table_from_the_site_replaces_the_built_in_one(fresh_db, monkeypatch):
    newer = {**built_in(), "updated": "2099-01-01"}
    monkeypatch.setattr(laws.httpx, "get", lambda *a, **kw: Reply(newer))
    laws.refresh()
    assert laws.table()["updated"] == "2099-01-01"
    monkeypatch.setattr(laws, "_table", None)  # and it's remembered after a restart
    assert laws.table()["updated"] == "2099-01-01"


@pytest.mark.parametrize("fetched", [
    {"updated": "1999-01-01"},                                                   # older
    {"regions": "nope", "updated": "2099-01-01"},                                # wrong shape
    {"regions": [{"id": "US-CA", "name": "California", "summary": "x", "consent": "maybe", "sources": ["x"]}]},
], ids=["older", "malformed", "bad-entry"])
def test_an_older_or_broken_table_is_ignored(fresh_db, monkeypatch, fetched):
    data = {**built_in(), **fetched}
    monkeypatch.setattr(laws.httpx, "get", lambda *a, **kw: Reply(data))
    laws.refresh()
    assert laws.table() == built_in()


def test_lockdown_skips_the_fetch(fresh_db, monkeypatch):
    settings.update({"lockdown": True})
    monkeypatch.setattr(laws.httpx, "get", lambda *a, **kw: pytest.fail("fetched in Lockdown mode"))
    laws.refresh()


def test_retention_is_30_days_unless_changed(fresh_db, monkeypatch):
    assert store.retention_days() == 30
    monkeypatch.setenv("TRAILMIX_AUDIO_RETENTION_DAYS", "7")
    assert store.retention_days() == 7
    settings.update({"audio_retention_days": 0})  # a choice made in the app wins over the environment
    assert store.retention_days() == 0


def test_retention_must_be_a_number_of_days(fresh_db):
    with pytest.raises(ValueError):
        settings.update({"audio_retention_days": -5})
    with pytest.raises(ValueError):
        settings.update({"audio_retention_days": True})


def test_keeping_forever_never_sweeps(fresh_db, monkeypatch):
    settings.update({"audio_retention_days": -1})
    monkeypatch.setattr(db, "meetings_with_audio", lambda: pytest.fail("looked for audio to delete"))
    assert store.sweep_expired() == 0


def test_expiry_follows_the_setting(fresh_db):
    settings.update({"audio_retention_days": 0})  # audio from before the change goes at the next clean-up
    assert store.expires_at({"created_at": "2026-10-01T00:00:00Z"}).startswith("2026-10-01")
    assert store.expires_at({"created_at": "2026-10-01T00:00:00Z", "keep_audio": 1}) is None
    settings.update({"audio_retention_days": -1})
    assert store.expires_at({"created_at": "2026-10-01T00:00:00Z"}) is None
    settings.update({"audio_retention_days": 30})
    assert store.expires_at({"created_at": "2026-10-01T00:00:00Z"}).startswith("2026-10-31")
