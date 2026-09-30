"""Writes a finished meeting (summary and/or transcript) to a folder of your choice."""
import json
import os
import re
from datetime import datetime
from pathlib import Path

import database as db
import meeting_text


def _fmt(t: float) -> str:
    s = int(t)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _local_date(m: dict) -> datetime:
    return datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).astimezone()


def _transcript_lines(m: dict, cfg: dict, markdown: bool) -> list[str]:
    segs = meeting_text.segments(m)
    if not segs:
        return [m["transcript"]] if m.get("transcript") else []
    names = meeting_text.speaker_names(m, cfg)
    lines = []
    for s in segs:
        who = f" {names.get(s['speaker'], s['speaker'])}:" if s.get("speaker") else ""
        stamp = f"[{_fmt(s['start'])}]{who}"
        lines.append(f"**{stamp}** {s['text']}" if markdown else f"{stamp} {s['text']}")
    return lines


def _meta_line(m: dict) -> str:
    when = _local_date(m).strftime("%A, %B %-d, %Y at %-I:%M %p")
    return f"{when} · {_fmt(m['duration_sec'])}"


def build(m: dict, cfg: dict, fmt: str, include_summary: bool, include_transcript: bool, part: str) -> str:
    """part: 'all' | 'summary' | 'transcript' - which of the selected sections go in this file."""
    want_summary = include_summary and part in ("all", "summary") and bool(m.get("summary"))
    want_transcript = include_transcript and part in ("all", "transcript") and bool(m.get("transcript"))
    summary = meeting_text.summary_with_tasks(m["summary"], db.tasks_for(m["id"])) if want_summary else ""

    if fmt == "json":
        data = {"title": m["title"], "created_at": m["created_at"], "duration_sec": m["duration_sec"]}
        if want_summary:
            data["summary"] = summary
            data["action_items"] = db.tasks_for(m["id"])
        if want_transcript:
            names = meeting_text.speaker_names(m, cfg)
            segs = meeting_text.segments(m)
            data["transcript"] = [{**s, "speaker": names.get(s["speaker"], s["speaker"])} for s in segs] or m["transcript"]
            data["bookmarks"] = meeting_text.bookmarks(m)
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    md = fmt == "md"
    title = m["title"] if part in ("all", "summary") else f"{m['title']} - Transcript"
    out = [f"# {title}" if md else title.upper(), "", f"_{_meta_line(m)}_" if md else _meta_line(m), ""]
    if want_summary:
        # The summary already uses "## Overview" style headings, so it drops straight in.
        out += [summary.strip(), ""]
    if want_transcript:
        if want_summary or part == "all":
            out += ["## Transcript" if md else "TRANSCRIPT", ""]
        out += [line + ("\n" if md else "") for line in _transcript_lines(m, cfg, md)]
    return "\n".join(out).rstrip() + "\n"


def _safe_name(title: str) -> str:
    name = re.sub(r'[\\/*?"<>|\x00-\x1f]', "", title.replace(":", ".")).strip(" .")
    return name[:80] or "Meeting"


def export_meeting(m: dict, s: dict) -> list[str]:
    """Writes the meeting per the export settings and returns the file paths.
    Files this meeting exported earlier are replaced (e.g. after the title changed)."""
    include_summary, include_transcript = s["export_summary"], s["export_transcript"]
    if not (include_summary or include_transcript):
        return []
    fmt = s["export_format"]
    folder = Path(s["export_dir"]).expanduser()
    folder.mkdir(parents=True, exist_ok=True)

    previous = set(json.loads(m["exported_paths"])) if m.get("exported_paths") else set()
    base = f"{_local_date(m):%Y-%m-%d %H-%M} {_safe_name(m['title'])}"
    if s["export_separate_files"] and include_summary and include_transcript:
        jobs = [(f"{base} - summary.{fmt}", "summary"), (f"{base} - transcript.{fmt}", "transcript")]
    else:
        jobs = [(f"{base}.{fmt}", "all")]

    written = []
    for filename, part in jobs:
        path = folder / filename
        n = 2
        while path.exists() and str(path) not in previous:  # never overwrite a file we didn't write
            path = folder / f"{Path(filename).stem} ({n}){Path(filename).suffix}"
            n += 1
        text = build(m, s, fmt, include_summary, include_transcript, part)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)  # atomic: a sync tool or editor never sees a half-written file
        written.append(str(path))

    for old in previous - set(written):  # renamed/reformatted: remove only what we created before
        Path(old).unlink(missing_ok=True)
    return written
