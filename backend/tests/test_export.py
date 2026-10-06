import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

import archive
import database as db
import documents
import exporter


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    return tmp_path


def make_meeting(audio: bool) -> dict:
    mid = db.create_meeting("Launch sync", title_auto=False)
    segs = [{"start": 0, "end": 2, "speaker": "You", "text": "Shall we ship Tuesday?"},
            {"start": 2.5, "end": 5, "speaker": "Them", "text": "Yes, after the login fix."}]
    summary = "## Overview\nWe set the beta for **Tuesday**.\n\n## Action Items\n- [ ] Priya: fix the login bug\n- [ ] Alex: update docs"
    db.update_meeting(mid, transcript="x", segments_json=segs, transcribed=1, has_system=1, duration_sec=5, summary=summary,
                      status="done", speaker_names_json={"Them": "Priya"})
    db.replace_tasks(mid, ["Priya: fix the login bug", "Alex: update docs"])
    db.set_task_done(db.tasks_for(mid)[0]["id"], True)
    if audio:
        folder = db.AUDIO_DIR / str(mid)
        folder.mkdir(parents=True)
        (folder / "mic.ogg").write_bytes(b"mic audio")
        (folder / "system.ogg").write_bytes(b"system audio")
        (folder / "mix.ogg").write_bytes(b"both sides")
        db.update_meeting(mid, audio_deleted=0)
    return db.get_meeting(mid)


def cfg(tmp_path, **over):
    base = {"export_dir": str(tmp_path / "Exports"), "export_formats": ["pdf", "docx", "odt", "md", "txt"], "export_summary": True,
            "export_transcript": True, "export_separate_files": False, "export_audio": False, "your_name": "Alex"}
    return {**base, **over}


def test_every_format_renders(fresh_db):
    doc = documents.meeting_doc("T", __import__("datetime").datetime(2026, 1, 1, 9), 60, "", "## A\n- [x] done\n- b",
                                [{"time": "00:01", "speaker": "You", "text": "Hi"}])
    assert documents.render(doc, "pdf").startswith(b"%PDF")
    for fmt, part in (("docx", "word/document.xml"), ("odt", "content.xml")):
        with zipfile.ZipFile(BytesIO(documents.render(doc, fmt))) as z:
            assert "Hi" in z.read(part).decode()
    assert b"- [x] done" in documents.render(doc, "md")


def test_folder_per_meeting_with_linked_audio(fresh_db, monkeypatch):
    monkeypatch.setattr(exporter.store, "playback_path", lambda m: db.AUDIO_DIR / str(m["id"]) / "mix.ogg")
    m = make_meeting(audio=True)
    paths = [Path(p) for p in exporter.export_meeting(m, cfg(fresh_db), include_audio=True)]
    folder = paths[0].parent
    names = {p.relative_to(folder).as_posix() for p in paths}
    assert {"Launch sync.pdf", "Launch sync.docx", "meeting.json", "Recording.ogg", "Tracks/You.ogg", "Tracks/Them.ogg"} <= names
    # the recording is the same file as Trailmix's own (a hard link), not a copy
    assert (folder / "Tracks" / "You.ogg").stat().st_ino == (db.AUDIO_DIR / str(m["id"]) / "mic.ogg").stat().st_ino
    record = json.loads((folder / "meeting.json").read_text())
    assert record["tasks"][0] == {"text": "Priya: fix the login bug", "done": True} and record["speakers"] == {"Them": "Priya"}


def test_rename_moves_the_folder_and_drops_formats_turned_off(fresh_db):
    m = make_meeting(audio=False)
    first = exporter.export_meeting(m, cfg(fresh_db))
    db.update_meeting(m["id"], title="Beta launch", exported_paths=json.dumps(first))
    again = [Path(p) for p in exporter.export_meeting(db.get_meeting(m["id"]), cfg(fresh_db, export_formats=["pdf"]))]
    assert not Path(first[0]).parent.exists()
    assert {p.name for p in again} == {"Beta launch.pdf", "meeting.json"}
    assert sorted(p.name for p in again[0].parent.iterdir()) == ["Beta launch.pdf", "meeting.json"]


def test_export_then_import_into_a_fresh_trailmix(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(exporter.store, "playback_path", lambda m: db.AUDIO_DIR / str(m["id"]) / "mix.ogg")
    m = make_meeting(audio=True)
    folder = Path(exporter.export_meeting(m, cfg(fresh_db), include_audio=True)[0]).parent
    record = json.loads((folder / "meeting.json").read_text())

    # a brand-new, empty Trailmix
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "new")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "new" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "new" / "t.db")
    db.init_db()
    new_id = archive.restore(record, folder)
    back = db.get_meeting(new_id)
    assert back["title"] == "Launch sync" and back["status"] == "done" and back["keep_audio"] == 1
    assert [(t["text"], bool(t["done"])) for t in db.tasks_for(new_id)] == [("Priya: fix the login bug", True), ("Alex: update docs", False)]
    assert (Path(back["audio_dir"]) / "mic.ogg").read_bytes() == b"mic audio"
    assert archive.restore(record, folder) is None  # already here: not imported twice
