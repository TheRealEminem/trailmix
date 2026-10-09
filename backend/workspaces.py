"""Sorting meetings into workspaces (a job, a committee, personal life…).

A meeting whose title names a workspace goes there. Otherwise the summary AI is shown the workspaces (with
what you wrote about each) and the meeting's notes, and picks one, or none: a meeting that fits nowhere stays
unsorted rather than being forced into the wrong place. Meetings you placed by hand are never moved.
"""
import re
import threading

import database as db
import llm_engine

NOTES_CHARS = 2500  # how much of the notes (or transcript) the AI sees: the overview is what matters


def _named_in(name: str, text: str) -> bool:
    """The name as a word or words, however it's spaced: "Cold Connect" also finds "ColdConnect"."""
    spaced = r"[\s_-]*".join(re.escape(w) for w in name.split())
    return bool(re.search(r"(?<!\w)" + spaced + r"(?!\w)", text, re.I))


EXAMPLES = 4  # meetings already in each workspace shown to the AI (yours first): small models need them


def _examples(space: dict, leave_out: int) -> list[str]:
    """Meetings certainly in this workspace: placed there by hand, or named after it. Never ones the AI
    sorted, or one early guess would pull in more and more like it."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT title, workspace_auto FROM meetings WHERE workspace_id = ? AND id != ?
               ORDER BY workspace_auto, created_at DESC""", (space["id"], leave_out)).fetchall()
    sure = [r["title"] for r in rows if not r["workspace_auto"] or _named_in(space["name"], r["title"])]
    return sure[:EXAMPLES]


def _prompt(m: dict, spaces: list[dict]) -> str:
    listing = []
    for w in spaces:
        line = f"- {w['name']}" + (f": {w['about']}" if w["about"] else "")
        examples = _examples(w, m["id"])
        if examples:
            line += "\n  Meetings already in it: " + "; ".join(f'"{t}"' for t in examples)
        listing.append(line)
    # The notes without a Flagged Moments list (older notes can have long invented ones).
    material = llm_engine.place_moments(m.get("summary") or m.get("transcript") or "", [])[:NOTES_CHARS]
    return f"""MEETING: {m['title']}
{material}

---
WORKSPACES:
{chr(10).join(listing)}

Pick the workspace this meeting fits best. Reply with two lines and nothing else:
Workspace: <its name, exactly as listed>
Fit: <good, partial, or poor>"""


def _answer(text: str, candidates: list[dict]) -> dict | None:
    """The workspace named on the "Workspace:" line, if the model says it fits well. ("Partial" fits were
    mostly wrong in testing, e.g. tabletop games filed under a startup: better left for you to sort.)"""
    named = re.search(r"workspace:\s*(.+)", text, re.I)
    fit = re.search(r"fit:\s*(\w+)", text, re.I)
    if not named or not fit or fit.group(1).lower() != "good":
        return None
    said = named.group(1).strip(" .*\"'`")
    exact = [w for w in candidates if w["name"].lower() == said.lower()]
    if exact:
        return exact[0]
    mentioned = [w for w in candidates if _named_in(w["name"], said)]
    return mentioned[0] if len(mentioned) == 1 else None  # a name it made up: no


def choose(m: dict, spaces: list[dict], cfg: dict, pid: str | None = None) -> dict | None:
    """The workspace a meeting belongs in, or None. `pid`: the AI provider to ask (default: your chain)."""
    if not spaces:
        return None
    titled = [w for w in spaces if _named_in(w["name"], m["title"])]
    if len(titled) == 1:
        return titled[0]
    candidates = titled or spaces
    for provider in [pid] if pid else llm_engine.chain("auto", cfg):
        try:
            return _answer(llm_engine.generate(provider, cfg, _prompt(m, candidates), keep_alive="5m", exact=True),
                           candidates)
        except llm_engine.LLMError:
            continue
    # No AI available: only an unmistakable mention in the notes counts.
    named = [w for w in candidates if _named_in(w["name"], m.get("summary") or "")]
    return named[0] if len(named) == 1 else None


def auto_sort(meeting_id: int, cfg: dict, pid: str | None = None) -> None:
    """After a meeting's notes are written: sort it, if it isn't in a workspace yet."""
    m = db.get_meeting(meeting_id)
    spaces = db.list_workspaces()
    if not m or m.get("workspace_id") or not spaces or not cfg.get("auto_workspace"):
        return
    chosen = choose(m, spaces, cfg, pid)
    if chosen and not (db.get_meeting(meeting_id) or {}).get("workspace_id"):  # not placed by hand meanwhile
        db.update_meeting(meeting_id, workspace_id=chosen["id"], workspace_auto=1)


# ── Sorting every unsorted meeting ──────────────────────────────────────

_lock = threading.Lock()
_job: dict = {}


def job_status() -> dict:
    with _lock:
        return dict(_job)


def sort_all(cfg: dict) -> bool:
    """Sorts every meeting that isn't in a workspace, in the background. False if already running."""
    with _lock:
        if _job.get("active"):
            return False
        ids = db.unsorted_meeting_ids()
        _job.clear()
        _job.update(active=True, total=len(ids), done=0, sorted=0)
    threading.Thread(target=_run, args=(ids, cfg), daemon=True, name="sort-workspaces").start()
    return True


def _run(ids: list[int], cfg: dict) -> None:
    spaces = db.list_workspaces()
    try:
        for meeting_id in ids:
            m = db.get_meeting(meeting_id)
            if m and not m.get("workspace_id"):
                chosen = choose(m, spaces, cfg)
                if chosen and not (db.get_meeting(meeting_id) or {}).get("workspace_id"):
                    db.update_meeting(meeting_id, workspace_id=chosen["id"], workspace_auto=1)
                    with _lock:
                        _job["sorted"] += 1
            with _lock:
                _job["done"] += 1
    finally:
        for pid in llm_engine.chain("auto", cfg):
            llm_engine.unload(pid, cfg)
        with _lock:
            _job["active"] = False
