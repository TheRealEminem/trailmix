"""SQLite connection and schema definitions."""
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.getenv("TRAILMIX_DATA_DIR", Path(__file__).parent / "data"))
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "trailmix.db"

# status: recording -> queued -> [ready_transcribe] -> [waiting_confirm] -> transcribing
#         -> [ready_summarize] -> [waiting_confirm] -> summarizing -> done | error
# ready_*: waiting for you to start that step (auto mode off); waiting_confirm: not enough free RAM.
FTS_TABLE = ("CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(title, transcript, summary, tags, attachments, "
             "tokenize='porter unicode61');")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    duration_sec       REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'recording',
    wait_reason        TEXT,
    audio_path         TEXT,                       -- legacy single-file uploads
    transcript         TEXT,
    summary            TEXT,
    summary_provider   TEXT,
    summary_error      TEXT,
    error              TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL          -- JSON
);

CREATE TABLE IF NOT EXISTS action_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS action_items_meeting ON action_items(meeting_id);

-- Workspaces: the parts of your life meetings belong to (a job, a committee, personal). A meeting has one or
-- none (meetings.workspace_id). `about` describes it, for sorting meetings into it automatically.
CREATE TABLE IF NOT EXISTS workspaces (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color     TEXT NOT NULL DEFAULT 'forest',
    about     TEXT NOT NULL DEFAULT '',
    position  INTEGER NOT NULL DEFAULT 0
);

