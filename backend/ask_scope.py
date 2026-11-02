"""Which meetings a question across meetings reads: the time window (asked for, or said in the question), the
sources, and the tasks taken from those meetings when the question is about promises."""

import re
from datetime import UTC, datetime, timedelta

import database as db

PER_MEETING = 3000  # characters each meeting should get at least; sets how many fit in the AI's context
MOST_MEETINGS = 20
FEWEST_MEETINGS = 3

# Time words in a question, and the window they mean. The first match wins, so "last week" beats "week".
_WINDOWS = [
    (r"\btoday\b|\bthis morning\b|\bthis afternoon\b", 1),
    (r"\byesterday\b", 2),
    (r"\blast week\b", 14),  # the week before this one, so this one and that one
    (r"\b(this|past|this past) week\b", 7),
    (r"\blast month\b", 62),
    (r"\b(this|past|this past) month\b", 31),
    (r"\blast quarter\b", 183),
    (r"\b(this|past|this past) quarter\b", 92),
    (r"\blast year\b", 731),
    (r"\b(this|past|this past) year\b", 366),
    (r"\brecent(ly)?\b|\blately\b|\bthese days\b", 14),
]
_LAST_N = re.compile(r"\b(?:last|past) (\d{1,3}) (day|week|month)s?\b")
_UNIT_DAYS = {"day": 1, "week": 7, "month": 31}

# Questions about what someone said they'd do: the tasks taken from the notes answer them best.
_ABOUT_TASKS = re.compile(r"\b(promis\w*|commit\w*|owe|on the hook|action items?|to-?dos?|tasks?|follow(?:ed)?[ -]?ups?|"
                          r"unresolved|outstanding|open items?|still open|pending|said (?:i|we|they)'?d)\b")


def days_in(question: str) -> int | None:
    """The time window the question's words ask for ("recently" is two weeks), or None for any time."""
    q = question.lower()
    if m := _LAST_N.search(q):
        return max(1, int(m[1]) * _UNIT_DAYS[m[2]])
    return next((days for pattern, days in _WINDOWS if re.search(pattern, q)), None)


def about_tasks(question: str) -> bool:
    return bool(_ABOUT_TASKS.search(question.lower()))


def since(days: int | None, now: datetime | None = None) -> str | None:
    if not days:
        return None
    start = (now or datetime.now(UTC)) - timedelta(days=days)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def room_for(context_chars: int) -> int:
    """How many meetings to read, so each still gets PER_MEETING characters."""
    return max(FEWEST_MEETINGS, min(MOST_MEETINGS, context_chars // PER_MEETING))


def pick(question: str, room: int, workspace_id: int | None = None, days: int | None = None,
         sources: list[str] | None = None) -> list[int]:
    """The meetings to read, newest first. If every meeting in scope fits, all of them; otherwise the best matches
    for the question's words, the newest of those first (so "when did we last…" finds the latest), and, inside a
    time window, the window's newest meetings to fill any room left."""
    start = since(days)
    readable = db.readable_meetings(workspace_id, start, sources)
    if len(readable) <= room:
        return [m["id"] for m in readable]
    hits = db.search(question, limit=room * 2, any_term=True, workspace_id=workspace_id, since=start, sources=sources)
    readable_ids = {m["id"] for m in readable}
    hits = sorted((h for h in hits if h["id"] in readable_ids), key=lambda h: (h["created_at"], h["id"]), reverse=True)
    ids = [h["id"] for h in hits[:room]]
    if days or not ids:  # no words matched anywhere: the newest few
        ids += [m["id"] for m in readable if m["id"] not in ids][: room - len(ids) if days else min(5, room)]
    order = {m["id"]: i for i, m in enumerate(readable)}
    return sorted(ids, key=order.__getitem__)


def tasks_block(workspace_id: int | None, days: int | None, sources: list[str] | None, limit: int) -> tuple[str, list[int]]:
    """The tasks taken from the meetings in scope, newest meeting first, as a block for the AI, and the meetings
    they come from (so their [M12] citations link). Empty when there are none."""
    in_scope = {m["id"] for m in db.readable_meetings(workspace_id, since(days), sources)}
    lines, used = [], []
    for t in db.all_tasks():  # open ones first, then newest meeting first
        if t["meeting_id"] not in in_scope:
            continue
        local = datetime.fromisoformat(t["meeting_created_at"].replace("Z", "+00:00")).astimezone()
        line = f"- [M{t['meeting_id']}] {local:%b} {local.day}{' (done)' if t['done'] else ''}: {t['text']}"
        if sum(len(x) + 1 for x in lines) + len(line) > limit:
            break
        lines.append(line)
        if t["meeting_id"] not in used:
            used.append(t["meeting_id"])
    if not lines:
        return "", []
    head = 'TASKS TAKEN FROM THESE MEETINGS (each starts with its owner\'s name; "(done)" = ticked off):'
    return head + "\n" + "\n".join(lines), used
