"""Other note takers whose new meetings come into Trailmix by themselves.

Each source can list its notes made after a time and import one by its id (importer.py does the importing). Turned
on in the Import panel, a source is asked every 15 minutes (main.py's sync loop) for notes made since the last time,
and brings in the ones that aren't here yet: from when you turned it on, or from the start you picked. Right after a
recording, every source with a key is asked a few times for its copy of that meeting, which then merges into the
recording (duplicates.py).

Nothing is fetched in Lockdown mode.
"""
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import database as db
import importer
import settings

log = logging.getLogger("trailmix")

# A note can show up a while after it was made (Granola lists a note once its summary is written, after the
# meeting), so each sync looks back this far before the last one that got through.
LOOKBACK = timedelta(days=1)
# A recording's copy elsewhere was started within this long of it.
NEARBY_S = 1800


@dataclass(frozen=True)
class Source:
    name: str
    label: str
    key_setting: str
    notes: Callable[[str, datetime | None], list[dict]]   # (key, made after) -> [{id, created_at, imported, …}]
    import_note: Callable[[str, dict, bool], int | None]  # (key, a note from notes(), keep their notes) -> meeting id
    # For connecting it in the Import page: where to find a key, what it looks like, and what plan it needs
    steps: tuple[str, ...] = ()
    placeholder: str = ""
    note: str = ""


def _sources() -> dict[str, Source]:
    import fathom
    import fireflies

    return {
        "granola": Source(
            "granola", "Granola", "granola_api_key",
            lambda key, after: importer.granola_notes(key, after),
            lambda key, note, keep: importer.import_granola_note(key, note["id"], keep),
            steps=("In Granola, open Settings → Connectors → API keys.",
                   "Click Create new key, give it access to your notes, and click Generate API Key.",
                   "Copy the key (it starts with grn_) and paste it here."),
            placeholder="grn_…",
            note="Granola's API needs a Business or Enterprise plan. On other plans, copy a meeting's transcript in "
                 "Granola and paste it under Import once."),
        "fathom": Source(
            "fathom", "Fathom", "fathom_api_key", fathom.notes, fathom.import_note,
            steps=("In Fathom, open User Settings → API Access.",
                   "Click Generate API Key, then copy it and paste it here."),
            placeholder="Fathom API key",
            note="Brings in the meetings you recorded, with Fathom's notes and next steps."),
        "fireflies": Source(
            "fireflies", "Fireflies", "fireflies_api_key", fireflies.notes, fireflies.import_note,
            steps=("Sign in at app.fireflies.ai and open Integrations.",
                   "Choose Fireflies API, then copy your API key and paste it here."),
            placeholder="Fireflies API key",
            note="Fireflies' free plan allows 500 API requests a day: enough for checking every 15 minutes and "
                 "bringing in a few meetings a day."),
    }


SOURCES = _sources()

STARTS = {"now": timedelta(0), "week": timedelta(days=7), "all": None}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Sync ────────────────────────────────────────────────────────────────

_running: set[str] = set()
_running_lock = threading.Lock()


def sync(name: str, cfg: dict | None = None) -> int:
    """Brings in the source's notes made since the last sync (and not here yet). Returns how many came in; how it
    went is kept in source_sync for the Import panel."""
    cfg = cfg or settings.get_all()
    src = SOURCES[name]
    key = cfg[src.key_setting]
    if not key or settings.locked(cfg):
        return 0
    with _running_lock:
        if name in _running:
            return 0
        _running.add(name)
    try:
        return _sync(src, key, cfg)
    finally:
        with _running_lock:
            _running.discard(name)


def _sync(src: Source, key: str, cfg: dict) -> int:
    started = _now()
    state = db.source_sync(src.name)
    if not state:  # turned on without picking a start: from now on
        db.set_source_sync(src.name, since=importer._iso(started))
        state = db.source_sync(src.name)
    since = importer._parse_time(state["since"])
    last_ok = importer._parse_time(state["last_ok"])
    after = max(filter(None, (since, last_ok - LOOKBACK if last_ok else None)), default=None)
    try:
        notes = src.notes(key, after)
    except Exception as e:
        log.info("Syncing %s: %s", src.label, e)
        db.set_source_sync(src.name, last_try=importer._iso(started), error=str(e)[:300])
        return 0
    made, problem = 0, None
    for note in notes:
        when = importer._parse_time(note["created_at"])
        if note["imported"] or (since and when and when < since):
            continue
        try:
            if src.import_note(key, note, cfg["auto_import_keep_notes"]):
                made += 1
        except Exception as e:  # this one again next time; the rest carry on
            log.warning("Importing %s note %s: %s", src.label, note["id"], e)
            problem = problem or str(e)[:300]
        time.sleep(importer._REQUEST_GAP_S)
    db.set_source_sync(src.name, last_ok=importer._iso(started), last_try=importer._iso(started), last_count=made,
                       total=(state["total"] or 0) + made, error=problem)
    return made


