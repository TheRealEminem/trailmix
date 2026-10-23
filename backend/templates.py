"""Summary templates: what the notes should look like for different kinds of meetings."""

import re

ACTION_RULE = (
    'Always end with a "## Action Items" section: one bullet per task, written as '
    '"- [ ] <person\'s name>: <task> (<due date, if one was mentioned>)", for example '
    '"- [ ] Dana: fix the login bug (Wednesday)". Use "Unassigned" when nobody took it on. '
    'Write "- None" if there were no action items.'
)

TEMPLATES = {
    "general": {
        "name": "General meeting",
        "sections": """## Overview
Two to four sentences: what the meeting was for and what came out of it.

## Discussion
One "### " subheading per topic, in the order they came up. Under each, bullets with the specifics: what
was said and by whom, names, numbers, dates, tools, and where the topic landed.

## Decisions
Bullet list of decisions made (write "None" if there were none).

## Open Questions
Questions left unanswered and things someone needs to find out (write "None" if there were none).""",
    },
    "one_on_one": {
        "name": "1:1",
        "sections": """## Overview
One or two sentences on the tone and main themes.

## Updates & Wins
What's going well, progress since last time.

## Challenges & Blockers
Anything that's hard, stuck, or worrying.

## Feedback
Feedback given in either direction (write "None" if there was none).

## Growth & Career
Goals, development, or career topics discussed (write "None" if not discussed).""",
    },
    "standup": {
        "name": "Standup",
        "sections": """## Overview
One sentence on the state of the team.

## By Person
For each person: a sub-bullet each for done, doing next, and blockers.

## Blockers
Every blocker raised, and who can unblock it.""",
    },
    "interview": {
        "name": "Interview",
        "sections": """## Overview
The role and a two-sentence impression of the candidate.

## Strengths
Evidence-backed strengths, citing what the candidate said.

## Concerns
Gaps, risks, or unclear answers.

## Notable Answers
Short summaries of the most informative answers.

## Recommendation
Hire / no hire / needs another round, with a one-line reason. Say if the transcript isn't enough to judge.""",
    },
    "sales": {
        "name": "Sales call",
        "sections": """## Overview
Who the customer is and what the call was about.

## Needs & Pain Points
What they're trying to solve, in their words where possible.

## Objections & Concerns
Hesitations, competitors mentioned, risks to the deal.

## Budget, Timeline & Decision Makers
Whatever was said about money, dates, and who decides (write "Not discussed" if nothing).""",
    },
    "minutes": {
        "name": "Board or committee minutes",
        "sections": """## Overview
The body that met, what the meeting was for, and who chaired or attended, as far as the transcript says.

## Agenda Items
For each item discussed, a short ### heading, then what was presented, the main points raised, and where it landed.

## Motions & Votes
Each motion: who moved and seconded it (if said), what it proposed, and the result (carried, failed or tabled, with
the vote count if given). Write "None" if there were no motions.

## Decisions
What the group agreed, one bullet each.""",
    },
    "custom": {"name": "Custom (from Settings)", "sections": ""},
}


def listing() -> list[dict]:
    return [{"id": k, "name": v["name"]} for k, v in TEMPLATES.items()]


NOTES_SYSTEM = """You take detailed, factual notes on one part of a meeting transcript. Reply with the notes only:
no introduction, no commentary about the transcript, no closing remarks."""

FINAL_SYSTEM = """You write meeting notes in Markdown. Reply with the notes only, starting with the first
heading: never describe the transcript, never address the reader, no introduction or closing remarks."""


def _shape(template_id: str, custom: str) -> str:
    t = TEMPLATES.get(template_id) or TEMPLATES["general"]
    if template_id == "custom" and custom.strip():
        return custom.strip()
    return "Use these sections:\n\n" + (t["sections"] or TEMPLATES["general"]["sections"])


def _length(minutes: float) -> str:
    if minutes < 8:
        return "It was a short meeting: keep the notes short."
    if minutes < 25:
        return "Be concise but complete."
    return "It was a long meeting: be thorough, and cover every topic that took more than a minute or two."


