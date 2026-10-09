"""Quick questions: when the AI isn't sure about something in a meeting, it asks you, and your answer fixes it.

After a meeting's notes and tags are written, Trailmix may ask (at most MAX_OPEN per meeting):
  them      who was on the other side of the call (the people the notes mention, or ones you named before)
  spelling  whether a name is a misspelling of one you've confirmed ("Jef" → "Jeff")
  term      whether a word was misheard ("Cooper Netties" → "Kubernetes"), as the AI suspects
  workspace whether the meeting belongs in a workspace it only partly fits
Answers fix the transcript, notes, tasks and title, or the speaker name, or the workspace. Names and terms you
confirm join your vocabulary, which the speech model hears at the start of each recording and the notes AI
is told about, so they come out right next time. A question you dismiss isn't asked again.
"""
import json
import re
from difflib import SequenceMatcher

import database as db
import llm_engine

MAX_OPEN = 3
NAME = re.compile(r"\b[A-ZÀ-ɏ][a-zÀ-ɏ'’\-]{2,}(?:[ \t]+[A-ZÀ-ɏ][a-zÀ-ɏ'’\-]{2,})?\b")
NOT_NAMES = {"The", "This", "That", "They", "There", "Then", "Thanks", "Thank", "Okay", "Yeah", "Yes", "Monday",
             "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March",
             "April", "June", "July", "August", "September", "October", "November", "December", "Overview",
             "Discussion", "Decisions", "Action", "Items", "Open", "Questions", "Flagged", "Moments", "None",
             "Unassigned", "What", "When", "Where", "Who", "Why", "How", "And", "But", "So", "We", "You", "Them"}


# ── Storage ─────────────────────────────────────────────────────────────

def _ensure_table() -> None:
    with db.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            key TEXT NOT NULL,                -- what it's about, so it's never asked twice
            prompt TEXT NOT NULL,
            options_json TEXT NOT NULL,       -- [{"label", "value"}]
            free_text INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',  -- open | answered | dismissed
            answer TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (meeting_id, kind, key))""")


def add(meeting_id: int, kind: str, key: str, prompt: str, options: list[dict], payload: dict,
        free_text: bool = False) -> bool:
    """Asks, unless this exact question was asked before (answered or dismissed) or enough are open."""
    _ensure_table()
    with db.connect() as conn:
        open_now = conn.execute("SELECT count(*) FROM questions WHERE meeting_id = ? AND status = 'open'",
                                (meeting_id,)).fetchone()[0]
        if open_now >= MAX_OPEN:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO questions (meeting_id, kind, key, prompt, options_json, free_text, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (meeting_id, kind, key.lower(), prompt, json.dumps(options), int(free_text), json.dumps(payload)))
        return cur.rowcount > 0


def open_for(meeting_id: int) -> list[dict]:
    _ensure_table()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM questions WHERE meeting_id = ? AND status = 'open' ORDER BY id",
                            (meeting_id,)).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "prompt": r["prompt"], "options": json.loads(r["options_json"]),
             "free_text": bool(r["free_text"])} for r in rows]


def open_counts() -> dict[int, int]:
    """meeting id → open questions, for the sidebar."""
    _ensure_table()
    with db.connect() as conn:
        rows = conn.execute("SELECT meeting_id, count(*) FROM questions WHERE status = 'open' GROUP BY meeting_id")
        return {r[0]: r[1] for r in rows}


# ── Vocabulary ──────────────────────────────────────────────────────────

def vocabulary() -> list[str]:
    import settings

    return list(settings.get_all().get("vocabulary") or [])


def learn(*words: str) -> None:
    import settings

    vocab = vocabulary()
    for w in words:
        w = w.strip()
        if w and w.lower() not in {v.lower() for v in vocab}:
            vocab.append(w)
    settings.update({"vocabulary": vocab[-200:]})


def vocabulary_prompt() -> str:
    """For the speech model's first words and the notes AI: names and terms spelled the way you confirmed."""
    vocab = vocabulary()
    return ("Names and terms: " + ", ".join(vocab[-60:]) + ".") if vocab else ""


# ── Fixing a meeting ────────────────────────────────────────────────────

def _swap(text: str | None, old: str, new: str) -> str | None:
    if not text:
        return text
    return re.sub(rf"(?<!\w){re.escape(old)}(?!\w)", new, text)


