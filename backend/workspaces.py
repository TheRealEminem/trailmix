"""Organizing meetings: topic tags, workspaces (a job, a committee, personal life…), and suggesting workspaces.

Tags: a few short topics per meeting from its notes ("vaccine cold chain", "city council"), made by the summary
AI. They're searchable, help sorting, and are what workspace suggestions are drawn from.

Sorting: a meeting whose title names a workspace goes there. Otherwise the summary AI is shown the workspaces
(what you wrote about each, and meetings certainly in each as examples) with the meeting's notes and tags, and
picks the best fit; only a fit it calls good counts. Anything else stays in Default. Meetings you placed by
hand are never moved.

Each meeting records which model tagged and sorted it, so a better model can redo the work (model_ranks.py).
"""
import json
import re
import threading

import database as db
import llm_engine
import questions

NOTES_CHARS = 2500  # how much of the notes (or transcript) the AI sees: the overview is what matters
EXAMPLES = 4        # meetings certainly in each workspace shown to the AI (yours first): small models need them
MAX_TAGS = 6


def _named_in(name: str, text: str) -> bool:
    """The name as a word or words, however it's spaced: "Cold Connect" also finds "ColdConnect"."""
    spaced = r"[\s_-]*".join(re.escape(w) for w in name.split())
    return bool(re.search(r"(?<!\w)" + spaced + r"(?!\w)", text, re.I))


def _material(m: dict) -> str:
    # The notes without a Flagged Moments list (older notes can have long invented ones).
    return llm_engine.place_moments(m.get("summary") or m.get("transcript") or "", [])[:NOTES_CHARS]


def tags_of(m: dict) -> list[str]:
    try:
        return json.loads(m["tags_json"]) if m.get("tags_json") else []
    except ValueError:
        return []


def _ask(cfg: dict, prompt: str, pid: str | None) -> tuple[str, str] | None:
    """(answer, model) from the given provider, or your chain; None if no AI could answer."""
    for provider in [pid] if pid else llm_engine.chain("auto", cfg):
        try:
            answer = llm_engine.generate(provider, cfg, prompt, keep_alive="5m", exact=True)
            return answer, llm_engine.model_for(provider, cfg)
        except llm_engine.LLMError:
            continue
    return None


# ── Tags ────────────────────────────────────────────────────────────────

def _tags_prompt(m: dict) -> str:
    return f"""MEETING: {m['title']}
{_material(m)}

---
List 3 to {MAX_TAGS} short topic tags for this meeting: what it was about, the project, organization or
activity (e.g. "vaccine cold chain", "investor pitch", "city council", "tabletop game"). Lowercase, one to
three words each. Reply with the tags on one line, separated by commas, and nothing else."""


def _parse_tags(text: str) -> list[str]:
    line = next((ln for ln in text.strip().splitlines() if ln.strip()), "")
    line = re.sub(r"^(tags?\s*:)", "", line, flags=re.I)
    out = []
    for raw in re.split(r"[,;]", line):
        tag = re.sub(r"[\s_]+", " ", raw.strip(" .#*\"'`-[]").lower()).strip()
        if tag and len(tag) <= 40 and len(tag.split()) <= 4 and tag not in out:
            out.append(tag)
    return out[:MAX_TAGS]


def tag(meeting_id: int, cfg: dict, pid: str | None = None) -> list[str]:
    """Tags a meeting from its notes. Returns the tags (empty if no AI could)."""
    m = db.get_meeting(meeting_id)
    if not m or not (m.get("summary") or m.get("transcript")):
        return []
    got = _ask(cfg, _tags_prompt(m), pid)
    if not got:
        return []
    tags = _parse_tags(got[0])
    if tags:
        db.update_meeting(meeting_id, tags_json=tags, tags_model=got[1])
    return tags


# ── Sorting ─────────────────────────────────────────────────────────────

def _examples(space: dict, leave_out: int) -> list[str]:
    """Meetings certainly in this workspace: placed there by hand, or named after it. Never ones the AI
    sorted, or one early guess would pull in more and more like it."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT title, workspace_auto FROM meetings WHERE workspace_id = ? AND id != ?
               ORDER BY workspace_auto, created_at DESC""", (space["id"], leave_out)).fetchall()
    sure = [r["title"] for r in rows if not r["workspace_auto"] or _named_in(space["name"], r["title"])]
    return sure[:EXAMPLES]


