"""Exports meetings to a folder you own: one folder per meeting.

    <export folder>/2026-10-04 09-00 Launch planning/
        Launch planning.pdf          the notes and transcript (PDF, Word, OpenDocument, Markdown, text: your pick)
        meeting.json                 everything, for re-importing into Trailmix (archive.py)
        Recording.ogg                the call, both sides       } when exporting audio, or for meetings set to
        Tracks/You.ogg, Them.ogg     each side on its own       } "Keep forever"

Audio is hard-linked, not copied: on the same disk the exported file and Trailmix's own are the same bytes
under two names, so exporting it takes no extra space, and when Trailmix's 30-day clean-up removes its name
the exported one carries on. (A different disk or a synced folder gets a real copy.)
"""
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

import archive
import audio_store as store
import database as db
import documents
import meeting_text

TRACK_NAMES = {"mic": "You", "system": "Them"}


def _local_date(m: dict) -> datetime:
    return datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).astimezone()


def _safe_name(title: str) -> str:
    name = re.sub(r'[\\/*?"<>|\x00-\x1f]', "", title.replace(":", ".")).strip(" .")
    return name[:80] or "Meeting"


def folder_name(m: dict) -> str:
    return f"{_local_date(m):%Y-%m-%d %H-%M} {_safe_name(m['title'])}"


def _transcript(m: dict, cfg: dict) -> list[dict]:
    names = meeting_text.speaker_names(m, cfg)
    return [{"time": documents.fmt_duration(t["start"]), "speaker": names.get(t["speaker"], t["speaker"]) if t.get("speaker") else "",
             "text": t["text"]} for t in meeting_text.turns(meeting_text.segments(m))]


def _speakers(m: dict, cfg: dict) -> str:
    if not m.get("has_system"):
        return ""
    names = meeting_text.speaker_names(m, cfg)
    return f"{names.get('You', 'You')} & {names.get('Them', 'Them')}"


def documents_for(m: dict, cfg: dict) -> dict[str, bytes]:
    """filename -> bytes for the notes document(s), per the export settings."""
    include_summary = cfg["export_summary"] and bool(m.get("summary"))
    include_transcript = cfg["export_transcript"] and bool(m.get("transcript"))
    if not (include_summary or include_transcript):
        return {}
    summary = meeting_text.summary_with_tasks(m["summary"], db.tasks_for(m["id"])) if include_summary else None
    transcript = _transcript(m, cfg) if include_transcript else None
    title = _safe_name(m["title"])
    when, length, who = _local_date(m), m.get("duration_sec") or 0, _speakers(m, cfg)
    if cfg["export_separate_files"] and include_summary and include_transcript:
        parts = [(f"{title} - Notes", "summary"), (f"{title} - Transcript", "transcript")]
    else:
        parts = [(title, "all")]
    out = {}
    for name, part in parts:
        doc = documents.meeting_doc(m["title"], when, length, who, summary, transcript, part)
        for fmt in cfg["export_formats"] or ["pdf"]:
            out[f"{name}.{fmt}"] = documents.render(doc, fmt)
    return out