def notes_prompt(chunk: str, part: int, parts: int | None, about: str) -> tuple[str, str]:
    """(system, prompt) for notes on one part of a transcript too long to summarize in one go. `parts` is
    None for notes written during the meeting (live_notes.py), when nobody knows yet how long it will run."""
    of = f" OF {parts}" if parts else ""
    where = f"part {part} of {parts}" if parts else f"part {part}, written while the meeting is still going"
    return NOTES_SYSTEM, f"""{about}
This is {where} of the transcript.

TRANSCRIPT (PART {part}{of}):
{chunk}

---
Summarize this part of the meeting in your own words. Do not copy lines from the transcript. For each
topic discussed: one bullet starting with its timestamp, then two or three sub-bullets with the specifics
that matter (names, numbers, dates, tools, places) and where it landed. Then list any decisions, action
items (who, what, by when) and open questions from this part. At most about 25 bullets in all. Only use
what is in the transcript.
"""


_MY_HEADING = re.compile(r"^\s*(?:\[[\d:]+\]\s*)?#{1,6}\s")


def is_heading(line: str) -> bool:
    """Whether one of your typed note lines ("[12:34] # Budget", "## Plan") is a heading."""
    return bool(_MY_HEADING.match(line))


def _outline(mine: list[str], shape: str, source: str) -> str:
    """How the notes are built around the ones you typed (as Granola does): your notes are the outline, with your
    headings as the sections, and the template only shapes what you didn't cover."""
    if any(is_heading(line) for line in mine):
        sections = ("Keep their headings, in their order and in their words, as your sections (a \"#\" or \"##\" "
                    "heading becomes a \"## \" section, a \"###\" heading a \"### \" subsection). Under each, ")
    else:
        sections = ("They have no headings: group their lines under \"## \" topic headings of your own, in the "
                    "order they wrote them. Under each, ")
    return (f"""Use MY NOTES as the outline of your notes: they are what the person recording most wanted remembered.
{sections}turn each of their lines into a clear bullet in the same place and order (nested lines stay nested), then add
the detail from {source} beneath it as sub-bullets: what was said and by whom, names, numbers, dates, and where it
landed. Keep their numbers, names and links exactly, and write in the third person ("Mark liked…", not "I liked…").
After their sections, add what else is worth noting that their notes don't cover, using these sections where they
fit (leave out any that would be empty or only repeat theirs):

{shape.removeprefix("Use these sections:").strip()}""")


def final_prompt(template_id: str, custom: str, material: str, from_notes: bool, moments: list[str],
                 about: str, minutes: float, mine: list[str] = ()) -> tuple[str, str]:
    """(system, prompt) for the meeting notes. The material comes first and the instructions after it,
    so they're the last thing the model reads (and never the part a too-long prompt loses). `mine`: the notes
    you typed yourself (lines of markdown), which become the notes' outline."""
    own, own_rule = "", ""
    if mine:
        own = ("\n\nMY NOTES, TYPED BY THE PERSON WHO RECORDED THE MEETING (\"I\" is them; a [time] is when a line "
               "was typed):\n" + "\n".join(mine))
        own_rule = ("\nNever say anything came from typed notes or MY NOTES, and never give a point a time it "
                    "doesn't have.")
    flagged = ""
    if moments:
        # Only "cover them": the Flagged Moments section itself is written by Trailmix from the real flags
        # (llm_engine.place_moments). Asked to write it, small models list half the meeting as "flagged".
        flagged = (
            "\n\nThe listener flagged these moments as important; make sure the notes cover what was said "
            "there. Don't write a section listing them:\n" + "\n".join(moments)
        )
    label = "NOTES TAKEN DURING THE MEETING, IN ORDER" if from_notes else "TRANSCRIPT"
    source = "the notes above" if from_notes else "the transcript above"
    shape = _outline(mine, _shape(template_id, custom), source) if mine else _shape(template_id, custom)
    combine = (" They cover the meeting in order: combine them into one set of notes for the whole meeting, and"
               " don't mention parts or sections." if from_notes else "")
    return FINAL_SYSTEM, f"""{about}

{label}:
{material}{own}

---
Write the notes for this meeting from {source}.{combine} {shape}

{ACTION_RULE}
{_length(minutes)} Summarize in your own words; never copy transcript lines. Only use information from
{source}{" and MY NOTES" if mine else ""}. Do not invent names, dates, or facts.{flagged}{own_rule}
Start your reply directly with the first heading.
"""


def actions_prompt(material: str, from_notes: bool, about: str) -> tuple[str, str]:
    """(system, prompt) for just the action items, when the notes came back without them."""
    source = "notes" if from_notes else "transcript"
    return FINAL_SYSTEM, f"""{about}

{source.upper()}:
{material}

---
List the action items from this meeting's {source}: things someone said they would do, or was asked to do.
Reply with only this section:

## Action Items
- [ ] <person's name>: <task> (<due date, if one was mentioned>)

{ACTION_RULE}
"""
