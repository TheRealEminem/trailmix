"""Fireflies meetings into Trailmix (sources.py connects it): the transcript, and Fireflies' notes as the summary.

Fireflies' API (https://docs.fireflies.ai) is GraphQL at one address, with the API key from Integrations →
Fireflies API sent as a bearer token. One small query lists the meetings since a time; another fetches one with its
sentences and summary. The free plan allows 500 requests a day, so the list asks for little and is paged 50 at a
time. Lines spoken by the key's owner become You, everyone else Them.
"""
import re
import time
from datetime import datetime, timedelta, UTC

import httpx

import database as db
import importer

API = "https://api.fireflies.ai/graphql"
_PAGE = 50  # the most Fireflies returns at once
_NOT_READY = timedelta(hours=2)

_LIST = """query Transcripts($fromDate: DateTime, $limit: Int, $skip: Int) {
  transcripts(fromDate: $fromDate, limit: $limit, skip: $skip) { id title dateString }
}"""
_ONE = """query Transcript($id: String!) {
  transcript(id: $id) {
    id title dateString duration
    user { email name }
    sentences { speaker_name text start_time end_time }
    summary { overview action_items }
    meeting_info { summary_status }
  }
}"""


def _ask(key: str, query: str, variables: dict) -> dict:
    try:
        r = httpx.post(API, json={"query": query, "variables": variables},
                       headers={"Authorization": f"Bearer {key}"}, timeout=60)
    except httpx.HTTPError as e:
        raise importer.ImportError_(f"Couldn't reach Fireflies: {e}") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    errors = body.get("errors") or []
    code = (errors[0].get("code") or (errors[0].get("extensions") or {}).get("code") or "") if errors else ""
    if r.status_code == 401 or code in ("auth_failed", "unauthenticated", "invalid_api_key"):
        raise importer.ImportError_("Fireflies didn't accept that API key. Find yours in Fireflies → Integrations → "
                                    "Fireflies API.")
    if r.status_code == 429 or code == "too_many_requests":
        wait = ((errors[0].get("extensions") or {}).get("metadata") or {}).get("retryAfter") if errors else None
        if wait and float(wait) > 120:  # the day's allowance is used up: next time
            raise importer.ImportError_("Fireflies' API allowance for today is used up; Trailmix tries again later.")
        time.sleep(min(float(wait or r.headers.get("Retry-After") or 10), 120))
        return _ask(key, query, variables)
    if code == "paid_required":
        raise importer.ImportError_("Fireflies needs a paid plan for this.")
    if errors:
        raise importer.ImportError_(f"Fireflies answered: {errors[0].get('message', code)[:200]}")
    if r.status_code >= 400:
        raise importer.ImportError_(f"Fireflies answered {r.status_code}: {r.text[:200]}")
    return body.get("data") or {}


def notes(key: str, after: datetime | None) -> list[dict]:
    """Meetings made after `after` (or all), newest first: [{id, created_at, imported, title}]."""
    have = db.imported_ids("fireflies")
    out, skip = [], 0
    while True:
        variables = {"limit": _PAGE, "skip": skip, **({"fromDate": importer._iso(after)} if after else {})}
        page = _ask(key, _LIST, variables).get("transcripts") or []
        for t in page:
            out.append({"id": t["id"], "created_at": t.get("dateString") or "", "imported": t["id"] in have,
                        "title": t.get("title") or ""})
        if len(page) < _PAGE:
            break
        skip += _PAGE
    out.sort(key=lambda n: n["created_at"], reverse=True)
    return out


def _time(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def segments(sentences: list[dict], me: str, duration_min: float | None) -> list[dict]:
    """Fireflies sentences -> Trailmix segments, the same speaker's sentences joined into turns."""
    times = [(_time(s.get("start_time")), _time(s.get("end_time"))) for s in sentences]
    longest = max((b for _, b in times), default=0)
    # Seconds, but in milliseconds if they run far past the meeting's length
    scale = 1000.0 if longest > max((duration_min or 0) * 60 * 3, 6 * 3600) else 1.0
    me = me.strip().lower()
    out = []
    for s, (a, b) in zip(sentences, times, strict=True):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        who = (s.get("speaker_name") or "").strip()
        speaker = "You" if me and who.lower() == me else "Them" if who else None
        start, end = round(a / scale, 2), round(max(a, b) / scale, 2)
        last = out[-1] if out else None
        if last and last["speaker"] == speaker and start - last["end"] < 2 and len(last["text"]) < 600:
            last["text"] += " " + text
            last["end"] = end
        else:
            out.append({"start": start, "end": end, "speaker": speaker, "text": text})
    if not any(s["speaker"] == "You" for s in out):
        for s in out:
            s["speaker"] = None
    return out


def summary(overview: str, action_items: str) -> str:
    """Fireflies' overview and action items as Trailmix notes. Its action items come grouped under each person's
    name in bold ("**Dana**", then her items); each becomes "- Dana: item"."""
    parts = []
    if overview.strip():
        parts.append("## Overview\n" + overview.strip())
    items, who = [], None
    for line in action_items.splitlines():
        line = line.strip()
        if not line:
            continue
        if name := re.fullmatch(r"\*\*(.+?)\*\*:?", line):
            who = name.group(1).strip()
            continue
        line = re.sub(r"^[-*•]\s*", "", line)
        items.append(f"- {who}: {line}" if who else f"- {line}")
    if items:
        parts.append("## Action Items\n" + "\n".join(items))
    return "\n\n".join(parts)


def import_note(key: str, note: dict, keep_summary: bool) -> int | None:
    """One Fireflies meeting; returns the new meeting id (None: already here, no transcript, or its notes are still
    being written)."""
    with importer.import_lock:
        if note["id"] in db.imported_ids("fireflies"):
            return None
        t = _ask(key, _ONE, {"id": note["id"]}).get("transcript")
        if not t:
            return None
        segs = segments(t.get("sentences") or [], (t.get("user") or {}).get("name") or "", t.get("duration"))
        if not segs:
            return None
        when = importer._parse_time(t.get("dateString")) or datetime.now(UTC)
        text = ""
        if keep_summary:
            status = (t.get("meeting_info") or {}).get("summary_status")
            s = t.get("summary") or {}
            text = summary(s.get("overview") or "", s.get("action_items") or "")
            if status == "processing" or (not text and datetime.now(UTC) - when < _NOT_READY):
                return None
        return importer.save_imported(t.get("title") or note.get("title") or "", when, segs, "fireflies",
                                      note["id"], text)