"""

# The receipt for every change to a meeting: what changed, when, from what, to what, and why. Only ever added to
# (update_meeting writes it, in the same transaction as the change), so nothing the app overwrites is lost.
# A meeting you delete takes its history with it.
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    why         TEXT
);
CREATE INDEX IF NOT EXISTS history_meeting ON history (meeting_id, id);

-- Every transcript ever made of a meeting, the one in use or not, so they can be compared and switched.
CREATE TABLE IF NOT EXISTS transcript_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    engine        TEXT,              -- local | remote | gemini | imported
    model         TEXT,
    made_with     TEXT,              -- as shown: "gemini-3.5-transcribe, after the meeting"
    source        TEXT,              -- recording | upgrade | original (from before versions were kept)
    transcript    TEXT NOT NULL,
    segments_json TEXT,
    duration_sec  REAL,
    has_system    INTEGER NOT NULL DEFAULT 0,
    seconds       REAL,              -- how long making it took
    applied       INTEGER NOT NULL DEFAULT 0,   -- it is the meeting's transcript now (when it changed: see history)
    note          TEXT
);
CREATE INDEX IF NOT EXISTS versions_meeting ON transcript_versions (meeting_id, id);

-- What paid speech engines were asked to do, and roughly what it cost (pricing.py), so spending is never a surprise.
-- Kept after a meeting is deleted: it's a record of money spent, with no meeting content in it.
CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    meeting_id    INTEGER,
    kind          TEXT NOT NULL,     -- transcript | live captions | notes | questions | ... (the Usage panel names them)
    engine        TEXT NOT NULL,
    model         TEXT,
    audio_seconds REAL NOT NULL,
    est_cost      REAL,              -- US dollars; NULL when the price isn't known
    tokens_in     INTEGER,           -- what the provider reported for text work (NULL for audio)
    tokens_out    INTEGER
);
CREATE INDEX IF NOT EXISTS usage_at ON usage (at);

-- Work sent to Google's Batch API (half price, answers come back later), until its answers have been used.
CREATE TABLE IF NOT EXISTS batch_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,            -- Google's name for it: batches/...
    kind          TEXT NOT NULL,                   -- notes
    model         TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    state         TEXT NOT NULL DEFAULT 'pending', -- pending | running | succeeded | failed | cancelled | expired
    finished_at   TEXT,
    items_json    TEXT NOT NULL,                   -- {key: {meeting_id, summary_hash}}: what each request was for
    applied       INTEGER NOT NULL DEFAULT 0,      -- the answers have been put in the meetings
    result_json   TEXT,                            -- {applied, skipped: {reason: n}, failed, cost}
    est_cost      REAL,
    error         TEXT
);

-- Other note takers whose new meetings come in by themselves (sources.py): where each one's sync stands.
CREATE TABLE IF NOT EXISTS source_sync (
    source        TEXT PRIMARY KEY,                -- granola
    since         TEXT,                            -- bring in notes made from this time on (NULL: all of them)
    last_ok       TEXT,                            -- when the last sync that got through started
    last_try      TEXT,
    last_count    INTEGER NOT NULL DEFAULT 0,      -- meetings the last sync brought in
    total         INTEGER NOT NULL DEFAULT 0,      -- meetings brought in automatically, all told
    error         TEXT
);

-- Slides, PDFs, photos and links kept with a meeting (attachments.py). Their text is never part of the summary, so
-- none of it becomes a task; with `searchable` on, it (and its digest) is in the search index and what Ask reads.
CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    position      INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL,                   -- image | pdf | slides | document | other | link
    name          TEXT NOT NULL,
    mime          TEXT,
    path          TEXT,                            -- relative to DATA_DIR/attachments; NULL for a link
    preview_path  TEXT,                            -- a small JPEG to show (browsers can't show HEIC)
    size          INTEGER NOT NULL DEFAULT 0,
    original_size INTEGER,                         -- before it was shrunk
    url           TEXT,                            -- for a link
    caption       TEXT NOT NULL DEFAULT '',
    searchable    INTEGER NOT NULL DEFAULT 0,
    text          TEXT,                            -- what was read from it
    digest        TEXT,                            -- a few bullets on what it covers
    status        TEXT NOT NULL DEFAULT 'ready',   -- ready | reading | error
    error         TEXT
);
CREATE INDEX IF NOT EXISTS attachments_meeting ON attachments (meeting_id, position);

-- Files seen in watched folders (watched.py): each is looked at once, whatever became of it.
CREATE TABLE IF NOT EXISTS watched_files (
    path          TEXT PRIMARY KEY,
    size          INTEGER NOT NULL,
    state         TEXT NOT NULL,                   -- imported | before (there when the folder was added) | skipped | failed
    meeting_id    INTEGER,
    note          TEXT,                            -- why it was skipped, or what went wrong
    seen_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


# Columns added after the first release; applied to existing databases on startup.
_ADDED_COLUMNS = {
    "wait_reason": "TEXT",                           # why a queued/paused meeting is waiting
    "audio_dir": "TEXT",                             # data/audio/<id>/ holding mic + system tracks
    "has_system": "INTEGER NOT NULL DEFAULT 0",      # was a meeting-audio track captured?
    "audio_deleted": "INTEGER NOT NULL DEFAULT 0",
    "segments_json": "TEXT",                         # [{start, end, speaker, text}]
    "transcribed": "INTEGER NOT NULL DEFAULT 0",     # final (large-model) transcript saved?
    "requested_provider": "TEXT NOT NULL DEFAULT 'auto'",
    "requested_template": "TEXT",                    # summary template for the next run (None = default)
    "title_auto": "INTEGER NOT NULL DEFAULT 0",      # title is still the generated one (safe to auto-title)
    "draft_json": "TEXT",                            # live-draft lines, kept for the draft-vs-final comparison
    "exported_paths": "TEXT",                        # JSON list of files written by the exporter
    "export_error": "TEXT",
    "bookmarks_json": "TEXT",                        # [{t, note}] moments flagged while recording or after
    "speaker_names_json": "TEXT",                    # {"You": "Dale", "Them": "Dana"}
    "qa_json": "TEXT",                               # [{q, a, provider, at}] questions asked of this meeting
    "source": "TEXT",                                # where an imported meeting came from ("granola", "paste")
    "external_id": "TEXT",                           # its id there, so it isn't imported twice
    "uid": "TEXT",                                   # permanent id, kept in exports, so re-imports are recognised
    "keep_audio": "INTEGER NOT NULL DEFAULT 0",      # "Keep forever": exempt from the audio clean-up, archived
    "live_json": "TEXT",                             # final transcript made while recording (live.py), if any
    "live_notes_json": "TEXT",                       # notes written during the meeting (live_notes.py), if any
    "my_notes_json": "TEXT",                         # notes you typed: [{"t": seconds or null, "text"}]
    "workspace_id": "INTEGER",                       # workspaces.id, or NULL: not sorted into one yet
    "workspace_auto": "INTEGER NOT NULL DEFAULT 0",  # Trailmix chose the workspace (you can still change it)
    # Which model did each job (model_ranks.py knows when a better one could redo it):
    "transcribed_with": "TEXT",                      # e.g. "whisper-large-v3-turbo, while recording"
    "summary_model": "TEXT",                         # e.g. "qwen2.5:7b"
    "workspace_model": "TEXT",                       # the model that sorted it (NULL: by hand or by title)
    "tags_json": "TEXT",                             # ["vaccine cold chain", "investor pitch", …]
    "tags_model": "TEXT",
    "imported_at": "TEXT",                           # when an imported recording came in (its audio's 30 days)
    "requested_engine": "TEXT",                      # speech engine picked for this recording (NULL: the one in Settings)
    "requested_transcribe_model": "TEXT",            # its speech model, picked before processing (NULL: Settings')
    "requested_model": "TEXT",                       # the notes model for requested_provider (NULL: Settings')
    "ask_first": "INTEGER NOT NULL DEFAULT 0",       # waits for you to choose which AI processes it (an import)
    "merged_json": "TEXT",                           # copies of this meeting merged into it (duplicates.py), to undo
    "duplicate_of": "INTEGER",                       # looks like the same meeting as this one: waiting for you to say
    "not_duplicate_json": "TEXT",                    # meetings you said aren't the same as this one
}

_FTS_FIELDS = ("title", "transcript", "summary", "tags_json")
_FTS_ROW = ("SELECT id, title, coalesce(transcript, ''), coalesce(summary, ''), coalesce(tags_json, ''), "
            "coalesce((SELECT group_concat(a.name || ' ' || a.caption || ' ' || coalesce(a.digest, '') || ' ' || "
            "coalesce(a.text, ''), ' ') FROM attachments a WHERE a.meeting_id = meetings.id AND a.searchable = 1), '') "
            "FROM meetings")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(HISTORY_SCHEMA)
        conn.execute(FTS_TABLE)
        for name in ("tokens_in", "tokens_out"):  # usage rows from before text work was counted
            if name not in {r["name"] for r in conn.execute("PRAGMA table_info(usage)")}:
                conn.execute(f"ALTER TABLE usage ADD COLUMN {name} INTEGER")
        if "template" not in {r["name"] for r in conn.execute("PRAGMA table_info(workspaces)")}:
            conn.execute("ALTER TABLE workspaces ADD COLUMN template TEXT NOT NULL DEFAULT ''")  # its note style
        have = {r["name"] for r in conn.execute("PRAGMA table_info(meetings)")}
        for name, decl in _ADDED_COLUMNS.items():
            if name not in have:
                conn.execute(f"ALTER TABLE meetings ADD COLUMN {name} {decl}")
                if name == "transcribed":  # rows from before this column already have a final transcript
                    conn.execute("UPDATE meetings SET transcribed = 1 WHERE transcript IS NOT NULL")
        for (row_id,) in conn.execute("SELECT id FROM meetings WHERE uid IS NULL").fetchall():
            conn.execute("UPDATE meetings SET uid = ? WHERE id = ?", (uuid.uuid4().hex, row_id))
        if "attachments" not in {r[1] for r in conn.execute("PRAGMA table_info(meetings_fts)")}:  # an older index
            conn.execute("DROP TABLE meetings_fts")
            conn.execute(FTS_TABLE)
        indexed = conn.execute("SELECT count(*) FROM meetings_fts").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM meetings").fetchone()[0]
        if indexed != total:  # first run with search, or an index that drifted: rebuild it
            conn.execute("DELETE FROM meetings_fts")
            conn.execute(f"INSERT INTO meetings_fts (rowid, title, transcript, summary, tags, attachments) {_FTS_ROW}")


BACKUP_DIR = DATA_DIR / "backups"
KEEP_BACKUPS = 3


def last_run_version() -> str:
    """The version of Trailmix that last started ("" before backups existed, or on a fresh install)."""
    marker = DATA_DIR / "last-version"
    return marker.read_text().strip() if marker.exists() else ""


def back_up_for(version: str) -> Path | None:
    """The first time a version of Trailmix starts, a copy of the database as the previous version left it,
    before this one changes anything: if an update lets you down, going back (or restoring) never costs you
    meetings. Keeps the newest three. Returns the copy, or None when there was nothing new to back up."""
    marker = DATA_DIR / "last-version"
    last = marker.read_text().strip() if marker.exists() else ""
    if last == version or not DB_PATH.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(version)
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"trailmix-{last or 'earlier'}-before-{version}.db"
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(target)
    try:
        src.backup(dst)  # consistent even mid-write, unlike copying the file
    finally:
        dst.close()
        src.close()
    others = sorted((p for p in BACKUP_DIR.glob("trailmix-*.db") if p != target), key=lambda p: p.stat().st_mtime)
    for old in others[:max(0, len(others) - (KEEP_BACKUPS - 1))]:  # this one, plus the newest two before it
        old.unlink()
    marker.write_text(version)
    return target


@contextmanager
def connect():
    """Short-lived connection per operation; safe across FastAPI's worker threads."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _reindex(conn, meeting_id: int) -> None:
    conn.execute("DELETE FROM meetings_fts WHERE rowid = ?", (meeting_id,))
    conn.execute(f"INSERT INTO meetings_fts (rowid, title, transcript, summary, tags, attachments) {_FTS_ROW} WHERE id = ?",
                 (meeting_id,))


