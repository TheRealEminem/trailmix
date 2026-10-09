"""Bringing meetings recorded elsewhere into Trailmix: from Granola (its API), or a pasted transcript.

Imported meetings have a transcript but no audio. They're searchable, can be asked about and summarized
like any other; Granola's own notes come along as the summary, or Trailmix writes one.
"""
import logging
import re
import threading
import time
from datetime import datetime, timezone

import httpx

import database as db
import pipeline


# Where an imported transcript (and notes) came from, as shown in "Transcribed with" and "Notes by".
SOURCE_NAMES = {"granola": "Granola", "paste": "a pasted transcript", "file": "a transcript file"}

log = logging.getLogger("trailmix")

GRANOLA_API = "https://public-api.granola.ai/v1"
_REQUEST_GAP_S = 0.25  # Granola allows 5 requests a second


class ImportError_(Exception):
    pass


# ── Turning a transcript into a meeting ─────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _line(s: dict) -> str:
    m, sec = divmod(int(s["start"]), 60)
    h, m = divmod(m, 60)
    stamp = f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
    return f"[{stamp}] {s['speaker'] + ': ' if s.get('speaker') else ''}{s['text']}"


def save_imported(title: str, when: datetime, segments: list[dict], source: str, external_id: str | None,
                  summary: str | None) -> int:
    """Creates the meeting. With a summary it's done; without one it goes to the summarizer like any meeting."""
    import meeting_text

    labeled = any(s.get("speaker") for s in segments)
    meeting_id = db.create_imported_meeting(title or "Imported meeting", _iso(when), source, external_id)
    duration = max((s["end"] for s in segments), default=0)
    db.update_meeting(
        meeting_id, transcript="\n".join(_line(s) for s in segments), segments_json=segments, transcribed=1,
        has_system=int(labeled), duration_sec=duration, title_auto=int(not title),
        summary=summary or None, summary_provider=source if summary else None,
        summary_model=SOURCE_NAMES.get(source, source) if summary else None,
        transcribed_with=SOURCE_NAMES.get(source, source),
        status="done" if summary else "queued",
    )
    if summary:
        db.replace_tasks(meeting_id, meeting_text.action_items(summary))
    else:
        pipeline.enqueue(meeting_id)
    return meeting_id


# ── Pasted text ─────────────────────────────────────────────────────────

_SPEAKER_LINE = re.compile(r"^\s*(?:\[?(\d{1,2}(?::\d{2}){1,2})\]?\s*)?([A-Za-z][\w .'-]{0,40}?)\s*:\s+(.+)$")
_TIME_ONLY = re.compile(r"^\s*\[?(\d{1,2}(?::\d{2}){1,2})\]?\s+(.+)$")
_ME = {"me", "you", "i", "myself"}
WORDS_PER_SECOND = 2.5  # for spacing out lines that carry no timestamps


