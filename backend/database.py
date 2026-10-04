"""SQLite connection and schema definitions."""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.getenv("TRAILMIX_DATA_DIR", Path(__file__).parent / "data"))
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "trailmix.db"

# status: recording -> queued -> [ready_transcribe] -> [waiting_confirm] -> transcribing
#         -> [ready_summarize] -> [waiting_confirm] -> summarizing -> done | error
# ready_*: waiting for you to start that step (auto mode off); waiting_confirm: not enough free RAM.
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

-- Full-text index over what you'd search for; rowid = meetings.id. Kept in sync by update_meeting.
CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(title, transcript, summary, tokenize='porter unicode61');
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
    "speaker_names_json": "TEXT",                    # {"You": "Mark", "Them": "Dana"}
    "qa_json": "TEXT",                               # [{q, a, provider, at}] questions asked of this meeting
    "source": "TEXT",                                # where an imported meeting came from ("granola", "paste")
    "external_id": "TEXT",                           # its id there, so it isn't imported twice
}

_FTS_FIELDS = ("title", "transcript", "summary")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(meetings)")}
        for name, decl in _ADDED_COLUMNS.items():
            if name not in have:
                conn.execute(f"ALTER TABLE meetings ADD COLUMN {name} {decl}")
                if name == "transcribed":  # rows from before this column already have a final transcript
                    conn.execute("UPDATE meetings SET transcribed = 1 WHERE transcript IS NOT NULL")
        indexed = conn.execute("SELECT count(*) FROM meetings_fts").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM meetings").fetchone()[0]
        if indexed != total:  # first run with search, or an index that drifted: rebuild it
            conn.execute("DELETE FROM meetings_fts")
            conn.execute(
                "INSERT INTO meetings_fts (rowid, title, transcript, summary) "
                "SELECT id, title, coalesce(transcript, ''), coalesce(summary, '') FROM meetings"
            )


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
    conn.execute(
        "INSERT INTO meetings_fts (rowid, title, transcript, summary) "
        "SELECT id, title, coalesce(transcript, ''), coalesce(summary, '') FROM meetings WHERE id = ?",
        (meeting_id,),
    )


def create_meeting(title: str, title_auto: bool) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO meetings (title, title_auto, status) VALUES (?, ?, 'recording')", (title, int(title_auto))
        )
        meeting_id = cur.lastrowid
        audio_dir = AUDIO_DIR / str(meeting_id)
        conn.execute("UPDATE meetings SET audio_dir = ? WHERE id = ?", (str(audio_dir), meeting_id))
        _reindex(conn, meeting_id)
    return meeting_id


def create_imported_meeting(title: str, created_at: str, source: str, external_id: str | None) -> int:
    """A meeting that arrives with its transcript (no audio, nothing recorded here)."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO meetings (title, created_at, status, source, external_id, audio_deleted) VALUES (?, ?, 'queued', ?, ?, 1)",
            (title, created_at, source, external_id),
        )
        meeting_id = cur.lastrowid
        _reindex(conn, meeting_id)
    return meeting_id


def imported_ids(source: str) -> dict[str, int]:
    """external id -> meeting id, for meetings already imported from `source`."""
    with connect() as conn:
        rows = conn.execute("SELECT id, external_id FROM meetings WHERE source = ? AND external_id IS NOT NULL", (source,))
        return {r["external_id"]: r["id"] for r in rows}


def list_meetings() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, duration_sec, status FROM meetings ORDER BY id DESC"
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


def recent_meetings(limit: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def meetings_with_status(*statuses: str) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM meetings WHERE status IN ({marks})", statuses).fetchall()
    return [dict(r) for r in rows]


def meetings_with_audio() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, status FROM meetings WHERE audio_dir IS NOT NULL AND audio_deleted = 0"
        ).fetchall()
    return [dict(r) for r in rows]


_UPDATABLE = {
    "title", "status", "wait_reason", "duration_sec", "transcript", "summary", "summary_provider",
    "summary_error", "error", "has_system", "audio_deleted", "segments_json", "transcribed",
    "requested_provider", "requested_template", "title_auto", "draft_json", "exported_paths", "export_error",
    "bookmarks_json", "speaker_names_json", "qa_json",
}
_JSON_FIELDS = {"segments_json", "draft_json", "exported_paths", "bookmarks_json", "speaker_names_json", "qa_json"}


def update_meeting(meeting_id: int, **fields) -> None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"Cannot update columns: {bad}")
    if not fields:
        return
    for k in _JSON_FIELDS & set(fields):
        if not isinstance(fields[k], (str, type(None))):
            fields[k] = json.dumps(fields[k])
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE meetings SET {assignments} WHERE id = ?", (*fields.values(), meeting_id))
        if set(fields) & set(_FTS_FIELDS):
            _reindex(conn, meeting_id)


def delete_meeting(meeting_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
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


def search(text: str, limit: int = 30, any_term: bool = False) -> list[dict]:
    q = fts_query(text, any_term)
    if not q:
        return []
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.title, m.created_at, m.duration_sec, m.status,
                       snippet(meetings_fts, -1, '{HIT_START}', '{HIT_END}', '…', 14) AS snippet
                FROM meetings_fts JOIN meetings m ON m.id = meetings_fts.rowid
                WHERE meetings_fts MATCH ? ORDER BY bm25(meetings_fts, 5.0, 1.0, 2.0) LIMIT ?""",
            (q, limit),
        ).fetchall()
    return [dict(r) for r in rows]


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
            """SELECT a.id, a.text, a.done, a.meeting_id, m.title AS meeting_title, m.created_at AS meeting_created_at
               FROM action_items a JOIN meetings m ON m.id = a.meeting_id
               ORDER BY a.done, m.id DESC, a.position"""
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