def _sort_prompt(m: dict, spaces: list[dict]) -> str:
    listing = []
    for w in spaces:
        line = f"- {w['name']}" + (f": {w['about']}" if w["about"] else "")
        examples = _examples(w, m["id"])
        if examples:
            line += "\n  Meetings already in it: " + "; ".join(f'"{t}"' for t in examples)
        listing.append(line)
    tags = tags_of(m)
    return f"""MEETING: {m['title']}
{"Topics: " + ", ".join(tags) if tags else ""}
{_material(m)}

---
WORKSPACES:
{chr(10).join(listing)}

Pick the workspace this meeting fits best. Reply with two lines and nothing else:
Workspace: <its name, exactly as listed>
Fit: <good, partial, or poor>"""


def _answer(text: str, candidates: list[dict]) -> tuple[dict | None, dict | None]:
    """(the workspace named on the "Workspace:" line if the model says it fits well, or None; the one it
    named if the fit was only partial, to ask you about). "Partial" fits were mostly wrong in testing (e.g.
    tabletop games filed under a startup), so they're asked about rather than acted on."""
    named = re.search(r"workspace:\s*(.+)", text, re.I)
    fit = re.search(r"fit:\s*(\w+)", text, re.I)
    if not named or not fit:
        return None, None
    said = named.group(1).strip(" .*\"'`")
    exact = [w for w in candidates if w["name"].lower() == said.lower()]
    mentioned = [w for w in candidates if _named_in(w["name"], said)]
    space = exact[0] if exact else mentioned[0] if len(mentioned) == 1 else None  # a name it made up: no
    level = fit.group(1).lower()
    return (space, None) if level == "good" else (None, space if level == "partial" else None)


def choose(m: dict, spaces: list[dict], cfg: dict, pid: str | None = None) -> tuple[dict | None, str | None, dict | None]:
    """(the workspace a meeting belongs in or None, the model that decided or None if its title did, a
    workspace it only partly fits, to ask you about)."""
    if not spaces:
        return None, None, None
    titled = [w for w in spaces if _named_in(w["name"], m["title"])]
    if len(titled) == 1:
        return titled[0], None, None
    candidates = titled or spaces
    got = _ask(cfg, _sort_prompt(m, candidates), pid)
    if got:
        space, maybe = _answer(got[0], candidates)
        return space, got[1], maybe
    # No AI available: only an unmistakable mention in the notes counts.
    named = [w for w in candidates if _named_in(w["name"], m.get("summary") or "")]
    return (named[0] if len(named) == 1 else None), None, None


def _place(meeting_id: int, spaces: list[dict], cfg: dict, pid: str | None = None) -> bool:
    """Sorts one meeting (unless you placed it by hand). True if it went into a workspace."""
    m = db.get_meeting(meeting_id)
    if not m or (m.get("workspace_id") and not m.get("workspace_auto")):
        return False
    chosen, model, maybe = choose(m, spaces, cfg, pid)
    if maybe and not chosen:
        questions.ask_workspace(meeting_id, maybe)
    now = db.get_meeting(meeting_id) or {}
    if now.get("workspace_id") and not now.get("workspace_auto"):  # you placed it meanwhile
        return False
    db.update_meeting(meeting_id, workspace_id=chosen["id"] if chosen else None, workspace_auto=1 if chosen else 0,
                      workspace_model=model)
    return chosen is not None


def after_notes(meeting_id: int, cfg: dict, pid: str | None = None) -> None:
    """Once a meeting's notes are written: tag it, then sort it if it's still in Default."""
    if cfg.get("auto_workspace", True):
        tag(meeting_id, cfg, pid)
    spaces = db.list_workspaces()
    m = db.get_meeting(meeting_id)
    if spaces and m and not m.get("workspace_id") and cfg.get("auto_workspace"):
        _place(meeting_id, spaces, cfg, pid)
    questions.ask_about(meeting_id, cfg, pid)  # anything the AI wasn't sure of: names, misheard words


# ── Suggesting workspaces ───────────────────────────────────────────────

