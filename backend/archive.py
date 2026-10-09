"""meeting.json: everything about one meeting, in the export folder. It's the "Trailmix export" format, so
importing it rebuilds the meeting exactly (notes, transcript, tasks and their ticks, flags, names, Q&A, and
the recording when it's alongside).

    {"format": "trailmix-meeting", "version": 1, "uid": …, "title": …, "created_at": …, "duration_sec": …,
     "speakers": {"You": "Mark", "Them": "Dana"}, "summary": "## Overview…", "tasks": [{"text", "done"}],
     "bookmarks": [{"t", "note"}], "qa": […], "transcript": [{"start", "end", "speaker", "text"}],
     "audio": {"recording": "Recording.ogg", "tracks": {"mic": "Tracks/You.ogg", "system": "Tracks/Them.ogg"}}}
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import database as db
import meeting_text

FORMAT = "trailmix-meeting"
VERSION = 1
APP_VERSION = next((p.read_text().strip() for p in (Path(__file__).parent / "VERSION", Path(__file__).parent.parent / "VERSION")
                    if p.exists()), "dev")


def record(m: dict, audio: dict | None) -> dict:
    """The meeting.json content for a meeting (audio: the relative paths exported next to it, or None)."""
    return {
        "format": FORMAT,
        "version": VERSION,
        "uid": m.get("uid"),
        "title": m["title"],
        "title_auto": bool(m.get("title_auto")),
        "created_at": m["created_at"],
        "duration_sec": m.get("duration_sec") or 0,
        "source": m.get("source") or "trailmix",
        "has_system": bool(m.get("has_system")),
        "speakers": json.loads(m["speaker_names_json"]) if m.get("speaker_names_json") else {},
        "summary": m.get("summary"),
        "summary_provider": m.get("summary_provider"),
        "template": m.get("requested_template"),
        "tasks": [{"text": t["text"], "done": bool(t["done"])} for t in db.tasks_for(m["id"])],
        "bookmarks": meeting_text.bookmarks(m),
        "qa": json.loads(m["qa_json"]) if m.get("qa_json") else [],
        "transcript": meeting_text.segments(m),
        "audio": audio,
        "keep_audio": bool(m.get("keep_audio")),
        "workspace": (db.get_workspace(m["workspace_id"]) or {}).get("name") if m.get("workspace_id") else None,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": APP_VERSION,
    }


def looks_like_record(data: object) -> bool:
    return isinstance(data, dict) and data.get("format") == FORMAT and isinstance(data.get("transcript"), list)


def restore(data: dict, folder: Path | None) -> int | None:
    """Rebuilds a meeting from meeting.json (and the audio files next to it, if any). Returns the new
    meeting id, or None if a meeting with this uid is already here."""
    if data.get("version", 1) > VERSION:
        raise ValueError(f"This export is from a newer Trailmix ({data.get('app_version')}); update Trailmix first")
    uid = data.get("uid")
    if uid and db.meeting_by_uid(uid):
        return None
    segments = [{k: s.get(k) for k in ("start", "end", "speaker", "text")} for s in data.get("transcript", []) if s.get("text")]
    lines = []
    for s in segments:
        m, sec = divmod(int(s["start"] or 0), 60)
        h, m = divmod(m, 60)
        stamp = f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
        lines.append(f"[{stamp}] {s['speaker'] + ': ' if s.get('speaker') else ''}{s['text']}")
    meeting_id = db.create_imported_meeting(data.get("title") or "Imported meeting", data["created_at"],
                                            data.get("source") or "trailmix", uid, uid)
    db.update_meeting(
        meeting_id, transcript="\n".join(lines) or None, segments_json=segments, transcribed=1,
        has_system=int(bool(data.get("has_system"))), duration_sec=float(data.get("duration_sec") or 0),
        title_auto=int(bool(data.get("title_auto"))), summary=data.get("summary") or None,
        summary_provider=data.get("summary_provider"), requested_template=data.get("template"),
        speaker_names_json=data.get("speakers") or None, bookmarks_json=data.get("bookmarks") or None,
        qa_json=data.get("qa") or None, status="done" if data.get("summary") else "ready_summarize",
    )
    if data.get("workspace"):  # the same workspace here, made if it doesn't exist yet
        space = db.workspace_by_name(data["workspace"])
        db.update_meeting(meeting_id, workspace_id=space["id"] if space else db.create_workspace(data["workspace"]))
    tasks = data.get("tasks") or []
    db.replace_tasks(meeting_id, [t["text"] for t in tasks])
    for i, t in enumerate(db.tasks_for(meeting_id)):
        if i < len(tasks) and tasks[i].get("done"):
            db.set_task_done(t["id"], True)
    if folder:
        _restore_audio(meeting_id, data.get("audio") or {}, folder)
    return meeting_id


def _restore_audio(meeting_id: int, audio: dict, folder: Path) -> None:
    """Copies the exported recording back into Trailmix: the separate tracks if they're there (so the meeting
    can be transcribed again), else the mixed recording."""
    tracks = {name: folder / rel for name, rel in (audio.get("tracks") or {}).items() if (folder / rel).is_file()}
    if not tracks and audio.get("recording") and (folder / audio["recording"]).is_file():
        tracks = {"mic": folder / audio["recording"]}
    if not tracks:
        return
    target = db.AUDIO_DIR / str(meeting_id)
    target.mkdir(parents=True, exist_ok=True)
    for name, path in tracks.items():
        if name in ("mic", "system"):
            shutil.copy2(path, target / f"{name}.ogg")
    # It came from your archive, which you chose to keep: don't let the 30-day clean-up (counted from the
    # meeting's original date) delete it straight away.
    db.update_meeting(meeting_id, audio_dir=str(target), audio_deleted=0, keep_audio=1)
