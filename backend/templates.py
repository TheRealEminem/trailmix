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
Two or three sentences on what the meeting was about.

## Key Points
Bullet list of the main topics and conclusions.

## Decisions
Bullet list of decisions made (write "None" if there were none).""",
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


def build_prompt(template_id: str, custom: str, transcript: str, moments: list[str]) -> str:
    t = TEMPLATES.get(template_id) or TEMPLATES["general"]
    if template_id == "custom" and custom.strip():
        shape = custom.strip()
    else:
        shape = "Write the notes in Markdown with these sections:\n\n" + (t["sections"] or TEMPLATES["general"]["sections"])
    flagged = ""
    if moments:
        flagged = (
            "\n\nThe listener flagged these moments as important. Make sure the notes cover them, and add a "
            '"## Flagged Moments" section just before Action Items with one bullet per moment, starting with '
            "its timestamp:\n" + "\n".join(moments)
        )
    return f"""You are an assistant that writes concise, accurate meeting notes.
Below is a timestamped transcript of a meeting. {shape}

{ACTION_RULE}
Only use information present in the transcript. Do not invent names, dates, or facts.{flagged}

TRANSCRIPT:
{transcript}
"""