def _suggest_prompt(counts: list[tuple[str, int]], titles: list[str], existing: list[str]) -> str:
    topics = "\n".join(f"- {t} ({n})" for t, n in counts)
    have = f"\nThey already have these workspaces, so suggest others: {', '.join(existing)}." if existing else ""
    return f"""These are the topics of someone's recorded meetings, with how many meetings each came up in:
{topics}

Some of the meetings: {"; ".join(titles)}

---
Group these meetings into 2 to 6 workspaces: the separate parts of this person's life or work, such as a
company they work on, a committee or group they belong to, school, or personal life. Name each workspace after
the real organization or project when the topics show one (for example a company name), otherwise a short plain
name like "Personal".{have}

Reply with one workspace per line, the name, then " | ", then what goes in it in under 15 words. For example:
Acme Robotics | the startup: product, investors, hiring
Book Club | monthly book discussions with friends"""


def suggest(cfg: dict) -> list[dict]:
    """Workspace ideas from the tags of all your meetings: [{name, about}]."""
    counts: dict[str, int] = {}
    titles = []
    with db.connect() as conn:
        rows = conn.execute("SELECT title, tags_json FROM meetings ORDER BY created_at DESC").fetchall()
    for r in rows:
        titles.append(r["title"])
        for t in json.loads(r["tags_json"]) if r["tags_json"] else []:
            counts[t] = counts.get(t, 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:60]
    if not top:
        return []
    existing = [w["name"] for w in db.list_workspaces()]
    got = _ask(cfg, _suggest_prompt(top, titles[:40], existing), None)
    if not got:
        return []
    out = []
    examples = {"acme robotics", "book club", "name", "workspace"}  # echoes of the prompt's example
    for line in got[0].splitlines():
        found = re.match(r"^\s*(?:[-*\d.)]+\s*)?\**([^|*]{2,40}?)\**\s*\|\s*(.+)$", line)
        if not found:
            continue
        name, about = found.group(1).strip(), found.group(2).strip()
        if name.lower() in {e.lower() for e in existing} | examples or name.lower() == "default":
            continue
        if name.lower() not in {o["name"].lower() for o in out}:
            out.append({"name": name, "about": about[:200]})
    return out[:6]


# ── Background jobs: sort, re-sort, tag, suggest ────────────────────────

_lock = threading.Lock()
_job: dict = {}


def job_status() -> dict:
    with _lock:
        return dict(_job)


def start(kind: str, cfg: dict, only_older: bool = False) -> bool:
    """Runs one organizing job in the background. False if one is already running.

    sort:    meetings in Default
    resort:  every meeting not placed by hand (e.g. with a better model, or new workspaces)
    tag:     meetings without tags (only_older: also those tagged by a weaker model than yours now)
    suggest: tags meetings that have none, then suggests workspaces
    """
    import model_ranks

    with _lock:
        if _job.get("active"):
            return False
        with db.connect() as conn:
            rows = conn.execute(
                """SELECT id, workspace_id, workspace_auto, tags_json, tags_model FROM meetings
                   WHERE status != 'recording' AND (summary IS NOT NULL OR transcript IS NOT NULL)
                   ORDER BY created_at DESC""").fetchall()
        current = llm_engine.model_for(llm_engine.chain("auto", cfg)[0], cfg)
        if kind == "sort":
            ids = [r["id"] for r in rows if not r["workspace_id"]]
        elif kind == "resort":
            ids = [r["id"] for r in rows if not r["workspace_id"] or r["workspace_auto"]]
        elif kind in ("tag", "suggest"):
            ids = [r["id"] for r in rows
                   if not r["tags_json"] or (only_older and model_ranks.better(current, r["tags_model"]))]
        else:
            raise ValueError(kind)
        _job.clear()
        _job.update(active=True, kind=kind, total=len(ids), done=0, sorted=0, model=current, suggestions=None)
    threading.Thread(target=_run, args=(kind, ids, cfg), daemon=True, name=f"organize-{kind}").start()
    return True


def _run(kind: str, ids: list[int], cfg: dict) -> None:
    spaces = db.list_workspaces()
    try:
        for meeting_id in ids:
            if kind in ("tag", "suggest"):
                tag(meeting_id, cfg)
            elif _place(meeting_id, spaces, cfg):
                with _lock:
                    _job["sorted"] += 1
            with _lock:
                _job["done"] += 1
        if kind == "suggest":
            ideas = suggest(cfg)
            with _lock:
                _job["suggestions"] = ideas
    except Exception as e:  # never leave the job "active" forever
        with _lock:
            _job["error"] = str(e)[:200]
    finally:
        for pid in llm_engine.chain("auto", cfg):
            llm_engine.unload(pid, cfg)
        with _lock:
            _job["active"] = False
