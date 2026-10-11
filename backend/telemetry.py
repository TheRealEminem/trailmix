"""Anonymous usage stats, so Trailmix can support the computers people actually use it on.

On by default, off with one switch: the box on the first screen (StatsNotice) or Settings → Privacy. Nothing
is sent until you've seen that box. What is sent, and what never is, is in the privacy policy
(docs/privacy.html), and Settings shows the latest events exactly as they went out.

Sent: the app version; the computer (system and version, chip, memory and disk space, rounded); which
features are on and which AI writes the notes; per meeting, how long it was and how long processing took; and
which kinds of error happened (the error's type and stage, never its message, which can contain names or
paths). Never: anything from a meeting (titles, notes, transcripts, names, workspaces, recordings), your name,
or file paths.

Events go to PostHog as anonymous events: a random install ID (a new one if you turn stats off and on again),
no person profile, no location lookup; the PostHog project is set to discard IP addresses. Sending happens
in the background and never holds anything up; if it fails, the event is dropped.
"""
import logging
import os
import platform
import shutil
import sys
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import psutil

import archive
import database as db
import llm_engine
import resources
import settings

log = logging.getLogger("trailmix.telemetry")

# PostHog project API key: public by design (it can only send events, not read them). US region; the project
# discards IP addresses.
KEY = os.getenv("TRAILMIX_POSTHOG_KEY", "phc_prvN9BcvAcMJUZrU92e37P2DLfgMzDvZu8SDCvUSkSmp")
HOST = os.getenv("TRAILMIX_POSTHOG_HOST", "https://us.i.posthog.com")  # tests point it elsewhere
GB = 1024**3

_sent: deque = deque(maxlen=12)  # the latest events as sent, for Settings → Privacy → "See what's sent"
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telemetry")


def available() -> bool:
    return bool(KEY)


def enabled(cfg: dict) -> bool:
    return available() and bool(cfg["share_stats"]) and bool(cfg["stats_notice_seen"]) and not cfg.get("lockdown")


def _install_id(cfg: dict) -> str:
    if not cfg["install_id"]:
        cfg = settings.update({"install_id": uuid.uuid4().hex})
    return cfg["install_id"]


def _os_version() -> str:
    if sys.platform == "darwin":
        return platform.mac_ver()[0]
    return platform.release()


def _base() -> dict:
    return {"app_version": archive.APP_VERSION, "os": sys.platform, "os_version": _os_version(),
            "arch": platform.machine()}


def send(event: str, props: dict | None = None, cfg: dict | None = None) -> bool:
    """Queues one event (in the background). False if stats are off or not set up in this build."""
    cfg = cfg or settings.get_all()
    if not enabled(cfg):
        return False
    properties = {**_base(), **(props or {}), "$process_person_profile": False, "$geoip_disable": True,
                  "$lib": "trailmix"}
    body = {"api_key": KEY, "event": event, "distinct_id": _install_id(cfg),
            "timestamp": datetime.now(timezone.utc).isoformat(), "properties": properties}
    _sent.append({"event": event, "at": body["timestamp"], "properties": properties})
    _pool.submit(_post, body)
    return True


def _post(body: dict) -> None:
    try:
        httpx.post(f"{HOST}/i/v0/e/", json=body, timeout=10)
    except httpx.HTTPError as e:
        log.info("Usage stats not sent (%s)", e)


def recent() -> list[dict]:
    return list(_sent)


# ── What gets described ─────────────────────────────────────────────────

def _bucket(n: int) -> str:
    """Counts as ranges, so they describe how Trailmix is used without pinning anyone down."""
    for top, label in ((0, "0"), (5, "1-5"), (20, "6-20"), (50, "21-50"), (200, "51-200")):
        if n <= top:
            return label
    return "200+"


def computer() -> dict:
    vm, disk = psutil.virtual_memory(), shutil.disk_usage(db.DATA_DIR)
    return {
        "chip": resources.chip() or platform.processor() or platform.machine(),
        "cpu_cores": psutil.cpu_count(logical=False) or psutil.cpu_count(),
        "ram_gb": round(vm.total / GB),
        "disk_gb": int(round(disk.total / GB, -1)),
        "disk_free_percent": int(round(disk.free / disk.total * 100, -1)) if disk.total else None,
    }


def setup(cfg: dict) -> dict:
    """Which features are on, and which AI writes the notes (the model's name, never your keys)."""
    pid = llm_engine.chain("auto", cfg)[0]
    return {
        "transcribe_engine": cfg["transcribe_engine"],
        "live_final": cfg["live_final"],
        "live_notes": cfg["live_notes"],
        "notes_provider": pid,
        "notes_local": llm_engine.is_local(pid, cfg),
        "notes_model": llm_engine.model_for(pid, cfg) if pid != "custom" else "custom",
        "notes_fallback": cfg["summary_fallback"],
        "auto_transcribe": cfg["auto_transcribe"],
        "auto_summarize": cfg["auto_summarize"],
        "auto_workspace": cfg["auto_workspace"],
        "ask_questions": cfg["ask_questions"],
    }


def usage() -> dict:
    week = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    with db.connect() as conn:
        total = conn.execute("SELECT count(*) FROM meetings").fetchone()[0]
        recent_count = conn.execute("SELECT count(*) FROM meetings WHERE created_at >= ?", (week,)).fetchone()[0]
        spaces = conn.execute("SELECT count(*) FROM workspaces").fetchone()[0]
    return {"meetings": _bucket(total), "meetings_last_7_days": _bucket(recent_count), "workspaces": _bucket(spaces)}


def daily_properties(cfg: dict) -> dict:
    return {**computer(), **setup(cfg), **usage()}


def daily() -> None:
    """Once a day while Trailmix runs: that this copy is in use, on what, set up how."""
    cfg = settings.get_all()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not enabled(cfg) or cfg["stats_last_daily"] == today:
        return
    if send("app_active", daily_properties(cfg), cfg):
        settings.update({"stats_last_daily": today})


def version_changed(before: str, now: str) -> None:
    """An update installed, or someone went back: how often each version gets left is the clearest sign
    that one is broken."""
    if before and before != now:
        went_back = _older(now, before)
        send("version_changed", {"from_version": before, "to_version": now, "went_back": went_back})


def _older(a: str, b: str) -> bool:
    def parts(v: str) -> list[int]:
        return [int(x) if x.isdigit() else 0 for x in v.split(".")]
    return parts(a) < parts(b)
