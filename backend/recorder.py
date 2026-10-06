"""The native recorder (Trailmix's menu bar app) checking in, so the web app can record through it.

The menu bar app polls about once a second with what it would record (which mic, which apps' audio, on
which Mac) and picks up any commands waiting for it. The web app's Record button then becomes "ask the
recorder to start": no browser screen-share picker, and the other side of desktop calls is captured too.
"""
import threading
import time

SEEN_WITHIN_S = 5.0  # the recorder counts as available if it checked in this recently

_lock = threading.Lock()
_info: dict = {}
_seen_at = 0.0
_commands: list[str] = []


def check_in(info: dict) -> list[str]:
    """Called by the menu bar app. Returns (and clears) the commands waiting for it."""
    global _info, _seen_at
    with _lock:
        _info = {k: info.get(k) for k in ("mic", "source", "machine", "version", "mic_allowed", "sound_check",
                                     "shortcut_record", "shortcut_mark", "open_at_login")}
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
