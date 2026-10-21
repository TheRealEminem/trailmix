"""What the law says about recording calls where you are: a short entry per US state and per country, shown
during setup and in Settings → Privacy. General information, not legal advice; following the law is up to you.

The table (assets/recording-laws.json) ships with the app, and a newer copy is fetched from the Trailmix site once
a day (docs/recording-laws.json in the repository), so an entry can be corrected without an app update. Nothing
here changes a setting: an entry may suggest keeping recordings for less time, and you choose whether to apply it.
"""
import json
import logging
import threading
import time
from pathlib import Path

import httpx

import database as db

log = logging.getLogger("trailmix.laws")
BUILT_IN = Path(__file__).parent / "assets" / "recording-laws.json"
REMOTE = "https://therealeminem.github.io/trailmix/recording-laws.json"
REFRESH_S = 24 * 3600
CONSENT = ("all", "one", "mixed", "unknown")

_lock = threading.Lock()
_table: dict | None = None
_fetched_at = 0.0


def _cache() -> Path:
    return db.DATA_DIR / "recording-laws.json"


def _stamp(table: dict) -> tuple:
    return table.get("version", 0), table.get("updated", "")


def valid(table) -> bool:
    """Is this a table the app can show? (A bad fetch must never replace a good one.)"""
    if not isinstance(table, dict) or not isinstance(table.get("regions"), list) or not table.get("updated"):
        return False
    if not isinstance(table.get("fallback"), dict) or not table["fallback"].get("summary"):
        return False
    ids = set()
    for r in table["regions"]:
        if not isinstance(r, dict) or not r.get("id") or r["id"] in ids or not r.get("name") or not r.get("summary"):
            return False
        if r.get("consent") not in CONSENT or not r.get("sources"):
            return False
        retention = r.get("retention")
        if retention is not None and not (isinstance(retention, dict) and isinstance(retention.get("suggest"), int)):
            return False
        ids.add(r["id"])
    return True


def table() -> dict:
    """The newest table we have: the built-in one, or a fetched one that's newer."""
    global _table
    with _lock:
        if _table is None:
            best = json.loads(BUILT_IN.read_text())
            try:
                fetched = json.loads(_cache().read_text())
                if valid(fetched) and _stamp(fetched) > _stamp(best):
                    best = fetched
            except (OSError, ValueError):
                pass
            _table = best
        return _table


def refresh() -> None:
    """Fetches the latest table from the Trailmix site (at most once a day; quietly does nothing offline)."""
    global _table, _fetched_at
    import settings

    if time.time() - _fetched_at < REFRESH_S or settings.locked():
        return
    _fetched_at = time.time()
    try:
        r = httpx.get(REMOTE, timeout=10)
        data = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return
    if not valid(data):
        return
    if _stamp(data) > _stamp(table()):
        _cache().write_text(json.dumps(data))
        with _lock:
            _table = data
        log.info("Recording laws updated to %s", data.get("updated"))


def entry(region: str) -> dict:
    """The entry for a region id ("US-CA", "GB", …), or the general one."""
    for r in table()["regions"]:
        if r["id"] == region:
            return r
    return table()["fallback"]
