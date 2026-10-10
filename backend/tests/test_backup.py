import os
import sqlite3
import time

import database as db


def _use(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "trailmix.db")
    monkeypatch.setattr(db, "BACKUP_DIR", tmp_path / "backups")
    conn = sqlite3.connect(tmp_path / "trailmix.db")
    conn.execute("CREATE TABLE meetings (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO meetings (title) VALUES ('Launch plan')")
    conn.commit()
    conn.close()


def test_a_new_version_backs_up_the_database_once(tmp_path, monkeypatch):
    _use(tmp_path, monkeypatch)
    (tmp_path / "last-version").write_text("0.12.0")
    copy = db.back_up_for("0.13.0")
    assert copy.name == "trailmix-0.12.0-before-0.13.0.db"
    assert sqlite3.connect(copy).execute("SELECT title FROM meetings").fetchone() == ("Launch plan",)
    assert db.back_up_for("0.13.0") is None  # same version again: nothing new
    assert (tmp_path / "last-version").read_text() == "0.13.0"


def test_going_back_is_a_version_change_too(tmp_path, monkeypatch):
    _use(tmp_path, monkeypatch)
    (tmp_path / "last-version").write_text("0.13.0")
    assert db.back_up_for("0.12.0").name == "trailmix-0.13.0-before-0.12.0.db"


def test_the_first_run_with_backups_still_backs_up(tmp_path, monkeypatch):
    _use(tmp_path, monkeypatch)
    assert db.back_up_for("0.13.0").name == "trailmix-earlier-before-0.13.0.db"


def test_a_fresh_install_has_nothing_to_back_up(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "trailmix.db")
    monkeypatch.setattr(db, "BACKUP_DIR", tmp_path / "backups")
    assert db.back_up_for("0.13.0") is None and not (tmp_path / "backups").exists()


def test_only_the_newest_three_are_kept(tmp_path, monkeypatch):
    _use(tmp_path, monkeypatch)
    for i, version in enumerate(["0.13.0", "0.14.0", "0.15.0", "0.16.0", "0.17.0"]):
        copy = db.back_up_for(version)
        os.utime(copy, (time.time() + i, time.time() + i))
    kept = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert kept == ["trailmix-0.14.0-before-0.15.0.db", "trailmix-0.15.0-before-0.16.0.db", "trailmix-0.16.0-before-0.17.0.db"]
