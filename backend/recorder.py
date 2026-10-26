"""The native recorder (Trailmix's menu bar app) checking in, so the web app can record through it.

The menu bar app polls about once a second with what it would record (which mic, which apps' audio, on
which Mac) and picks up any commands waiting for it. The web app's Record button then becomes "ask the
recorder to start": no browser screen-share picker, and the other side of desktop calls is captured too.
"""
import threading
import time

SEEN_WITHIN_S = 5.0  # the recorder counts as available if it checked in this recently

# What a native recorder can do. The Mac app does all of it; ports to other platforms report fewer, and the
# web app hides what's missing. A check-in without a list comes from a Mac app older than the list.
CAPABILITIES = ("system_audio", "per_app_audio", "call_reminder", "hotkeys", "open_at_login", "auto_update",
                "sound_check")

_lock = threading.Lock()
_info: dict = {}
_seen_at = 0.0
_commands: list[str] = []


def check_in(info: dict) -> list[str]:
    """Called by the menu bar app. Returns (and clears) the commands waiting for it."""
    global _info, _seen_at
    with _lock:
        _info = {k: info.get(k) for k in ("mic", "source", "machine", "version", "mic_allowed", "sound_check",
                                     "shortcut_record", "shortcut_mark", "open_at_login", "update", "call_app",
                                     "call_reminders")}
        caps = info.get("capabilities")
        _info["capabilities"] = list(CAPABILITIES) if caps is None else [str(c) for c in caps]
        _info["platform"] = info.get("platform") or "macos"  # older Mac apps didn't say which Mac
        _seen_at = time.monotonic()
        out, _commands[:] = list(_commands), []
    return out


def status() -> dict:
    with _lock:
        available = time.monotonic() - _seen_at < SEEN_WITHIN_S
        return {"available": available, **(_info if available else {})}


def request(command: str) -> bool:
    """Queues a command for the recorder. False if no recorder is checking in."""
    with _lock:
        if time.monotonic() - _seen_at >= SEEN_WITHIN_S:
            return False
        if command not in _commands:
            _commands.append(command)
        return True