def sync_all() -> None:
    """The sync loop's round: every source that's turned on."""
    cfg = settings.get_all()
    if settings.locked(cfg):
        return
    for name in cfg["auto_import"]:
        if name in SOURCES:
            sync(name, cfg)


def sync_soon(name: str) -> None:
    threading.Thread(target=sync, args=(name,), daemon=True, name=f"sync-{name}").start()


def set_auto(name: str, on: bool, start: str = "now") -> None:
    """Turns a source's automatic import on (bringing in notes from `start`: now, a week back, or all) or off."""
    cfg = settings.get_all()
    chosen = [n for n in cfg["auto_import"] if n != name] + ([name] if on else [])
    settings.update({"auto_import": chosen})
    if on:
        back = STARTS[start]
        db.set_source_sync(name, since=importer._iso(_now() - back) if back is not None else None,
                           last_ok=None, error=None)


def check(name: str, key: str) -> None:
    """Asks the source for its newest notes, to see the key works (raises importer.ImportError_ if not)."""
    SOURCES[name].notes(key, _now())


def disconnect(name: str) -> None:
    """Off, and its key forgotten. The meetings it brought in stay."""
    set_auto(name, False)
    settings.update({SOURCES[name].key_setting: ""})


def status() -> list[dict]:
    cfg = settings.get_all()
    out = []
    for src in SOURCES.values():
        state = db.source_sync(src.name) or {}
        with _running_lock:
            running = src.name in _running
        out.append({"name": src.name, "label": src.label, "has_key": bool(cfg[src.key_setting]),
                    "auto": src.name in cfg["auto_import"], "running": running,
                    **{k: state.get(k) for k in ("since", "last_ok", "last_try", "last_count", "total", "error")}})
    return out


# ── After a recording ───────────────────────────────────────────────────

def pull_for(name: str, meeting_id: int, key: str) -> int:
    """Brings in the source's notes made at the same time as this recording (it was taking notes too), which then
    merge into it if they're the same meeting. Returns how many came in."""
    src = SOURCES[name]
    m = db.get_meeting(meeting_id)
    start = importer._parse_time(m["created_at"]) if m else None
    if not start:
        return 0
    end = start.timestamp() + (m["duration_sec"] or 0)
    made = 0
    for note in src.notes(key, start - timedelta(seconds=NEARBY_S)):
        when = importer._parse_time(note["created_at"])
        if note["imported"] or not when or not (start.timestamp() - NEARBY_S <= when.timestamp() <= end + NEARBY_S):
            continue
        if src.import_note(key, note, True):
            made += 1
        time.sleep(importer._REQUEST_GAP_S)
    return made


def any_key(cfg: dict) -> bool:
    return any(cfg[src.key_setting] for src in SOURCES.values())


def pull_soon(meeting_id: int, waits: tuple[int, ...] = (180, 900, 2700)) -> None:
    """After a recording: asks each source with a key for its notes on it a few times (they finish their notes a
    little after the meeting does), until some have come in."""
    def run():
        for wait in waits:
            time.sleep(wait)
            cfg = settings.get_all()
            if settings.locked(cfg) or cfg.get("duplicates") == "keep":
                return
            made = 0
            for src in SOURCES.values():
                if not cfg[src.key_setting]:
                    continue
                try:
                    made += pull_for(src.name, meeting_id, cfg[src.key_setting])
                except Exception as e:  # offline, the service down: try again later
                    log.info("Looking for %s's notes on meeting %s: %s", src.label, meeting_id, e)
            if made:
                return
    threading.Thread(target=run, daemon=True, name=f"sources-pull-{meeting_id}").start()


# ── Everything that brings meetings in by itself ────────────────────────

def connections() -> dict:
    """Services with a key and watched folders as one list, each the same shape for the Import page, and what could
    be connected: services without a key, and folders other meeting apps save into found on this Mac."""
    import watched

    rows = []
    for s in status():
        if s["has_key"]:
            rows.append({"id": s["name"], "kind": "service", "label": s["label"], "path": None, "auto": s["auto"],
                         "running": s["running"], "last": s["last_try"] or s["last_ok"], "last_count": s["last_count"],
                         "total": s["total"] or 0, "waiting": 0, "failed": 0, "error": s["error"], "since": s["since"]})
    folders = watched.status()
    for f in folders["folders"]:
        rows.append({"id": f"folder:{f['path']}", "kind": "folder", "label": watched.label_for(f["path"]),
                     "path": f["path"], "auto": True, "running": False, "last": f["last_scan"],
                     "last_count": f["last_count"], "total": f["imported"], "waiting": f["waiting"],
                     "failed": f["failed"], "error": f["error"], "since": None})
    services = [{"name": s["name"], "label": s["label"], "steps": list(SOURCES[s["name"]].steps),
                 "placeholder": SOURCES[s["name"]].placeholder, "note": SOURCES[s["name"]].note}
                for s in status() if not s["has_key"]]
    return {"connections": rows, "available": {"services": services, "folders": folders["suggestions"]}}
