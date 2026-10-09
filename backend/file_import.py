"""Importing files: recordings (audio or video from anywhere) and transcripts exported by other meeting apps.

Recordings: the file is streamed to a temporary copy, its audio converted with ffmpeg to what Trailmix keeps
(16 kHz mono Opus), and the copy deleted; your original is never touched. It's then transcribed and summarized
like any meeting, and its audio kept for the usual 30 days from the import (or forever, if you choose).

Transcripts: WebVTT and SRT (what Zoom, Microsoft Teams and Google Meet export), Otter's TXT and DOCX layout
("Name  0:05" then what they said), and plain text in the formats the paste importer reads.
"""
import logging
import re
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import audio_store as store
import database as db
import importer
import pipeline

log = logging.getLogger("trailmix.import")

MEDIA = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".oga", ".webm", ".mp4", ".mov", ".mkv", ".m4v",
         ".avi", ".wma", ".aif", ".aiff", ".caf", ".amr", ".3gp", ".mpeg", ".mpg"}
TRANSCRIPTS = {".vtt", ".srt", ".txt", ".docx", ".md"}
SUPPORTED = MEDIA | TRANSCRIPTS


def kind(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    return "recording" if ext in MEDIA else "transcript" if ext in TRANSCRIPTS else None


def title_from(name: str) -> str:
    stem = re.sub(r"[_]+", " ", Path(name).stem).strip()
    return stem[:120] or "Imported meeting"


# ── Transcripts ─────────────────────────────────────────────────────────

_CUE_TIME = re.compile(r"(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_VOICE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
_OTTER_HEADER = re.compile(r"^(.{1,60}?)\s{2,}(\d{1,2}:\d{2}(?::\d{2})?)\s*$")


def _cue_seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int((h or "0:")[:-1]) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _speaker(who: str | None, my_names: set[str]) -> str | None:
    if not who:
        return None
    mine = importer._ME | {n.lower() for n in my_names if n}
    return "You" if who.strip().lower() in mine else "Them"


def parse_cues(text: str, my_names: set[str]) -> list[dict]:
    """WebVTT or SRT: timed cues, the speaker as <v Name> or a "Name:" prefix."""
    segments = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("﻿", ""))
    for block in blocks:
        lines = [ln.strip() for ln in block.strip().split("\n") if ln.strip()]
        timing = next((i for i, ln in enumerate(lines) if _CUE_TIME.search(ln)), None)
        if timing is None:
            continue
        t = _CUE_TIME.search(lines[timing]).groups()
        start, end = _cue_seconds(*t[:4]), _cue_seconds(*t[4:])
        said = " ".join(lines[timing + 1:])
        who = None
        if voice := _VOICE.search(said):
            who = voice.group(1).strip()
        said = re.sub(r"<[^>]+>", "", said).strip()
        if who is None and (named := re.match(r"^([^:]{1,40}):\s+(.+)$", said)) and len(named.group(1).split()) <= 4:
            who, said = named.group(1).strip(), named.group(2).strip()
        if not said:
            continue
        speaker = _speaker(who, my_names)
        last = segments[-1] if segments else None
        if last and last["speaker"] == speaker and start - last["end"] < 1.0 and len(last["text"]) < 400:
            last["text"] += " " + said  # captions split sentences across cues: join them back
            last["end"] = round(end, 2)
        else:
            segments.append({"start": round(start, 2), "end": round(end, 2), "speaker": speaker, "text": said})
    return segments


def _otter(text: str) -> str:
    """Otter's layout ("Name  0:05" on its own line, then the words) into the "[0:05] Name: words" form."""
    lines = text.splitlines()
    if not any(_OTTER_HEADER.match(ln.strip()) for ln in lines):
        return text
    out, current = [], None
    for raw in lines:
        line = raw.strip()
        if header := _OTTER_HEADER.match(line):
            current = f"[{header.group(2)}] {header.group(1).strip()}:"
        elif line and current:
            out.append(f"{current} {line}")
            current = None
        elif line:
            out.append(line)
    return "\n".join(out)


def _docx_text(data: bytes) -> str:
    """A Word document's paragraphs as lines (a tab becomes two spaces, as in Otter's "Name<tab>0:05")."""
    with zipfile.ZipFile(BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for p in root.iter(f"{w}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{w}t":
                parts.append(node.text or "")
            elif node.tag == f"{w}tab":
                parts.append("  ")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def parse_transcript(name: str, data: bytes, my_names: set[str]) -> list[dict]:
    ext = Path(name).suffix.lower()
    if ext == ".docx":
        text = _docx_text(data)
    else:
        text = data.decode("utf-8", errors="replace")
    if ext in (".vtt", ".srt") or _CUE_TIME.search(text[:2000]):
        return parse_cues(text, my_names)
    return importer.parse_text(_otter(text), my_names)


def import_transcript(name: str, data: bytes, when: datetime, my_names: set[str]) -> int | None:
    """Creates the meeting (then summarized like any other). None if this file was imported before."""
    external = f"{name}:{len(data)}"
    if external in db.imported_ids("file"):
        return None
    segments = parse_transcript(name, data, my_names)
    if not segments:
        raise ValueError(f"{name}: no transcript found in it")
    return importer.save_imported(title_from(name), when, segments, "file", external, None)


# ── Recordings ──────────────────────────────────────────────────────────

def _probe(path: Path) -> tuple[float, datetime | None]:
    """(duration in seconds, when it was recorded if the file says)."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    duration = 0.0
    if d := re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out):
        duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    made = None
    if c := re.search(r"creation_time\s*:\s*(\S+)", out):
        try:
            made = datetime.fromisoformat(c.group(1).replace("Z", "+00:00"))
            if made.year < 2000:  # some cameras write 1970 or 1904 when the clock was never set
                made = None
        except ValueError:
            made = None
    return duration, made


def import_recording(name: str, temp: Path, modified: datetime) -> int | None:
    """Turns an uploaded recording (a temporary copy, deleted afterwards) into a meeting that's queued for
    transcription. None if this file was imported before."""
    try:
        size = temp.stat().st_size
        external = f"{name}:{size}"
        if external in db.imported_ids("file"):
            return None
        duration, recorded = _probe(temp)
        if duration <= 0:
            raise ValueError(f"{name}: no audio found in it")
        when = recorded or modified
        meeting_id = db.create_imported_meeting(title_from(name), importer._iso(when), "file", external)
        folder = db.AUDIO_DIR / str(meeting_id)
        folder.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-nostdin", "-y", "-i", str(temp), "-vn", "-ac", "1",
             "-ar", str(store.SAMPLE_RATE), "-c:a", "libopus", "-b:a", store.OPUS_BITRATE,
             str(store.ogg_path(folder, "mic"))], capture_output=True, text=True)
        if result.returncode != 0:
            db.delete_meeting(meeting_id)
            raise ValueError(f"{name}: couldn't read its audio ({result.stderr.strip()[:160]})")
        db.update_meeting(
            meeting_id, audio_dir=str(folder), audio_deleted=0, duration_sec=duration, title_auto=1,
            imported_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), status="queued",
            transcribed=0,
        )
        pipeline.enqueue(meeting_id)
        return meeting_id
    finally:
        temp.unlink(missing_ok=True)


def temp_path(name: str) -> Path:
    folder = db.DATA_DIR / "incoming"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"


def clear_leftovers() -> None:
    """Temporary copies left by an import that was cut short (the server stopped mid-way)."""
    folder = db.DATA_DIR / "incoming"
    if folder.is_dir():
        for f in folder.iterdir():
            f.unlink(missing_ok=True)