def replace_everywhere(meeting_id: int, old: str, new: str) -> None:
    """A word or name, corrected in the meeting's transcript, notes, tasks and title."""
    m = db.get_meeting(meeting_id)
    if not m:
        return
    segments = json.loads(m["segments_json"]) if m.get("segments_json") else None
    if segments:
        for s in segments:
            s["text"] = _swap(s["text"], old, new)
    db.update_meeting(meeting_id, transcript=_swap(m.get("transcript"), old, new), summary=_swap(m.get("summary"), old, new),
                      title=_swap(m["title"], old, new), segments_json=segments)
    with db.connect() as conn:
        for t in conn.execute("SELECT id, text FROM action_items WHERE meeting_id = ?", (meeting_id,)).fetchall():
            fixed = _swap(t["text"], old, new)
            if fixed != t["text"]:
                conn.execute("UPDATE action_items SET text = ? WHERE id = ?", (fixed, t["id"]))
        # Other questions still waiting about this meeting (e.g. the choices for who was on the call).
        _ensure_table()
        for q in conn.execute("SELECT id, prompt, options_json FROM questions WHERE meeting_id = ? AND status = 'open'",
                              (meeting_id,)).fetchall():
            conn.execute("UPDATE questions SET prompt = ?, options_json = ? WHERE id = ?",
                         (_swap(q["prompt"], old, new), _swap(q["options_json"], old, new), q["id"]))


def answer(question_id: int, value: str) -> int:
    """Applies your answer. Returns the meeting id."""
    _ensure_table()
    with db.connect() as conn:
        q = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not q:
        raise KeyError(question_id)
    q, payload, value = dict(q), json.loads(q["payload_json"]), value.strip()
    meeting_id = q["meeting_id"]
    if q["kind"] == "them" and value and value != "several":
        m = db.get_meeting(meeting_id)
        names = json.loads(m["speaker_names_json"]) if m and m.get("speaker_names_json") else {}
        db.update_meeting(meeting_id, speaker_names_json={**names, "Them": value})
        learn(value)
    elif q["kind"] in ("spelling", "term"):
        if value == "yes":
            replace_everywhere(meeting_id, payload["written"], payload["meant"])
            learn(payload["meant"])
        elif value == "no":
            learn(payload["written"])  # it was right as written: a real name or word to remember
        elif value:  # typed the right spelling
            replace_everywhere(meeting_id, payload["written"], value)
            learn(value)
    elif q["kind"] == "workspace" and value.isdigit() and db.get_workspace(int(value)):
        db.update_meeting(meeting_id, workspace_id=int(value), workspace_auto=0, workspace_model=None)
    with db.connect() as conn:
        conn.execute("UPDATE questions SET status = 'answered', answer = ? WHERE id = ?", (value, question_id))
    return meeting_id


def dismiss(question_id: int) -> None:
    _ensure_table()
    with db.connect() as conn:
        conn.execute("UPDATE questions SET status = 'dismissed' WHERE id = ?", (question_id,))


# ── Asking ──────────────────────────────────────────────────────────────

def _ask_ai(cfg: dict, prompt: str, pid: str | None) -> str:
    for provider in [pid] if pid else llm_engine.chain("auto", cfg):
        try:
            return llm_engine.generate(provider, cfg, prompt, keep_alive="5m", exact=True)
        except llm_engine.LLMError:
            continue
    return ""


PEOPLE_PROMPT = """MEETING NOTES:
{notes}

---
Who took part in this meeting or was mentioned by name? List people's names exactly as written in the notes
(first names, or first and last), separated by commas. Only people, not companies or places. If none, reply
None."""

TERMS_PROMPT = """These meeting notes were written from an automatic transcript, which sometimes mishears names,
companies, products and jargon.
{vocab}
MEETING NOTES:
{notes}

---
List up to 3 words or names in the notes that are probably transcription mistakes, with what was most likely
said, one per line as: written → meant
Only list ones you're fairly sure about, never ordinary words. If there are none, reply None."""


def _people(m: dict, cfg: dict, pid: str | None) -> list[str]:
    reply = _ask_ai(cfg, PEOPLE_PROMPT.format(notes=(m.get("summary") or "")[:5000]), pid)
    if not reply or reply.strip().lower().startswith("none"):
        return []
    out = []
    for raw in re.split(r"[,\n]", reply):
        name = raw.strip(" .*-•\t\"'")
        if 2 < len(name) <= 40 and name[0].isupper() and len(name.split()) <= 3 and name not in out:
            out.append(name)
    return out[:8]


def _known_people() -> list[str]:
    """Names you've confirmed: your vocabulary and the speaker names you've set (most used first)."""
    counts: dict[str, int] = {}
    with db.connect() as conn:
        for (names,) in conn.execute("SELECT speaker_names_json FROM meetings WHERE speaker_names_json IS NOT NULL"):
            try:
                for v in json.loads(names).values():
                    if isinstance(v, str) and v.strip():
                        counts[v.strip()] = counts.get(v.strip(), 0) + 1
            except ValueError:
                pass
    for v in vocabulary():
        counts[v] = counts.get(v, 0) + 1
    return sorted(counts, key=lambda n: -counts[n])