def create_meeting(title: str, title_auto: bool, workspace_id: int | None = None,
                   requested_engine: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO meetings (title, title_auto, status, uid, workspace_id, requested_engine) "
            "VALUES (?, ?, 'recording', ?, ?, ?)",
            (title, int(title_auto), uuid.uuid4().hex, workspace_id, requested_engine),
        )
        meeting_id = cur.lastrowid
        audio_dir = AUDIO_DIR / str(meeting_id)
        conn.execute("UPDATE meetings SET audio_dir = ? WHERE id = ?", (str(audio_dir), meeting_id))
        _reindex(conn, meeting_id)
    return meeting_id


def create_imported_meeting(title: str, created_at: str, source: str, external_id: str | None, uid: str | None = None) -> int:
    """A meeting that arrives with its transcript (no audio until the importer adds some)."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO meetings (title, created_at, status, source, external_id, audio_deleted, uid) "
            "VALUES (?, ?, 'queued', ?, ?, 1, ?)",
            (title, created_at, source, external_id, uid or uuid.uuid4().hex),
        )
        meeting_id = cur.lastrowid
        _reindex(conn, meeting_id)
    return meeting_id


def meeting_by_uid(uid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE uid = ?", (uid,)).fetchone()
    return dict(row) if row else None


def all_meeting_ids() -> list[int]:
    with connect() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM meetings ORDER BY id")]


def imported_ids(source: str) -> dict[str, int]:
    """external id -> meeting id, for meetings already imported from `source` (also those merged into another meeting
    as a duplicate: that meeting has them now)."""
    with connect() as conn:
        rows = conn.execute("SELECT id, external_id FROM meetings WHERE source = ? AND external_id IS NOT NULL", (source,))
        out = {r["external_id"]: r["id"] for r in rows}
        for r in conn.execute("SELECT id, merged_json FROM meetings WHERE merged_json IS NOT NULL"):
            for merged in json.loads(r["merged_json"]):
                if merged["meeting"].get("source") == source and merged["meeting"].get("external_id"):
                    out[merged["meeting"]["external_id"]] = r["id"]
        return out


def count_meetings() -> int:
    with connect() as conn:
        return conn.execute("SELECT count(*) FROM meetings").fetchone()[0]


def list_meetings() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, duration_sec, status, workspace_id, ask_first FROM meetings "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_meeting(meeting_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    return dict(row) if row else None


def get_meetings(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM meetings WHERE id IN ({marks})", ids).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def recent_meetings(limit: int, workspace_id: int | None = None) -> list[dict]:
    within = "WHERE workspace_id = ?" if workspace_id else ""
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM meetings {within} ORDER BY created_at DESC, id DESC LIMIT ?",
                            (*([workspace_id] if workspace_id else []), limit)).fetchall()
    return [dict(r) for r in rows]


def meetings_with_status(*statuses: str) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM meetings WHERE status IN ({marks})", statuses).fetchall()
    return [dict(r) for r in rows]


def meetings_with_audio() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, coalesce(imported_at, created_at) AS kept_since, status FROM meetings "
            "WHERE audio_dir IS NOT NULL AND audio_deleted = 0 AND keep_audio = 0"
        ).fetchall()
    return [dict(r) for r in rows]


_UPDATABLE = {
    "title", "status", "wait_reason", "duration_sec", "transcript", "summary", "summary_provider",
    "summary_error", "error", "has_system", "audio_deleted", "segments_json", "transcribed",
    "requested_provider", "requested_template", "title_auto", "draft_json", "exported_paths", "export_error",
    "bookmarks_json", "speaker_names_json", "qa_json", "keep_audio", "audio_dir", "live_json", "workspace_id", "workspace_auto",
    "live_notes_json", "my_notes_json", "transcribed_with", "summary_model", "workspace_model", "tags_json", "tags_model", "imported_at",
    "created_at", "requested_engine", "requested_transcribe_model", "requested_model", "ask_first",
    "merged_json", "duplicate_of", "not_duplicate_json", "source", "external_id",
}
_JSON_FIELDS = {"merged_json", "not_duplicate_json", "my_notes_json", "live_notes_json", "tags_json", "segments_json", "draft_json", "exported_paths", "bookmarks_json", "speaker_names_json", "qa_json"}


# What the history keeps. Not the transient bookkeeping (status, progress, live drafts), and not segments_json: its
# text is the transcript's, and every transcript is kept whole in transcript_versions.
_TRACKED = {
    "title", "transcript", "summary", "summary_provider", "summary_model", "speaker_names_json", "my_notes_json",
    "bookmarks_json", "workspace_id", "tags_json", "created_at", "transcribed_with", "keep_audio", "audio_deleted",
}


def update_meeting(meeting_id: int, why: str | None = None, merge_s: float = 0, **fields) -> None:
    """Changes columns of a meeting. `why` goes in the history ("you renamed it", "transcribed with ..."). With
    `merge_s`, a change to a field whose last history entry has the same `why` and is less than that many seconds old
    updates that entry instead of adding one, so notes saved as you type make one entry per stretch of typing."""
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"Cannot update columns: {bad}")
    if not fields:
        return
    for k in _JSON_FIELDS & set(fields):
        if not isinstance(fields[k], (str, type(None))):
            fields[k] = json.dumps(fields[k])
    assignments = ", ".join(f"{k} = ?" for k in fields)
    tracked = [k for k in fields if k in _TRACKED]
    with connect() as conn:
        old = (conn.execute(f"SELECT {', '.join(tracked)} FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
               if tracked else None)
        conn.execute(f"UPDATE meetings SET {assignments} WHERE id = ?", (*fields.values(), meeting_id))
        for k in tracked:
            before, after = (old[k] if old else None), fields[k]
            if str(before) != str(after) and not (before in (None, "") and after in (None, "")):
                last = conn.execute(
                    "SELECT id, old_value FROM history WHERE meeting_id = ? AND field = ? AND why IS ? AND"
                    " at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) ORDER BY id DESC LIMIT 1",
                    (meeting_id, k, why, f"-{merge_s} seconds")).fetchone() if merge_s else None
                if last and last["id"] == conn.execute("SELECT max(id) FROM history WHERE meeting_id = ? AND field = ?",
                                                       (meeting_id, k)).fetchone()[0]:
                    if str(last["old_value"]) == str(after):  # typed back to how it was: no change after all
                        conn.execute("DELETE FROM history WHERE id = ?", (last["id"],))
                    else:
                        conn.execute("UPDATE history SET new_value = ?, at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                                     " WHERE id = ?", (None if after is None else str(after), last["id"]))
                    continue
                conn.execute("INSERT INTO history (meeting_id, field, old_value, new_value, why) VALUES (?, ?, ?, ?, ?)",
                             (meeting_id, k, None if before is None else str(before),
                              None if after is None else str(after), why))
        if set(fields) & set(_FTS_FIELDS):
            _reindex(conn, meeting_id)


def replace_history(meeting_id: int, rows: list[dict]) -> None:
    """Gives an imported meeting the history it had where it was exported (instead of the entries made by importing it)."""
    with connect() as conn:
        conn.execute("DELETE FROM history WHERE meeting_id = ?", (meeting_id,))
        for r in sorted(rows, key=lambda r: r.get("at") or ""):
            conn.execute("INSERT INTO history (meeting_id, at, field, old_value, new_value, why) VALUES (?, ?, ?, ?, ?, ?)",
                         (meeting_id, r["at"], r["field"], r.get("old_value"), r.get("new_value"), r.get("why")))


def history_for(meeting_id: int, field: str | None = None, limit: int = 200) -> list[dict]:
    """The changes to a meeting, newest first: {id, at, field, old_value, new_value, why}."""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM history WHERE meeting_id = ? {'AND field = ?' if field else ''} ORDER BY id DESC LIMIT ?",
            (meeting_id, *([field] if field else []), limit)).fetchall()
    return [dict(r) for r in rows]


# ── Transcript versions ─────────────────────────────────────────────────

def add_transcript_version(meeting_id: int, *, transcript: str, segments_json: str | None, engine: str | None,
                           model: str | None, made_with: str | None, source: str, duration_sec: float = 0,
                           has_system: bool = False, seconds: float | None = None, applied: bool = False,
                           note: str | None = None, created_at: str | None = None) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO transcript_versions (meeting_id, engine, model, made_with, source, transcript, segments_json,"
            " duration_sec, has_system, seconds, applied, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " coalesce(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
            (meeting_id, engine, model, made_with, source, transcript, segments_json, duration_sec, int(has_system),
             seconds, int(applied), note, created_at)).lastrowid


_baseline = threading.Lock()  # two requests must not both record a meeting's "original"


def transcript_versions(meeting_id: int) -> list[dict]:
    """Every transcript of a meeting, oldest first, with its text. A meeting from before versions were kept gets
    its current transcript recorded as the "original" first."""
    with _baseline, connect() as conn:
        rows = conn.execute("SELECT * FROM transcript_versions WHERE meeting_id = ? ORDER BY id", (meeting_id,)).fetchall()
        if not rows:
            m = conn.execute("SELECT transcript, segments_json, transcribed_with, duration_sec, has_system "
                             "FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if m and m["transcript"]:
                conn.execute(
                    "INSERT INTO transcript_versions (meeting_id, made_with, source, transcript, segments_json,"
                    " duration_sec, has_system, applied) VALUES (?, ?, 'original', ?, ?, ?, ?, 1)",
                    (meeting_id, m["transcribed_with"], m["transcript"], m["segments_json"], m["duration_sec"],
                     m["has_system"]))
                rows = conn.execute("SELECT * FROM transcript_versions WHERE meeting_id = ? ORDER BY id",
                                    (meeting_id,)).fetchall()
    return [dict(r) for r in rows]


def get_transcript_version(version_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM transcript_versions WHERE id = ?", (version_id,)).fetchone()
    return dict(row) if row else None


def mark_applied(meeting_id: int, version_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE transcript_versions SET applied = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE meeting_id = ?",
                     (version_id, meeting_id))


def log_usage(meeting_id: int | None, kind: str, engine: str, model: str | None, audio_seconds: float,
              est_cost: float | None, tokens_in: int | None = None, tokens_out: int | None = None) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO usage (meeting_id, kind, engine, model, audio_seconds, est_cost, tokens_in, tokens_out) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (meeting_id, kind, engine, model, audio_seconds, est_cost, tokens_in, tokens_out))


def usage_since(iso: str) -> list[dict]:
    """What was used by kind, engine and model since `iso`:
    [{kind, engine, model, seconds, tokens_in, tokens_out, cost, unpriced, n}]."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT kind, engine, model, sum(audio_seconds) AS seconds, sum(tokens_in) AS tokens_in, "
            "sum(tokens_out) AS tokens_out, sum(est_cost) AS cost, sum(est_cost IS NULL) AS unpriced, count(*) AS n "
            "FROM usage WHERE at >= ? GROUP BY kind, engine, model ORDER BY cost DESC, n DESC",
            (iso,)).fetchall()
    return [dict(r) for r in rows]


