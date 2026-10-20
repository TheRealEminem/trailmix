import os
import time
from pathlib import Path

import pytest

import database as db
import file_import
import pipeline
import settings
import watched

TRANSCRIPT = "Me: Shall we ship on Tuesday?\nThem: Yes, and Priya will update the docs by Monday.\n"
VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Dana>Morning all.\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(pipeline, "enqueue", lambda *a, **kw: None)
    monkeypatch.setattr(watched, "_pending", {})
    monkeypatch.setattr(watched, "_status", {})
    settings.update({"export_dir": str(tmp_path / "exports")})
    folder = tmp_path / "watch"
    folder.mkdir()
    return folder


def put(path: Path, text: str, age: float = 60) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def two_looks():
    watched.scan_all()
    watched.scan_all()


def titles() -> list[str]:
    return sorted(m["title"] for m in db.list_meetings())


def test_new_files_come_in_once_they_have_settled(setup):
    watched.add(str(setup))
    put(setup / "Budget review.txt", TRANSCRIPT)
    watched.scan_all()
    assert titles() == []  # seen once: might still be being written
    watched.scan_all()
    assert titles() == ["Budget review"]
    two_looks()
    assert len(db.list_meetings()) == 1  # each file only once
    assert watched.status()["folders"][0]["imported"] == 1


def test_a_file_still_changing_waits(setup):
    watched.add(str(setup))
    put(setup / "Standup.vtt", VTT, age=0)
    two_looks()
    assert titles() == [] and watched.status()["folders"][0]["waiting"] == 1


def test_a_file_dated_ahead_still_comes_in(setup):
    watched.add(str(setup))
    put(setup / "From a fast clock.vtt", VTT, age=-3600)
    two_looks()
    assert titles() == ["From a fast clock"]


def test_whats_there_already_stays_out_unless_asked(setup, tmp_path):
    put(setup / "Old.vtt", VTT)
    watched.add(str(setup))
    two_looks()
    assert titles() == []
    other = tmp_path / "other"
    put(other / "Old too.vtt", VTT)
    watched.add(str(other), existing=True)
    two_looks()
    assert titles() == ["Old too"]


def test_notes_that_arent_transcripts_are_left_out(setup):
    watched.add(str(setup))
    put(setup / "Shopping list.md", "eggs\nmilk\nthe good bread\n")
    put(setup / ".hidden.vtt", VTT)
    put(setup / "Half.vtt.crdownload", VTT)
    two_looks()
    assert titles() == []


def test_zoom_folders_name_the_meeting_and_audio_beats_video(setup, monkeypatch):
    made = []
    monkeypatch.setattr(file_import, "recording_from", lambda name, path, when: made.append((name, path.name)) or len(made))
    watched.add(str(setup))
    meeting = setup / "2026-10-20 10.00.00 Weekly sync"
    put(meeting / "audio1234567890.m4a", "x")
    put(meeting / "video1234567890.mp4", "x")
    two_looks()
    assert made == [("Weekly sync.m4a", "audio1234567890.m4a")]


def test_too_deep_and_trailmix_folders_are_not_looked_in(setup, tmp_path):
    settings.update({"export_dir": str(setup / "exports")})
    watched.add(str(setup))
    put(setup / "exports" / "2026-10-20 Sync" / "Transcript.vtt", VTT)
    put(setup / "a" / "b" / "c" / "d" / "Deep.vtt", VTT)
    put(setup / "a" / "b" / "c" / "Shallow.vtt", VTT)
    two_looks()
    assert titles() == ["Shallow"]
    with pytest.raises(ValueError, match="Trailmix keeps or exports"):
        watched.add(str(setup / "exports"))


def test_a_bad_file_is_noted_and_not_tried_again(setup, monkeypatch):
    calls = []

    def broken(name, path, when):
        calls.append(name)
        raise ValueError(f"{name}: no audio found in it")

    monkeypatch.setattr(file_import, "recording_from", broken)
    watched.add(str(setup))
    put(setup / "Broken.m4a", "x")
    two_looks()
    two_looks()
    assert calls == ["Broken.m4a"] and watched.status()["folders"][0]["failed"] == 1


def test_a_missing_folder_says_so_and_stopping_keeps_the_meetings(setup):
    watched.add(str(setup))
    put(setup / "Kept.vtt", VTT)
    two_looks()
    setup.joinpath("Kept.vtt").unlink()
    setup.rmdir()
    watched.scan_all()
    assert "isn't there" in watched.status()["folders"][0]["error"]
    watched.remove(str(setup))
    assert watched.status()["folders"] == [] and titles() == ["Kept"]


def test_bad_paths_are_refused(setup):
    with pytest.raises(ValueError, match="full path"):
        watched.add("Documents/Zoom")
    with pytest.raises(ValueError, match="no folder"):
        watched.add(str(setup / "nope"))


def test_the_api_adds_and_removes_folders(setup, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setattr(main.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    client = TestClient(main.app)
    r = client.post("/api/import/folders", json={"path": str(setup)})
    assert r.status_code == 200 and r.json()["folders"][0]["path"] == str(setup.resolve())
    assert client.post("/api/import/folders", json={"path": "relative/path"}).status_code == 422
    assert client.request("DELETE", "/api/import/folders", params={"path": str(setup.resolve())}).json()["folders"] == []


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")
def test_a_real_recording_comes_in_and_the_original_is_left_alone(setup):
    import subprocess

    watched.add(str(setup))
    original = setup / "Call with Dana.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "aac", str(original)], check=True)
    before = original.read_bytes()
    old = time.time() - 60
    os.utime(original, (old, old))
    two_looks()
    [m] = db.list_meetings()
    assert m["title"] == "Call with Dana" and original.read_bytes() == before
