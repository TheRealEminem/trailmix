"""Summary templates: what the notes should look like for different kinds of meetings."""

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


def notes_prompt(chunk: str, part: int, parts: int, about: str) -> tuple[str, str]:
    """(system, prompt) for notes on one part of a transcript too long to summarize in one go."""
    return NOTES_SYSTEM, f"""{about}
This is part {part} of {parts} of the transcript.

TRANSCRIPT (PART {part} OF {parts}):
{chunk}

---
Write notes on this part of the meeting. For every topic discussed, starting with its timestamp: the
specifics (who said what, names, numbers, dates, tools, places) and where it landed. Then list any
decisions, action items (who, what, by when) and open questions from this part. Bullets only. Only use what
is in the transcript.
"""


def final_prompt(template_id: str, custom: str, material: str, from_notes: bool, moments: list[str],
                 about: str, minutes: float) -> tuple[str, str]:
    """(system, prompt) for the meeting notes. The material comes first and the instructions after it,
    so they're the last thing the model reads (and never the part a too-long prompt loses)."""
    flagged = ""
    if moments:
        flagged = (
            "\n\nThe listener flagged these moments as important. Make sure the notes cover them, and add a "
            '"## Flagged Moments" section just before Action Items with one bullet per moment, starting with '
            "its timestamp:\n" + "\n".join(moments)
        )
    label = "NOTES TAKEN DURING THE MEETING, IN ORDER" if from_notes else "TRANSCRIPT"
    source = "the notes above" if from_notes else "the transcript above"
    combine = (" They cover the meeting in order: combine them into one set of notes for the whole meeting, and"
               " don't mention parts or sections." if from_notes else "")
    return FINAL_SYSTEM, f"""{about}

{label}:
{material}

---
Write the notes for this meeting from {source}.{combine} {_shape(template_id, custom)}

{ACTION_RULE}
{_length(minutes)} Only use information from {source}. Do not invent names, dates, or facts.{flagged}
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
