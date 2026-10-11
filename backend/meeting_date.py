"""When did an imported meeting happen? A pasted transcript or a file often carries no date (or the day it
was downloaded), so the AI reads what was said for clues: dates and weekdays, "December 15th", "the 2026
work plan", holidays, seasons. It only suggests; the person picks the date.
"""
import json
import re
from datetime import date

import llm_engine

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec")
_CLUE = re.compile(
    rf"\b(?:{_MONTHS})\b|\b(?:19|20)\d{{2}}\b|\b\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?\b|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tonight|yesterday|tomorrow|"
    r"this (?:week|month|year)|next (?:week|month|year)|last (?:week|month|year)|"
    r"christmas|thanksgiving|new year|halloween|easter|holiday|spring|summer|fall|autumn|winter|"
    r"quarter|q[1-4]|fiscal|deadline|election)\b",
    re.IGNORECASE,
)
OPENING_LINES = 25
BUDGET = 12000

PROMPT = """Work out the date this meeting took place, from what was said in it.

Today is {today}. The meeting happened before today, often months or years before, so today's date is not \
a clue. Look for: spoken dates and weekdays; holidays and seasons; years ("our plan for 2026" is said before \
2026 starts, "what we did in 2025" near or after its end); deadlines and events described as coming up \
("the study session on December 15th" means the meeting is before December 15th) or as just past.

Reply with JSON only, no other text, listing the clues first:
{{"clues": ["short quote", "..."], "date": "YYYY-MM-DD", "sure_of": "day" or "month" or "year", \
"why": "one short sentence for the reader"}}
A date that is mentioned is rarely the meeting's own date: when the clues only show the meeting came before \
(or after) some day, pick a likely day before (or after) it, and say "month" or "year" in sure_of. Say "day" \
only when someone says what today's date is. If only the month is clear, give the most likely day in it. If \
nothing hints at a date, give "date": null.

Lines from the transcript (its opening, then every line near a mention of a time; "…" marks skipped parts):
{lines}"""


def clues(transcript: str, budget: int = BUDGET) -> str:
    """The opening lines (where people say what the meeting is) and every line mentioning a date or time,
    with the lines around it (captions break sentences across lines)."""
    lines = [ln for ln in transcript.splitlines() if ln.strip()]
    keep = set(range(min(OPENING_LINES, len(lines))))
    for i, ln in enumerate(lines):
        if _CLUE.search(ln):
            keep.update(range(max(0, i - 1), min(len(lines), i + 2)))
    out, used, last = [], 0, -1
    for i in sorted(keep):
        piece = (["…"] if i != last + 1 else []) + [lines[i]]
        cost = sum(len(p) + 1 for p in piece)
        if used + cost > budget:
            break
        out += piece
        used += cost
        last = i
    return "\n".join(out)


def _says_day(when: date, quotes: list) -> bool:
    """Whether a clue names that very day ("March 3rd", "3/3"), as opposed to a deadline or a month."""
    month = when.strftime("%B").lower()
    for q in map(str, quotes):
        q = q.lower()
        if re.search(rf"\b({month}|{month[:3]})\.?\s+{when.day}(st|nd|rd|th)?\b|\b{when.month}/{when.day}\b", q):
            return True
    return False


def parse(reply: str, latest: date) -> dict:
    """{"date": "YYYY-MM-DD" | None, "sure_of": ..., "why": ...} from the AI's reply. Small models tend to put a
    meeting that never says its year in this year, even in the future: it's moved back to the last time that
    day came round. And "sure of the day" needs a clue that names the day."""
    found = re.search(r"\{.*\}", reply, re.DOTALL)
    try:
        data = json.loads(found.group(0)) if found else {}
    except json.JSONDecodeError:
        data = {}
    why = str(data.get("why") or "").strip()[:300]
    when = None
    if isinstance(data.get("date"), str):
        try:
            when = date.fromisoformat(data["date"].strip()[:10])
        except ValueError:
            when = None
    sure = data.get("sure_of") if data.get("sure_of") in ("day", "month", "year") else "month"
    if when and when > latest and when.year <= latest.year + 1:
        when = when.replace(day=min(when.day, 28)) if when.month == 2 else when  # no Feb 29 in most years
        when = when.replace(year=latest.year) if when.replace(year=latest.year) <= latest else when.replace(year=latest.year - 1)
        sure = "month"
        why = "Going by what was said, but the AI wasn't sure of the year, so check it."
    if when and not date(1990, 1, 1) <= when <= latest:
        when = None
    if when and sure == "day" and not _says_day(when, data.get("clues") or []):
        sure = "month"
    if not when:
        return {"date": None, "sure_of": None, "why": why or "Nothing in the transcript says when it was"}
    return {"date": when.isoformat(), "sure_of": sure, "why": why}


def guess(m: dict, cfg: dict) -> dict:
    """Asks the AI (the usual chain of providers) when meeting `m` happened. Raises llm_engine.LLMError."""
    latest = date.today()
    prompt = PROMPT.format(today=latest.strftime("%B %-d, %Y"), lines=clues(m["transcript"] or ""))
    reply, _ = llm_engine.run_chain("auto", cfg, lambda pid: llm_engine.generate(pid, cfg, prompt, exact=True))
    return parse(reply, latest)
