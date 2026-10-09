import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pytest

import database as db
import file_import

ME = {"Mark Morrison"}

ZOOM_VTT = """WEBVTT

1
00:00:01.200 --> 00:00:04.500
Mark Morrison: Morning everyone, let's start with the pilot.

2
00:00:04.600 --> 00:00:06.000
Mark Morrison: Direct Relief said yes.

3
00:00:07.000 --> 00:00:10.250
Priya Shah: Great, I'll send the contract today.
"""

TEAMS_VTT = """WEBVTT

00:00:00.000 --> 00:00:03.000
<v Priya Shah>Can everyone hear me?</v>

00:00:03.500 --> 00:00:05.000
<v Mark Morrison>Yes, loud and clear.</v>
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
Welcome to the commission meeting.

2
00:00:05,000 --> 00:00:08,500
First item is the housing code.
"""

OTTER = """Mark Morrison  0:05
Let's review the minutes from last time.

Jeff  1:12
I moved to approve them.
"""


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(file_import.pipeline, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(file_import.importer.pipeline, "enqueue", lambda *a, **k: None)
    return tmp_path


def test_zoom_captions_with_names_join_into_turns():
    segs = file_import.parse_transcript("GMT20261001.transcript.vtt", ZOOM_VTT.encode(), ME)
    assert segs == [
        {"start": 1.2, "end": 6.0, "speaker": "You", "text": "Morning everyone, let's start with the pilot. Direct Relief said yes."},
        {"start": 7.0, "end": 10.25, "speaker": "Them", "text": "Great, I'll send the contract today."},
    ]


def test_teams_voice_tags():
    segs = file_import.parse_transcript("meeting.vtt", TEAMS_VTT.encode(), ME)
    assert [(s["speaker"], s["text"]) for s in segs] == [("Them", "Can everyone hear me?"), ("You", "Yes, loud and clear.")]


def test_srt_without_speakers():
    segs = file_import.parse_transcript("commission.srt", SRT.encode(), ME)
    assert [(s["start"], s["speaker"], s["text"]) for s in segs] == [
        (1.0, None, "Welcome to the commission meeting."), (5.0, None, "First item is the housing code.")]


def test_otter_text_and_word_exports():
    for name, data in [("otter.txt", OTTER.encode()), ("otter.docx", _docx(OTTER))]:
        segs = file_import.parse_transcript(name, data, ME)
        assert [(s["start"], s["speaker"], s["text"]) for s in segs] == [
            (5, "You", "Let's review the minutes from last time."), (72, "Them", "I moved to approve them.")], name


def test_a_transcript_file_becomes_a_meeting_once(fresh_db):
    when = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
    mid = file_import.import_transcript("Weekly_sync.vtt", ZOOM_VTT.encode(), when, ME)
    m = db.get_meeting(mid)
    assert (m["title"], m["created_at"], m["transcribed_with"]) == ("Weekly sync", "2026-10-01T17:00:00.000Z", "a transcript file")
    assert file_import.import_transcript("Weekly_sync.vtt", ZOOM_VTT.encode(), when, ME) is None  # already here


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_recording_is_converted_and_the_copy_deleted(fresh_db, tmp_path):
    original = tmp_path / "Board call.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:d=3", "-shortest", "-metadata",
                    "creation_time=2026-09-15T14:30:00Z", str(original)], check=True)
    temp = file_import.temp_path(original.name)
    shutil.copy(original, temp)
    mid = file_import.import_recording(original.name, temp, datetime(2026, 10, 9, tzinfo=timezone.utc))
    m = db.get_meeting(mid)
    assert not temp.exists() and original.exists()  # the copy is gone, your file isn't
    assert m["created_at"] == "2026-09-15T14:30:00.000Z"  # when it was recorded, from the file
    assert m["imported_at"] and m["title_auto"] == 1 and m["status"] == "queued" and 2.5 < m["duration_sec"] < 3.5
    assert (db.AUDIO_DIR / str(mid) / "mic.ogg").stat().st_size > 0


def _docx(text: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paras = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{line.replace("  ", "")}</w:t></w:r></w:p>' if "  " not in line else
        f'<w:p><w:r><w:t>{line.split("  ")[0]}</w:t><w:tab/><w:t>{line.split("  ")[1]}</w:t></w:r></w:p>'
        for line in text.splitlines())
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{paras}</w:body></w:document>')
    return buf.getvalue()