def add_batch_job(name: str, kind: str, model: str, items: dict, est_cost: float | None) -> int:
    with connect() as conn:
        return conn.execute("INSERT INTO batch_jobs (name, kind, model, items_json, est_cost) VALUES (?, ?, ?, ?, ?)",
                            (name, kind, model, json.dumps(items), est_cost)).lastrowid


def batch_jobs(open_only: bool = False, limit: int = 50) -> list[dict]:
    """Batch jobs, newest first. open_only: only those whose answers haven't been used yet."""
    where = "WHERE applied = 0 AND state IN ('pending', 'running', 'succeeded')" if open_only else ""
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM batch_jobs {where} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def update_batch_job(job_id: int, **fields) -> None:
    allowed = {"state", "finished_at", "applied", "result_json", "error"}
    assert set(fields) <= allowed
    with connect() as conn:
        conn.execute(f"UPDATE batch_jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), job_id))


def source_sync(source: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM source_sync WHERE source = ?", (source,)).fetchone()
    return dict(row) if row else None


def set_source_sync(source: str, **fields) -> None:
    allowed = {"since", "last_ok", "last_try", "last_count", "total", "error"}
    assert set(fields) <= allowed
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO source_sync (source) VALUES (?)", (source,))
        if fields:
            conn.execute(f"UPDATE source_sync SET {', '.join(f'{k} = ?' for k in fields)} WHERE source = ?",
                         (*fields.values(), source))


def watched_paths(under: str) -> set[str]:
    """The files in this watched folder (and below) that have been looked at already."""
    prefix = under.rstrip("/") + "/"
    with connect() as conn:
        rows = conn.execute("SELECT path FROM watched_files WHERE substr(path, 1, ?) = ?", (len(prefix), prefix))
        return {r["path"] for r in rows}


def watched_counts(under: str) -> dict[str, int]:
    """state -> how many files, in this watched folder."""
    prefix = under.rstrip("/") + "/"
    with connect() as conn:
        rows = conn.execute("SELECT state, count(*) AS n FROM watched_files WHERE substr(path, 1, ?) = ? GROUP BY state",
                            (len(prefix), prefix))
        return {r["state"]: r["n"] for r in rows}


def add_watched_files(rows: list[tuple[str, int, str, int | None, str | None]]) -> None:
    """(path, size, state, meeting_id, note) for each."""
    with connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO watched_files (path, size, state, meeting_id, note) VALUES (?, ?, ?, ?, ?)",
                         rows)


def delete_meeting(meeting_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM history WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM transcript_versions WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM attachments WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.execute("DELETE FROM meetings_fts WHERE rowid = ?", (meeting_id,))


# ── Search ──────────────────────────────────────────────────────────────

HIT_START, HIT_END = "", ""  # private-use markers the UI turns into highlights
_STOPWORDS = set(
    "a an and are as at be but by did do does for from had has have how i in is it its me my of on or our so "
    "that the their them then there these they this to was we were what when where which who why will with "
    "you your about any can could should would into than also just".split()
)


def fts_query(text: str, any_term: bool = False) -> str | None:
    """Turns free text into a safe FTS5 query: quoted terms, prefix match on the last one.
    any_term=True (for questions) drops filler words and matches meetings with any remaining term."""
    words = re.findall(r"[\w']+", text.lower())
    if any_term:
        words = [w for w in words if w not in _STOPWORDS and len(w) > 2]
    if not words:
        return None
    terms = [f'"{w}"' for w in words[:12]]
    terms[-1] += "*"
    return (" OR " if any_term else " AND ").join(terms)


def search(text: str, limit: int = 30, any_term: bool = False, workspace_id: int | None = None) -> list[dict]:
    """Best matches first. `workspace_id`: only meetings in that workspace."""
    q = fts_query(text, any_term)
    if not q:
        return []
    within = "AND m.workspace_id = ?" if workspace_id else ""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.title, m.created_at, m.duration_sec, m.status, m.workspace_id,
                       snippet(meetings_fts, -1, '{HIT_START}', '{HIT_END}', '…', 14) AS snippet
                FROM meetings_fts JOIN meetings m ON m.id = meetings_fts.rowid
                WHERE meetings_fts MATCH ? {within} ORDER BY bm25(meetings_fts, 5.0, 1.0, 2.0, 3.0, 1.0) LIMIT ?""",
            (q, *([workspace_id] if workspace_id else []), limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Attachments ─────────────────────────────────────────────────────────

_ATTACHMENT_FIELDS = {"position", "kind", "name", "mime", "path", "preview_path", "size", "original_size", "url",
                      "caption", "searchable", "text", "digest", "status", "error"}
_ATTACHMENT_FTS = {"name", "caption", "searchable", "text", "digest"}


def add_attachment(meeting_id: int, **fields) -> int:
    bad = set(fields) - _ATTACHMENT_FIELDS
    if bad:
        raise ValueError(f"Cannot set attachment columns: {bad}")
    with connect() as conn:
        if "position" not in fields:
            fields["position"] = conn.execute("SELECT coalesce(max(position), -1) + 1 FROM attachments WHERE meeting_id = ?",
                                              (meeting_id,)).fetchone()[0]
        cols = ", ".join(fields)
        cur = conn.execute(f"INSERT INTO attachments (meeting_id, {cols}) VALUES (?, {', '.join('?' * len(fields))})",
                           (meeting_id, *fields.values()))
        if fields.get("searchable"):
            _reindex(conn, meeting_id)
        return cur.lastrowid


def get_attachment(attachment_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    return dict(row) if row else None


def attachments_for(meeting_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM attachments WHERE meeting_id = ? ORDER BY position, id", (meeting_id,))
        return [dict(r) for r in rows]


def update_attachment(attachment_id: int, **fields) -> None:
    bad = set(fields) - _ATTACHMENT_FIELDS
    if bad:
        raise ValueError(f"Cannot update attachment columns: {bad}")
    if not fields:
        return
    with connect() as conn:
        conn.execute(f"UPDATE attachments SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), attachment_id))
        if set(fields) & _ATTACHMENT_FTS:
            row = conn.execute("SELECT meeting_id FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
            if row:
                _reindex(conn, row["meeting_id"])


def delete_attachment(attachment_id: int) -> None:
    with connect() as conn:
        row = conn.execute("SELECT meeting_id FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        if row:
            _reindex(conn, row["meeting_id"])


# ── Action items ────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def replace_tasks(meeting_id: int, texts: list[str]) -> None:
    """Replaces a meeting's action items, keeping 'done' on items whose text survived a regenerate."""
    with connect() as conn:
        done = {_norm(r["text"]) for r in conn.execute(
            "SELECT text FROM action_items WHERE meeting_id = ? AND done = 1", (meeting_id,))}
        conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
        conn.executemany(
            "INSERT INTO action_items (meeting_id, position, text, done) VALUES (?, ?, ?, ?)",
            [(meeting_id, i, t, int(_norm(t) in done)) for i, t in enumerate(texts)],
        )


def tasks_for(meeting_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, text, done FROM action_items WHERE meeting_id = ? ORDER BY position", (meeting_id,)
        ).fetchall()
    return [{**dict(r), "done": bool(r["done"])} for r in rows]


def all_tasks() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.id, a.text, a.done, a.meeting_id, m.title AS meeting_title, m.created_at AS meeting_created_at,
                      m.workspace_id
               FROM action_items a JOIN meetings m ON m.id = a.meeting_id
               ORDER BY a.done, m.created_at DESC, m.id DESC, a.position"""
        ).fetchall()
    return [{**dict(r), "done": bool(r["done"])} for r in rows]


def set_task_done(task_id: int, done: bool) -> int | None:
    """Returns the task's meeting id, or None if there's no such task."""
    with connect() as conn:
        row = conn.execute("SELECT meeting_id FROM action_items WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE action_items SET done = ? WHERE id = ?", (int(done), task_id))
    return row["meeting_id"]


# ── Workspaces ──────────────────────────────────────────────────────────

WORKSPACE_COLORS = ("forest", "sky", "sun", "trail", "ink")


def list_workspaces() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT w.*, (SELECT count(*) FROM meetings m WHERE m.workspace_id = w.id) AS meetings
               FROM workspaces w ORDER BY w.position, w.id"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_workspace(workspace_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return dict(row) if row else None


def workspace_by_name(name: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE name = ?", (name.strip(),)).fetchone()
    return dict(row) if row else None


def create_workspace(name: str, color: str = "", about: str = "") -> int:
    with connect() as conn:
        count, top = conn.execute("SELECT count(*), coalesce(max(position), -1) FROM workspaces").fetchone()
        color = color if color in WORKSPACE_COLORS else WORKSPACE_COLORS[count % len(WORKSPACE_COLORS)]
        return conn.execute("INSERT INTO workspaces (name, color, about, position) VALUES (?, ?, ?, ?)",
                            (name.strip(), color, about.strip(), top + 1)).lastrowid


def update_workspace(workspace_id: int, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("name", "color", "about", "position", "template") and v is not None}
    if not allowed:
        return
    with connect() as conn:
        conn.execute(f"UPDATE workspaces SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?",
                     (*allowed.values(), workspace_id))


def delete_workspace(workspace_id: int) -> None:
    """Its meetings stay, just not in a workspace any more."""
    with connect() as conn:
        conn.execute("UPDATE meetings SET workspace_id = NULL, workspace_auto = 0 WHERE workspace_id = ?", (workspace_id,))
        conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))


def unsorted_meeting_ids() -> list[int]:
    with connect() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM meetings WHERE workspace_id IS NULL AND status != 'recording' ORDER BY created_at DESC")]