def _seconds(stamp: str) -> float:
    parts = [int(p) for p in stamp.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_text(text: str, my_names: set[str] = frozenset()) -> list[dict]:
    """Segments from a transcript as text: "Me: …" / "Them: …" (Granola's Copy transcript), "Name: …", and
    optional [mm:ss] timestamps. "Me"/"You" (or your name) become You, anyone else Them. A line without a
    speaker continues the one before it (wrapped text); a transcript with no speakers at all stays
    unlabeled, one line per segment. Lines without times are spaced out by their length."""
    me = _ME | {n.lower() for n in my_names if n}
    segments: list[dict] = []
    clock = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        stamp, speaker, said = None, None, line
        found = _SPEAKER_LINE.match(line)
        if found and len(found.group(2).split()) <= 4:
            stamp, who, said = found.group(1), found.group(2).strip(), found.group(3).strip()
            speaker = "You" if who.lower() in me else "Them"
        elif (timed := _TIME_ONLY.match(line)):
            stamp, said = timed.group(1), timed.group(2).strip()
        if speaker is None and stamp is None and segments and segments[-1]["speaker"]:
            segments[-1]["text"] += " " + said  # wrapped text
            segments[-1]["end"] = round(segments[-1]["end"] + len(said.split()) / WORDS_PER_SECOND, 2)
            clock = segments[-1]["end"]
            continue
        start = _seconds(stamp) if stamp else clock
        end = start + max(1.0, len(said.split()) / WORDS_PER_SECOND)
        segments.append({"start": round(start, 2), "end": round(end, 2), "speaker": speaker, "text": said})
        clock = end
    return segments


# ── Granola ─────────────────────────────────────────────────────────────

def _granola(key: str, path: str, params: dict | None = None) -> httpx.Response:
    try:
        r = httpx.get(f"{GRANOLA_API}{path}", params=params, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    except httpx.HTTPError as e:
        raise ImportError_(f"Couldn't reach Granola: {e}") from e
    if r.status_code == 401:
        raise ImportError_("Granola didn't accept that API key. Create one in Granola → Settings → Connectors → API keys.")
    if r.status_code == 403:
        raise ImportError_("This Granola key can't read notes. Granola's API needs a Business or Enterprise plan, "
                           "and a key with note access.")
    if r.status_code == 429:
        time.sleep(2)
        return _granola(key, path, params)
    return r


def granola_notes(key: str) -> list[dict]:
    """Every note the key can see, newest first: [{id, title, created_at, imported}]."""
    have = db.imported_ids("granola")
    notes, cursor = [], None
    while True:
        params = {"page_size": 30, **({"cursor": cursor} if cursor else {})}
        r = _granola(key, "/notes", params)
        if r.status_code >= 400:
            raise ImportError_(f"Granola answered {r.status_code}: {r.text[:200]}")
        data = r.json()
        for n in data.get("notes", []):
            notes.append({"id": n["id"], "title": n.get("title") or "Untitled", "created_at": n["created_at"],
                          "imported": n["id"] in have, "meeting_id": have.get(n["id"])})
        cursor = data.get("cursor")
        if not data.get("hasMore") or not cursor:
            break
        time.sleep(_REQUEST_GAP_S)
    notes.sort(key=lambda n: n["created_at"], reverse=True)
    return notes


def _granola_transcript(key: str, note_id: str) -> list[dict]:
    items, cursor = [], None
    while True:
        r = _granola(key, f"/notes/{note_id}/transcript", {"page_size": 100, **({"cursor": cursor} if cursor else {})})
        if r.status_code >= 400:
            raise ImportError_(f"Granola answered {r.status_code} for a transcript")
        data = r.json()
        items += data.get("transcript", [])
        cursor = data.get("cursor")
        if not data.get("hasMore") or not cursor:
            return items
        time.sleep(_REQUEST_GAP_S)


def granola_segments(items: list[dict]) -> list[dict]:
    """Granola transcript items -> Trailmix segments (seconds from the first item; me -> You, them -> Them)."""
    starts = [t for t in (_parse_time(i.get("start_time")) for i in items) if t]
    zero = min(starts) if starts else None
    segments = []
    for i in items:
        text = (i.get("text") or "").strip()
        if not text:
            continue
        speaker = i.get("speaker") or {}
        who = speaker.get("attribution") or {"microphone": "me", "speaker": "them"}.get(speaker.get("source", ""))
        label = {"me": "You", "them": "Them"}.get(who)
        a, b = _parse_time(i.get("start_time")), _parse_time(i.get("end_time"))
        start = (a - zero).total_seconds() if a and zero else (segments[-1]["end"] if segments else 0.0)
        end = (b - zero).total_seconds() if b and zero else start + max(1.0, len(text.split()) / WORDS_PER_SECOND)
        segments.append({"start": round(start, 2), "end": round(max(end, start), 2), "speaker": label, "text": text})
    # iOS notes are all "microphone": with no "them" at all, the labels say nothing
    if not any(s["speaker"] == "Them" for s in segments):
        for s in segments:
            s["speaker"] = None
    segments.sort(key=lambda s: s["start"])
    return segments


def import_granola_note(key: str, note_id: str, keep_summary: bool) -> int | None:
    """Imports one note; returns the new meeting id (None if it was already imported or has no transcript)."""
    if note_id in db.imported_ids("granola"):
        return None
    r = _granola(key, f"/notes/{note_id}", {"include": "transcript"})
    if r.status_code == 413:  # transcript too large to include: fetch it in pages
        r = _granola(key, f"/notes/{note_id}")
        note = r.json() if r.status_code < 400 else None
        items = _granola_transcript(key, note_id) if note else []
    else:
        note = r.json() if r.status_code < 400 else None
        items = (note or {}).get("transcript") or []
    if not note:
        raise ImportError_(f"Granola answered {r.status_code} for note {note_id}")
    segments = granola_segments(items)
    if not segments:
        return None
    event = note.get("calendar_event") or {}
    when = _parse_time(event.get("scheduled_start_time")) or _parse_time(note.get("created_at")) or datetime.now(timezone.utc)
    summary = (note.get("summary_markdown") or note.get("summary_text") or "").strip() if keep_summary else ""
    return save_imported(note.get("title") or event.get("event_title") or "", when, segments, "granola", note_id, summary)


# A Granola import runs in the background; the app polls its progress.
_lock = threading.Lock()
_job: dict = {}


def job_status() -> dict:
    with _lock:
        return dict(_job)


def start_granola_import(key: str, note_ids: list[str], keep_summary: bool) -> bool:
    with _lock:
        if _job.get("active"):
            return False
        _job.clear()
        _job.update(active=True, total=len(note_ids), done=0, imported=0, skipped=0, errors=[], current=None)
    threading.Thread(target=_run_granola, args=(key, note_ids, keep_summary), daemon=True, name="granola-import").start()
    return True


def _run_granola(key: str, note_ids: list[str], keep_summary: bool) -> None:
    for note_id in note_ids:
        with _lock:
            _job["current"] = note_id
        try:
            made = import_granola_note(key, note_id, keep_summary)
            with _lock:
                _job["imported" if made else "skipped"] += 1
        except Exception as e:
            log.warning("Granola import of %s failed: %s", note_id, e)
            with _lock:
                _job["errors"].append(str(e)[:200])
            if isinstance(e, ImportError_) and "API key" in str(e):
                break
        with _lock:
            _job["done"] += 1
        time.sleep(_REQUEST_GAP_S)
    with _lock:
        _job.update(active=False, current=None)
