"""Fathom meetings into Trailmix (sources.py connects it): the transcript, and Fathom's notes as the summary.

Fathom's API (https://developers.fathom.ai): an API key from User Settings → API Access, sent as X-Api-Key. Meetings
are listed with /meetings (with their action items), then each recording's transcript and summary are fetched.
Those two are "heavy" calls, 30 a minute at most, so they're spaced out. Lines spoken by whoever recorded the
meeting become You, everyone else Them.
"""
import re
import time
from datetime import datetime, timedelta, UTC

import httpx

import database as db
import importer
import itertools

API = "https://api.fathom.ai/external/v1"
_HEAVY_GAP_S = 2.1      # transcript and summary calls: 30 a minute
_NOT_READY = timedelta(hours=2)  # notes missing this soon after the meeting: still being written, try later


def _get(key: str, path: str, params: dict | list | None = None) -> dict:
    try:
        r = httpx.get(f"{API}{path}", params=params, headers={"X-Api-Key": key}, timeout=30)
    except httpx.HTTPError as e:
        raise importer.ImportError_(f"Couldn't reach Fathom: {e}") from e
    if r.status_code == 401:
        raise importer.ImportError_("Fathom didn't accept that API key. Create one in Fathom → User Settings → "
                                    "API Access.")
    if r.status_code == 429:
        time.sleep(min(float(r.headers.get("Retry-After") or 10), 60))
        return _get(key, path, params)
    if r.status_code >= 400:
        raise importer.ImportError_(f"Fathom answered {r.status_code}: {r.text[:200]}")
    return r.json()


def notes(key: str, after: datetime | None) -> list[dict]:
    """Meetings recorded after `after` (or all), newest first: [{id, created_at, imported, title, when, recorded_by,
    action_items}]."""
    have = db.imported_ids("fathom")
    out, cursor = [], None
    while True:
        params = [("include_action_items", "true")]
        if after:
            params.append(("created_after", importer._iso(after)))
        if cursor:
            params.append(("cursor", cursor))
        data = _get(key, "/meetings", params)
        for m in data.get("items") or []:
            rid = str(m["recording_id"])
            out.append({"id": rid, "created_at": m["created_at"], "imported": rid in have,
                        "title": m.get("meeting_title") or m.get("title") or "",
                        "when": m.get("recording_start_time") or m.get("scheduled_start_time") or m["created_at"],
                        "ended": m.get("recording_end_time"),
                        "recorded_by": m.get("recorded_by") or {},
                        "action_items": [a.get("description") for a in m.get("action_items") or []
                                         if a.get("description")]})
        cursor = data.get("next_cursor")
        if not cursor:
            break
    out.sort(key=lambda n: n["created_at"], reverse=True)
    return out


def _seconds(stamp: str) -> float:
    h, m, s = (int(x) for x in stamp.split(":"))
    return h * 3600 + m * 60 + s


def segments(items: list[dict], recorded_by: dict) -> list[dict]:
    """Fathom transcript items -> Trailmix segments: each line runs until the next one starts."""
    me_email = (recorded_by.get("email") or "").lower()
    me_name = (recorded_by.get("name") or "").strip().lower()
    out = []
    for i in items:
        text = (i.get("text") or "").strip()
        if not text or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", i.get("timestamp") or ""):
            continue
        speaker = i.get("speaker") or {}
        mine = ((speaker.get("matched_calendar_invitee_email") or "").lower() == me_email and me_email) or \
            ((speaker.get("display_name") or "").strip().lower() == me_name and me_name)
        out.append({"start": _seconds(i["timestamp"]), "end": 0.0, "speaker": "You" if mine else "Them",
                    "text": text})
    for a, b in itertools.pairwise(out):
        a["end"] = max(a["start"], b["start"])
    if out:
        last = out[-1]
        last["end"] = last["start"] + max(1.0, len(last["text"].split()) / importer.WORDS_PER_SECOND)
    if not any(s["speaker"] == "You" for s in out):  # nobody matched the recorder: the labels say nothing
        for s in out:
            s["speaker"] = None
    return out


def summary(markdown: str, action_items: list[str]) -> str:
    """Fathom's notes, with its action items under the heading Trailmix reads tasks from."""
    text = markdown.strip()
    if action_items and not re.search(r"^#+\s*action items", text, re.I | re.M):
        text += "\n\n## Action Items\n" + "\n".join(f"- {a}" for a in action_items)
    return text


def import_note(key: str, note: dict, keep_summary: bool) -> int | None:
    """One Fathom meeting; returns the new meeting id (None: already here, no transcript yet, or its notes are
    still being written)."""
    with importer.import_lock:
        if note["id"] in db.imported_ids("fathom"):
            return None
        items = _get(key, f"/recordings/{note['id']}/transcript").get("transcript") or []
        segs = segments(items, note["recorded_by"])
        if not segs:
            return None
        text = ""
        if keep_summary:
            time.sleep(_HEAVY_GAP_S)
            text = ((_get(key, f"/recordings/{note['id']}/summary").get("summary") or {})
                    .get("markdown_formatted") or "")
            ended = importer._parse_time(note.get("ended")) or importer._parse_time(note["when"])
            if not text.strip() and ended and datetime.now(UTC) - ended < _NOT_READY:
                return None
        when = importer._parse_time(note["when"]) or datetime.now(UTC)
        notes_md = summary(text, note["action_items"]) if text.strip() else ""
        meeting_id = importer.save_imported(note["title"], when, segs, "fathom", note["id"], notes_md)
        time.sleep(_HEAVY_GAP_S)
        return meeting_id
