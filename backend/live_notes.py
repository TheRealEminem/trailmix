"""Notes written during the meeting (settings.live_notes): every ten minutes of a recording, the notes AI writes
notes on that stretch of the live transcript, the way a note-taker jots things down as the meeting goes.

You see them in the window while you're recording ("Notes so far"). When you stop, a long meeting's notes are
written from these plus notes on the minutes after them, instead of starting from the top of the transcript
(measured: a 46-minute meeting on a 16 GB M4 with qwen2.5:7b, 3 minutes instead of 4 min 48 s, with the
notes so far on screen at once). A meeting short enough to read in one go is still written from the full
transcript, which gives the best notes.

The setting is Automatic, On or Off. Automatic is on when a cloud AI writes your notes (nothing extra runs on
this Mac) or this Mac has 32 GB of memory or more; with less, the notes model (about 5 GB with a 7B model)
would sit next to your call apps and the speech model, so it's yours to turn on. With a model on this Mac,
notes also wait while you're on battery below 40% or in Low Power Mode, or macOS reports critical memory
pressure (the same line the summary waits at); the meeting's notes then catch up after you stop, as they
always have.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

import psutil

import database as db
import live
import llm_engine
import meeting_text
import questions
import resources
import settings
import templates

log = logging.getLogger("trailmix.live_notes")

EVERY_S = int(os.getenv("TRAILMIX_LIVE_NOTES_EVERY_S", "600"))  # one stretch of meeting per set of notes
MOST_S = 2 * EVERY_S  # catching up after a pause: at most this much in one go
MIN_BATTERY = 40     # on battery below this (%), a local model waits
BIG_RAM_GB = 32      # Automatic turns on for a local model from this much memory
RETRY_S = 60         # after a failed attempt
POWER_CACHE_S = 10

_state: dict[int, dict] = {}  # meeting id -> {"notes": [...], "status": str, "busy": bool, "retry_at": float}
_lock = threading.Lock()
_power: tuple[float, dict] | None = None


# ── Should it run? ──────────────────────────────────────────────────────

def power() -> dict:
    """{"battery": has one, "plugged_in": bool, "percent": int | None, "low_power": bool}. A Mac without a
    battery (or where it can't be read) counts as plugged in."""
    global _power
    if _power and time.time() - _power[0] < POWER_CACHE_S:
        return _power[1]
    out = {"battery": False, "plugged_in": True, "percent": None, "low_power": False}
    if sys.platform == "darwin":
        try:
            batt = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
            found = re.search(r"InternalBattery.*?(\d+)%", batt)
            if found:
                out["battery"] = True
                out["percent"] = int(found.group(1))
                out["plugged_in"] = "AC Power" in batt
            modes = subprocess.run(["/usr/bin/pmset", "-g"], capture_output=True, text=True, timeout=3).stdout
            out["low_power"] = bool(re.search(r"^\s*(lowpowermode|powermode)\s+1\b", modes, re.M))
        except (OSError, subprocess.TimeoutExpired):
            pass
    _power = (time.time(), out)
    return out


def _notes_provider(cfg: dict) -> tuple[str, bool]:
    pid = llm_engine.chain("auto", cfg)[0]
    return pid, llm_engine.is_local(pid, cfg)


def decide(cfg: dict) -> dict:
    """Whether notes are written during meetings with these settings, and why, for Settings:
    {"on", "local", "reason", "warning", "ram_gb", "model_gb", "power"}."""
    pid, local = _notes_provider(cfg)
    ram = psutil.virtual_memory().total / resources.GB
    # What the model takes once loaded, with its working memory (Ollama's own figure runs a little lower)
    model_gb = llm_engine.ollama_ram_needed_gb(cfg) if local and llm_engine.ollama_model_size_gb(cfg) else 0
    choice = cfg["live_notes"]
    out = {"local": local, "ram_gb": round(ram), "model_gb": round(model_gb, 1), "power": power(), "warning": None}
    name = llm_engine.label(pid, cfg)
    if choice == "off":
        return {**out, "on": False, "reason": "Off: the notes are written after the meeting ends."}
    if not local:
        return {**out, "on": True, "reason": f"On: {name} writes them, so nothing extra runs on this Mac."}
    size = f"about {model_gb:.0f} GB" if model_gb else "several GB"
    if choice == "auto" and ram < BIG_RAM_GB:
        return {**out, "on": False, "reason": (
            f"Off for now: with {ram:.0f} GB of memory, the notes model ({size}) would run next to your call app "
            "and the speech model. Choose On to have it anyway.")}
    warning = None
    if ram < BIG_RAM_GB:
        warning = (f"The notes model ({size}) runs alongside your call every ten minutes. With {ram:.0f} GB of "
                   "memory, your call app or the live transcript may slow down while it does.")
    reason = ("On: the notes model runs on this Mac every ten minutes during a meeting, and lets go of its memory "
              "in between.")
    return {**out, "on": True, "reason": reason, "warning": warning}


def wait_reason(cfg: dict) -> str | None:
    """Why not to write notes right this moment (a local model only), or None to go ahead."""
    _, local = _notes_provider(cfg)
    if not local:
        return None
    p = power()
    if p["battery"] and not p["plugged_in"]:
        if p["low_power"]:
            return "Paused while Low Power Mode is on"
        if p["percent"] is not None and p["percent"] < MIN_BATTERY:
            return f"Paused on battery at {p['percent']}%"
    if resources.memory_pressure() >= 4:  # critical; at "warning" (Cloudy) macOS copes, as for the summary
        return "Paused while memory is critically low"
    return None


# ── During the meeting ──────────────────────────────────────────────────

def status(meeting_id: int) -> dict:
    """{"notes": [{"start", "end", "text"}], "notes_status": str | None} for the live view."""
    with _lock:
        st = _state.get(meeting_id)
        if not st:
            return {"notes": [], "notes_status": None}
        return {"notes": [n for n in st["notes"] if n["text"]], "notes_status": st["status"]}


def _transcript(session: "live.LiveSession", start: float, end: float, names: dict) -> str:
    """The live transcript from `start` to `end` seconds, as speaker turns like the full transcript."""
    lines = sorted((d for d in list(session.drafts) if start <= d["start"] < end), key=lambda d: d["start"])
    segs = [{**d, "end": d["start"] + 5} for d in lines]
    return "\n".join(
        f"[{meeting_text.fmt(t['start'])}] {names.get(t['speaker'], t['speaker']) + ': ' if t.get('speaker') else ''}{t['text']}"
        for t in meeting_text.turns(segs)
    )


def tick() -> None:
    """Called every few seconds by the engine: starts notes on the next stretch of any recording that has ten
    new minutes of transcript, if the setting and this Mac's state allow. One at a time."""
    cfg = settings.get_all()
    sessions = [s for s in live.sessions() if s.draft or s.final]
    with _lock:
        for gone in set(_state) - {s.meeting_id for s in sessions}:
            if not _state[gone]["busy"]:
                _state.pop(gone)
        if any(st["busy"] for st in _state.values()):
            return
    if not sessions or not decide(cfg)["on"]:
        return
    for session in sessions:
        with _lock:
            st = _state.setdefault(session.meeting_id, {"notes": [], "status": None, "busy": False, "retry_at": 0})
            covered = st["notes"][-1]["end"] if st["notes"] else 0.0
        ready = session.transcribed_until
        if ready - covered < EVERY_S:
            with _lock:
                if not st["status"] or not st["status"].startswith(("Paused", "Couldn't")):
                    st["status"] = f"{'Next' if st['notes'] else 'First'} notes at {meeting_text.fmt(covered + EVERY_S)}"
            continue
        why = wait_reason(cfg)
        with _lock:
            if why:
                st["status"] = f"{why}. They catch up after the meeting."
                continue
            if time.time() < st["retry_at"]:
                continue
            end = covered + min(MOST_S, (ready - covered) // EVERY_S * EVERY_S)
            st["busy"] = True
            st["status"] = f"Writing notes on {meeting_text.fmt(covered)} to {meeting_text.fmt(end)}…"
        threading.Thread(target=_write, args=(session, covered, end, cfg), daemon=True, name="live-notes").start()
        return


def _write(session: "live.LiveSession", start: float, end: float, cfg: dict) -> None:
    mid = session.meeting_id
    try:
        m = db.get_meeting(mid) or {}
        names = meeting_text.speaker_names(m, cfg)
        text = _transcript(session, start, end, names)
        notes = ""
        if text.strip():
            about = " ".join(x for x in (
                f'"{names["You"]}" is the person recording; "{names["Them"]}" is everyone else on the call.'
                if session.has_system else "",
                questions.vocabulary_prompt().replace("Names and terms:", "Names and terms that may come up, spelled correctly:"),
            ) if x)
            with _lock:
                part = len(_state[mid]["notes"]) + 1
            system, prompt = templates.notes_prompt(text, part, None, about)
            # A local model lets go of its memory straight after, so it isn't held through the call.
            notes = llm_engine.tidy(llm_engine.generate(_notes_provider(cfg)[0], cfg, prompt, system=system, keep_alive="0"))
        with _lock:
            st = _state[mid]
            st["notes"].append({"start": start, "end": end, "text": notes})
            st["status"] = f"Next notes at {meeting_text.fmt(end + EVERY_S)}"
            saved = list(st["notes"])
        db.update_meeting(mid, live_notes_json=saved)
    except Exception as e:
        log.warning("Notes during meeting %s failed: %s", mid, e)
        with _lock:
            st = _state[mid]
            st["status"] = f"Couldn't write notes just now ({str(e)[:120]}). Trying again in a minute."
            st["retry_at"] = time.time() + RETRY_S
    finally:
        with _lock:
            _state[mid]["busy"] = False


def wait_until_idle(meeting_id: int, timeout: float = 180) -> None:
    """After the recording stops: lets notes already being written finish (the meeting's notes use them)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            st = _state.get(meeting_id)
            if not st or not st["busy"]:
                return
        time.sleep(1)


# ── After the meeting ───────────────────────────────────────────────────

def usable(m: dict) -> tuple[list[dict], float] | None:
    """(notes, seconds they cover) to write a long meeting's notes from, or None. Only when the transcript is
    the one made while recording: notes on a rough draft shouldn't stand in for the accurate transcript."""
    written = json.loads(m["live_notes_json"]) if m.get("live_notes_json") else []
    if not written or "while recording" not in (m.get("transcribed_with") or ""):
        return None
    return [n for n in written if n.get("text")], written[-1]["end"]  # a stretch with no speech has no notes
