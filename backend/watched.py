"""Watched folders: recordings and transcripts that land in a folder you chose come into Trailmix by themselves.

For what other apps save as files: Zoom's local recordings, Google Meet and Teams recordings and transcripts synced
by Google Drive or OneDrive, exports from Otter or MacWhisper. Every minute (main.py's loop) each folder is looked
through, three levels deep. A new file comes in once it has stopped changing (another app may still be writing or
syncing it), and each file is looked at only once, whatever became of it. Your files are only read: never moved,
renamed or changed.

What's in a folder when you add it stays out, unless you ask for it. Also left out: hidden files and folders,
downloads in progress, Trailmix's own export folder and data, a video when the same recording's audio is next to it
(Zoom saves both), and text files that don't look like a transcript (no speakers), so a folder of notes doesn't
turn into meetings. Recordings wait for you to pick which AI transcribes them when "Ask before AI" is on.

Reading a folder sends nothing anywhere, so this works in Lockdown mode too.
"""
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import database as db
import file_import
import settings

log = logging.getLogger("trailmix.import")

DEPTH = 3                 # folders below the watched one that are looked in
MAX_FILES = 20_000        # per folder and look: a whole disk added by mistake doesn't stall the loop
SETTLE_S = 20             # a file unchanged since the last look, and for at least this long, is finished
MAX_TRANSCRIPT = 20 * 1024 * 1024
PARTIAL = {".part", ".partial", ".crdownload", ".download", ".tmp", ".icloud"}
BUNDLES = (".app", ".photoslibrary", ".musiclibrary", ".fcpbundle", ".imovielibrary")
VIDEO = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".3gp", ".mpeg", ".mpg"}
# Names apps give their files that say nothing about the meeting (Zoom's audio1234567890.m4a, closed_caption.txt):
# the folder they're in is named after it instead
_GENERIC_WORDS = {"audio", "video", "recording", "closed", "caption", "captions", "transcript", "zoom", "gmt", "x",
                  "only", "meeting", "chat"}

NOT_ALLOWED = ("Trailmix isn't allowed to read this folder. Allow it in System Settings → Privacy & Security → "
               "Files and Folders (or Full Disk Access).")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _excluded(cfg: dict) -> list[Path]:
    """Trailmix's own folders: what it exports and keeps mustn't come back in as new meetings."""
    return [Path(cfg["export_dir"]).expanduser().resolve(), Path(db.DATA_DIR).resolve()]


def _inside(p: Path, roots: list[Path]) -> bool:
    return any(p == r or r in p.parents for r in roots)


def _files(root: Path, excluded: list[Path]):
    """(path, stat) of each file in the folder Trailmix could import, three levels deep."""
    os.listdir(root)  # PermissionError here: not allowed to read it at all
    found = 0
    for here, dirs, names in os.walk(root):
        level = len(Path(here).relative_to(root).parts)
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.endswith(BUNDLES) and level < DEPTH
                   and not _inside(Path(here, d).resolve(), excluded)]
        for name in names:
            path = Path(here, name)
            if name.startswith(".") or path.suffix.lower() in PARTIAL or not file_import.kind(name):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            yield path, st
            found += 1
            if found >= MAX_FILES:
                return


def _generic(stem: str) -> bool:
    return all(w in _GENERIC_WORDS for w in re.findall(r"[a-z]+", stem.lower()))


def _pair(path: Path) -> tuple[str, str]:
    """The same recording saved as audio and as video: same folder, same name but for audio/video and a size."""
    return str(path.parent), re.sub(r"audio|video|_?\d{3,4}x\d{3,4}", "", path.stem.lower())


def name_for(path: Path, root: Path) -> str:
    """The file's name for its title: the folder it's in when the file's own name is just "audio1234.m4a"."""
    if _generic(path.stem) and path.parent != root:
        folder = re.sub(r"^\d{4}-\d{2}-\d{2}[ _T]?(\d{1,2}[.:]\d{2}([.:]\d{2})?)?\s*", "", path.parent.name).strip()
        if folder:
            return folder + path.suffix.lower()
    return path.name


# ── Looking through the folders ─────────────────────────────────────────

_pending: dict[str, tuple[int, float]] = {}  # new files seen once: (size, modified) then, to see they've settled
_status: dict[str, dict] = {}                # folder -> {last_scan, last_count, error}
_scan_lock = threading.Lock()


def _import(path: Path, st: os.stat_result, root: Path, cfg: dict) -> tuple[str, int | None, str | None]:
    """(state, meeting id, note) for one settled file."""
    name = name_for(path, root)
    when = datetime.fromtimestamp(st.st_mtime, timezone.utc)
    if file_import.kind(path.name) == "recording":
        meeting_id = file_import.recording_from(name, path, when)
        return ("imported", meeting_id, None) if meeting_id else ("skipped", None, "imported before")
    if st.st_size > MAX_TRANSCRIPT:
        return "skipped", None, "too big for a transcript"
    data = path.read_bytes()
    my_names = {cfg["your_name"]}
    if path.suffix.lower() in (".txt", ".md"):
        if not any(s.get("speaker") for s in file_import.parse_transcript(path.name, data, my_names)):
            return "skipped", None, "doesn't look like a transcript (no speakers)"
    meeting_id = file_import.import_transcript(name, data, when, my_names)
    return ("imported", meeting_id, None) if meeting_id else ("skipped", None, "imported before")