def _link(source: Path, target: Path) -> None:
    """Hard link when possible (no extra space); a copy across disks or when the file system can't link."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_ino == source.stat().st_ino:
            return
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # atomic: a sync tool or an open viewer never sees a half-written file


def export_meeting(m: dict, cfg: dict, include_audio: bool | None = None) -> list[str]:
    """Writes the meeting's folder per the export settings and returns the files in it that Trailmix wrote.
    A folder this meeting exported earlier is renamed if the title changed, and files it no longer produces
    (a format you turned off) are removed; anything else in the folder is left alone."""
    root = Path(cfg["export_dir"]).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    previous = [Path(p) for p in json.loads(m["exported_paths"])] if m.get("exported_paths") else []
    old_folder = next((p.parent for p in previous if p.name == "meeting.json"), None)

    folder = root / folder_name(m)
    if old_folder and old_folder != folder and old_folder.exists() and not folder.exists():
        old_folder.rename(folder)  # title or date changed: move the whole folder along
        previous = [folder / p.relative_to(old_folder) if p.is_relative_to(old_folder) else p for p in previous]
    elif folder.exists() and not (old_folder and old_folder == folder):
        mine = folder / "meeting.json"
        if not (mine.exists() and json.loads(mine.read_text() or "{}").get("uid") == m.get("uid")):
            n = 2  # someone else's folder with this name: don't mix into it
            while (root / f"{folder_name(m)} ({n})").exists():
                n += 1
            folder = root / f"{folder_name(m)} ({n})"
    folder.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name, data in documents_for(m, cfg).items():
        _write(folder / name, data)
        written.append(folder / name)

    audio = None
    with_audio = cfg.get("export_audio") if include_audio is None else include_audio
    if (with_audio or m.get("keep_audio")) and store.audio_bytes(m):
        audio = {"tracks": {}}
        tracks = store.track_dir(m)
        mix = store.playback_path(m)
        if mix:
            _link(mix, folder / "Recording.ogg")
            written.append(folder / "Recording.ogg")
            audio["recording"] = "Recording.ogg"
        for track, label in TRACK_NAMES.items():
            source = store.ogg_path(tracks, track)
            if source.exists() and mix != source:
                _link(source, folder / "Tracks" / f"{label}.ogg")
                written.append(folder / "Tracks" / f"{label}.ogg")
                audio["tracks"][track] = f"Tracks/{label}.ogg"
    elif m.get("keep_audio") or cfg.get("export_audio"):
        # Trailmix's own copy may be gone, but an earlier export of it lives on: keep pointing at it.
        kept = {p.relative_to(folder).as_posix() for p in previous if p.suffix == ".ogg" and p.is_relative_to(folder) and p.exists()}
        if kept:
            audio = {"recording": "Recording.ogg" if "Recording.ogg" in kept else None,
                     "tracks": {t: f"Tracks/{label}.ogg" for t, label in TRACK_NAMES.items() if f"Tracks/{label}.ogg" in kept}}
            written += [folder / k for k in kept]

    _write(folder / "meeting.json", (json.dumps(archive.record(m, audio), indent=2, ensure_ascii=False) + "\n").encode())
    written.append(folder / "meeting.json")

    for old in set(previous) - set(written):  # only files we wrote before; never anything else
        if old.suffix != ".ogg" or not (m.get("keep_audio") or cfg.get("export_audio")):
            old.unlink(missing_ok=True)
    return [str(p) for p in written]


# ── Exporting everything at once ────────────────────────────────────────

_lock = threading.Lock()
_job: dict = {}


def job_status() -> dict:
    with _lock:
        return dict(_job)


def export_all(cfg: dict, include_audio: bool) -> bool:
    """Exports every transcribed meeting in the background. False if an export-all is already running."""
    with _lock:
        if _job.get("active"):
            return False
        ids = [i for i in db.all_meeting_ids() if (db.get_meeting(i) or {}).get("transcript")]
        _job.clear()
        _job.update(active=True, total=len(ids), done=0, errors=[], folder=str(Path(cfg["export_dir"]).expanduser()))
    threading.Thread(target=_run_all, args=(ids, cfg, include_audio), daemon=True, name="export-all").start()
    return True


def _run_all(ids: list[int], cfg: dict, include_audio: bool) -> None:
    for meeting_id in ids:
        m = db.get_meeting(meeting_id)
        try:
            paths = export_meeting(m, cfg, include_audio)
            db.update_meeting(meeting_id, exported_paths=json.dumps(paths), export_error=None)
        except Exception as e:
            with _lock:
                _job["errors"].append(f"{m['title']}: {e}"[:200])
            db.update_meeting(meeting_id, export_error=f"Export failed: {e}")
        with _lock:
            _job["done"] += 1
    with _lock:
        _job["active"] = False