def _sound(word: str) -> str:
    """A rough sound skeleton: consonants only, with letters that sound alike folded together, so misheard
    words ("Cooper Netties" / "Kubernetes") look alike and different ideas don't."""
    w = re.sub(r"[^a-z]", "", word.lower())
    for a, b in (("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("z", "s"), ("x", "ks"), ("v", "f")):
        w = w.replace(a, b)
    w = re.sub(r"[aeiouyhw]", "", w)
    return re.sub(r"(.)\1+", r"\1", w)  # doubled letters count once


def sounds_alike(a: str, b: str) -> bool:
    x, y = _sound(a), _sound(b)
    return bool(x and y) and SequenceMatcher(None, x, y).ratio() >= 0.6


def _close(a: str, b: str) -> bool:
    """One or two letters apart ("Jef"/"Jeff", "Priya"/"Pria"), and not the same."""
    a_, b_ = a.lower(), b.lower()
    return a_ != b_ and min(len(a_), len(b_)) >= 3 and SequenceMatcher(None, a_, b_).ratio() >= 0.8


def ask_about(meeting_id: int, cfg: dict, pid: str | None = None) -> int:
    """Looks for things worth asking about in a meeting whose notes were just written. Returns how many it asked."""
    import settings

    m = db.get_meeting(meeting_id)
    if not m or not m.get("summary") or not cfg.get("ask_questions", True):
        return 0
    asked = 0
    me = settings.get_all().get("your_name", "").strip()
    people = [p for p in _people(m, cfg, pid) if p.lower() != me.lower() and p.split()[0] not in NOT_NAMES]
    known = _known_people()
    names = json.loads(m["speaker_names_json"]) if m.get("speaker_names_json") else {}

    # Who was on the other side of the call?
    if m.get("has_system") and not names.get("Them"):
        choices = list(dict.fromkeys(people + [k for k in known if k.lower() != me.lower()]))[:4]
        if choices:
            options = [{"label": c, "value": c} for c in choices] + [{"label": "Several people", "value": "several"}]
            asked += add(meeting_id, "them", "them", "Who was on the other side of this call?", options, {},
                         free_text=True)

    # A name one or two letters off one you've confirmed.
    words = set(people) | {w for w in NAME.findall(m["summary"]) if w.split()[0] not in NOT_NAMES}
    for written in sorted(words):
        match = next((k for k in known if _close(written, k)), None)
        if match and asked < MAX_OPEN:
            asked += add(meeting_id, "spelling", written, f"Is “{written}” the same as “{match}”?",
                         [{"label": f"Yes, it's {match}", "value": "yes"}, {"label": f"No, {written} is right", "value": "no"}],
                         {"written": written, "meant": match})

    # Words the AI thinks were misheard.
    if asked < MAX_OPEN:
        vocab = vocabulary_prompt()
        reply = _ask_ai(cfg, TERMS_PROMPT.format(notes=m["summary"][:5000], vocab=(vocab + "\n") if vocab else ""), pid)
        for line in reply.splitlines():
            found = re.match(r"^\s*[-*\d.)]*\s*[\"“]?(.+?)[\"”]?\s*(?:→|->|=>)\s*[\"“]?(.+?)[\"”]?\s*$", line)
            if not found:
                continue
            written, meant = found.group(1).strip(), found.group(2).strip()
            if (written.lower() == meant.lower() or len(written) > 40 or len(meant) > 40
                    or not sounds_alike(written, meant)  # a mishearing sounds alike; otherwise it's a rewrite
                    or not re.search(rf"(?<!\w){re.escape(written)}(?!\w)", m["summary"] + (m.get("transcript") or ""))):
                continue  # not actually in the meeting, or not a real change
            asked += add(meeting_id, "term", written, f"Where it says “{written}”, did they say “{meant}”?",
                         [{"label": f"Yes, “{meant}”", "value": "yes"}, {"label": "No, it's right", "value": "no"}],
                         {"written": written, "meant": meant}, free_text=True)
    return asked


def ask_workspace(meeting_id: int, maybe: dict) -> bool:
    """The sorter thought this meeting partly fits a workspace: ask rather than guess."""
    others = [w for w in db.list_workspaces() if w["id"] != maybe["id"]][:2]
    options = [{"label": f"Yes, {maybe['name']}", "value": str(maybe["id"])}]
    options += [{"label": w["name"], "value": str(w["id"])} for w in others]
    options.append({"label": "Leave it in Default", "value": "default"})
    return add(meeting_id, "workspace", str(maybe["id"]), f"Does this meeting belong in {maybe['name']}?", options,
               {"workspace_id": maybe["id"]})