def scan(folder: str, cfg: dict) -> int:
    """Brings in the folder's new files that have settled. Returns how many came in."""
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError("This folder isn't there (renamed, or on a disk that isn't connected?)")
    known = db.watched_paths(folder)
    files = [(p, st) for p, st in _files(root, _excluded(cfg)) if str(p) not in known]
    now = time.time()
    ready = []
    for p, st in files:
        seen = (st.st_size, st.st_mtime)
        age = now - st.st_mtime  # below zero: dated ahead (synced from a computer whose clock is fast)
        if _pending.get(str(p)) == seen and (age >= SETTLE_S or age < 0):
            ready.append((p, st))
        _pending[str(p)] = seen
    audio = {_pair(p) for p, _ in files if file_import.kind(p.name) == "recording" and p.suffix.lower() not in VIDEO}
    made = 0
    for p, st in ready:
        _pending.pop(str(p), None)
        if p.suffix.lower() in VIDEO and _pair(p) in audio:
            state, meeting_id, note = "skipped", None, "its audio is next to it"
        else:
            try:
                state, meeting_id, note = _import(p, st, root, cfg)
            except Exception as e:  # unreadable, not really a recording: noted, not tried again
                log.warning("Importing %s from a watched folder: %s", p, e)
                state, meeting_id, note = "failed", None, str(e)[:300]
        db.add_watched_files([(str(p), st.st_size, state, meeting_id, note)])
        made += state == "imported"
    return made


def scan_all() -> None:
    """The loop's look through every watched folder."""
    if not _scan_lock.acquire(blocking=False):
        return
    try:
        cfg = settings.get_all()
        for folder in cfg["watch_folders"]:
            try:
                made = scan(folder, cfg)
                _status[folder] = {"last_scan": _now_iso(), "last_count": made, "error": None}
            except PermissionError:
                _status[folder] = {**_status.get(folder, {}), "last_scan": _now_iso(), "error": NOT_ALLOWED}
            except Exception as e:
                _status[folder] = {**_status.get(folder, {}), "last_scan": _now_iso(), "error": str(e)[:300]}
    finally:
        _scan_lock.release()


# ── Adding and removing ─────────────────────────────────────────────────

def add(path: str, existing: bool = False) -> str:
    """Starts watching a folder. existing: also bring in what's in it now (otherwise only what arrives from now on)."""
    cfg = settings.get_all()
    p = Path(path.strip()).expanduser()
    if not p.is_absolute():
        raise ValueError("Use the folder's full path, or start it with ~")
    p = p.resolve()
    if not p.is_dir():
        raise ValueError(f"There's no folder at {p}")
    if _inside(p, _excluded(cfg)):
        raise ValueError("That's where Trailmix keeps or exports meetings: they'd come back in as copies")
    if str(p) in cfg["watch_folders"]:
        return str(p)
    if not existing:  # what's there now counts as seen
        try:
            files = list(_files(p, _excluded(cfg)))
        except PermissionError:
            raise ValueError(NOT_ALLOWED)
        db.add_watched_files([(str(f), st.st_size, "before", None, None) for f, st in files])
    settings.update({"watch_folders": cfg["watch_folders"] + [str(p)]})
    return str(p)


def remove(path: str) -> None:
    """Stops watching it. The files it brought in stay, and what was seen there is remembered."""
    cfg = settings.get_all()
    settings.update({"watch_folders": [f for f in cfg["watch_folders"] if f != path]})
    _status.pop(path, None)


def label_for(path: str) -> str:
    """A watched folder's name in the list: what it's for when it's one of the folders other apps save into."""
    p = Path(path)
    if p == Path.home() / "Documents" / "Zoom":
        return "Zoom recordings"
    if p.name == "Meet Recordings" and p.parent.parent.name.startswith("GoogleDrive-"):
        return "Google Meet recordings"
    if p.name == "Recordings" and p.parent.name.startswith("OneDrive"):
        return "Teams recordings"
    return p.name or str(p)


def _suggestions(watching: list[str]) -> list[dict]:
    """Folders other meeting apps save into that are on this Mac and not watched yet."""
    home = Path.home()
    found = [("Zoom recordings", home / "Documents" / "Zoom")]
    cloud = home / "Library" / "CloudStorage"
    try:
        drives = sorted(cloud.iterdir()) if cloud.is_dir() else []
    except OSError:
        drives = []
    for d in drives:
        if d.name.startswith("GoogleDrive-"):
            found.append(("Google Meet recordings (Google Drive)", d / "My Drive" / "Meet Recordings"))
        elif d.name.startswith("OneDrive"):
            found.append(("Teams recordings (OneDrive)", d / "Recordings"))
    out = []
    for label, p in found:
        try:
            if p.is_dir() and str(p.resolve()) not in watching:
                out.append({"label": label, "path": str(p)})
        except OSError:
            continue
    return out


def status() -> dict:
    cfg = settings.get_all()
    folders = []
    for f in cfg["watch_folders"]:
        counts = db.watched_counts(f)
        prefix = f.rstrip("/") + "/"
        folders.append({
            "path": f, "imported": counts.get("imported", 0), "skipped": counts.get("skipped", 0),
            "failed": counts.get("failed", 0), "waiting": sum(1 for p in list(_pending) if p.startswith(prefix)),
            **{k: _status.get(f, {}).get(k) for k in ("last_scan", "last_count", "error")},
        })
    return {"folders": folders, "suggestions": _suggestions(cfg["watch_folders"])}
