"""The native recorder (Trailmix's menu bar app) staying in touch, so the web app can record through it.

The menu bar app keeps a socket open (/api/recorder/ws in main.py) and sends what it would record (which
mic, which apps' audio, on which Mac) whenever that changes; commands for it go down the same socket the
moment they're asked for. The web app's Record button then becomes "ask the recorder to start": no browser
screen-share picker, and the other side of desktop calls is captured too.

An older menu bar app checks in over POST about once a second instead, and collects its commands then.
"""
import threading
import time
from collections.abc import Callable

import events

SEEN_WITHIN_S = 5.0  # without a socket, the recorder counts as available if it checked in this recently

# What a native recorder can do. The Mac app does all of it; ports to other platforms report fewer, and the
# web app hides what's missing. A check-in without a list comes from a Mac app older than the list.
CAPABILITIES = ("system_audio", "per_app_audio", "call_reminder", "hotkeys", "open_at_login", "auto_update",
                "sound_check")

_lock = threading.Lock()
_info: dict = {}
_seen_at = 0.0
_commands: list[str] = []
_push: Callable[[list[str]], None] | None = None  # sends commands down the open socket, while there is one


def check_in(info: dict) -> list[str]:
    """Called with what the menu bar app reports (over its socket, or an older one's POST). Returns (and
    clears) the commands waiting for it."""
    global _info, _seen_at
    with _lock:
        was = status_locked()
        _info = {k: info.get(k) for k in ("mic", "source", "machine", "version", "mic_allowed", "sound_check",
                                     "shortcut_record", "shortcut_mark", "open_at_login", "update", "call_app",
                                     "call_reminders")}
        caps = info.get("capabilities")
        _info["capabilities"] = list(CAPABILITIES) if caps is None else [str(c) for c in caps]
        _info["platform"] = info.get("platform") or "macos"  # older Mac apps didn't say which Mac
        _seen_at = time.monotonic()
        out, _commands[:] = list(_commands), []
        changed = status_locked() != was
    if changed:
        events.notify()
    return out


def attach(push: Callable[[list[str]], None]) -> None:
    """The menu bar app's socket is open: `push(commands)` sends it commands at once."""
    global _push
    with _lock:
        _push = push
    events.notify()


def detach(push: Callable[[list[str]], None]) -> None:
    """Its socket closed (a later one may already have replaced it)."""
    global _push, _seen_at
    with _lock:
        if _push is not push:
            return
        _push = None
        _seen_at = 0.0
    events.notify()


def status_locked() -> dict:
    available = _push is not None or time.monotonic() - _seen_at < SEEN_WITHIN_S
    return {"available": available, **(_info if available else {})}


def status() -> dict:
    with _lock:
        return status_locked()


def request(command: str) -> bool:
    """Sends a command to the recorder (or, for an older one, queues it for its next check-in). False if no
    recorder is in touch."""
    with _lock:
        push = _push
        if push is None:
            if time.monotonic() - _seen_at >= SEEN_WITHIN_S:
                return False
            if command not in _commands:
                _commands.append(command)
            return True
    push([command])
    return True
