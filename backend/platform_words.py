"""Words that fit the computer this server runs on, for what it says to people: "this Mac" and the menu bar on a
Mac, "this PC" and the system tray on Windows, plain "this computer" elsewhere. The web app's platform.ts has the
same words (it learns the OS from /api/health's "os")."""

import platform
import sys

_WORDS = {
    "macos": {"os_name": "macOS", "computer": "Mac", "this_computer": "this Mac", "your_computer": "your Mac",
              "recorder_name": "menu bar recorder"},
    "windows": {"os_name": "Windows", "computer": "PC", "this_computer": "this PC", "your_computer": "your PC",
                "recorder_name": "tray recorder"},
    "linux": {"os_name": "Linux", "computer": "computer", "this_computer": "this computer",
              "your_computer": "your computer", "recorder_name": "tray recorder"},
    "unknown": {"os_name": "the system", "computer": "computer", "this_computer": "this computer",
                "your_computer": "your computer", "recorder_name": "tray recorder"},
}


def os_id(plat: str | None = None) -> str:
    """"macos", "windows", "linux" or "unknown", from sys.platform (or `plat`)."""
    p = sys.platform if plat is None else plat
    if p == "darwin":
        return "macos"
    if p.startswith(("win", "cygwin", "msys")):
        return "windows"
    if p.startswith("linux"):
        return "linux"
    return "unknown"


def words(plat: str | None = None) -> dict:
    """{"os", "os_name", "computer", "this_computer", "your_computer", "recorder_name"} for this server's OS."""
    which = os_id(plat)
    return {"os": which, **_WORDS[which]}


def this_computer(plat: str | None = None) -> str:
    """"this Mac", "this PC", "this computer"."""
    return _WORDS[os_id(plat)]["this_computer"]


def cap(s: str) -> str:
    """"this Mac" → "This Mac"."""
    return s[:1].upper() + s[1:]


def os_version() -> str:
    """e.g. "15.1" on a Mac, "11 (10.0.22631)" on Windows; whatever the platform module says elsewhere."""
    if sys.platform == "darwin":
        return platform.mac_ver()[0] or platform.platform()
    if sys.platform == "win32":
        return f"{platform.release()} ({platform.version()})"
    return platform.platform()


# The labels diagnostics use ("macOS: 15.1", "Mac: Apple M4…", "Windows: …", "PC: …") on any OS, so they can be
# told apart from someone speaking.
ALL_LABELS = tuple(sorted({w["os_name"] for w in _WORDS.values() if w["os_name"] != "the system"}
                          | {cap(w["computer"]) for w in _WORDS.values()}))
