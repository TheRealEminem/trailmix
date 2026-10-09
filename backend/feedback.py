"""Problem reports: what was on screen, what went wrong, and diagnostics, with personal information removed
on this Mac before anything leaves it. The report opens as a new GitHub issue for you to check and send.

Personal information is removed in layers, so one missing doesn't let anything through:
  1. The window's text arrives with everything personal already replaced by placeholders: the page marks its
     private parts (titles, notes, transcripts, names, tasks, workspaces…) and sends "[transcript hidden]"
     instead of their text.
  2. Patterns: keys and tokens, emails, phone numbers, addresses, links, IP addresses, the macOS user name in
     file paths.
  3. Names Trailmix knows: yours, speakers', workspaces', and meeting titles.
  4. Your local AI (never a cloud one) rewrites what you typed and anything else free-form, replacing names,
     organizations and meeting content with [name], [organization], [meeting content].
  5. The patterns and known names once more, over the AI's output.
You see the result, can edit it, and send it yourself.
"""
import json
import platform
import re
from urllib.parse import quote

import archive
import database as db
import llm_engine
import resources

REPO = "TheRealEminem/trailmix"
LOG_LINES = 25
URL_BODY_MAX = 6000  # GitHub's new-issue link carries the text; longer links fail in some browsers

# ── Patterns ────────────────────────────────────────────────────────────

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # keys and tokens: OpenAI, Anthropic, Granola, Google, GitHub, bearer tokens, long random strings
    (re.compile(r"\b(?:sk-(?:ant-|proj-)?|grn_|AIza|ghp_|gho_|github_pat_|xox[bp]-)[A-Za-z0-9_\-]{8,}"), "[key]"),
    (re.compile(r"(?i)\b(bearer|token|api[_ -]?key|passcode|password|pwd)\b(\s*[:=]\s*|\s+)\S+"), r"\1\2[key]"),
    (re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b"), "[key]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    # links, except to this Mac
    (re.compile(r"\b(?:https?|zoommtg|msteams|facetime)://(?!(?:localhost|127\.0\.0\.1)[:/])\S+", re.I), "[link]"),
    (re.compile(r"\b(?:[\w-]+\.)+(?:zoom\.us|meet\.google\.com|teams\.microsoft\.com|webex\.com)/\S*", re.I), "[link]"),
    # the macOS user name in paths
    (re.compile(r"(/Users/)(?!Shared/)[^/\s]+"), r"\1[user]"),
    # IP addresses, except this Mac's own
    (re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    # street addresses ("742 Evergreen Terrace, Springfield, OR 97477")
    (re.compile(r"\b\d{1,6}\s+(?:[A-Z][\w'.-]*\s+){1,4}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
                r"Terrace|Way|Court|Ct|Place|Pl|Highway|Hwy|Parkway|Pkwy|Circle|Cir)\b\.?(?:,\s*[A-Z][\w .'-]*){0,2}"
                r"(?:\s+\d{5}(?:-\d{4})?)?"), "[address]"),
    # phone numbers: 9+ digits with the usual separators (dates, times and versions don't qualify)
    (re.compile(r"(?<![\w:.])\+?\d(?:[\s().-]*\d){8,14}(?![\w:])"), "[phone]"),
]


# A line of transcript or notes pasted or seen anywhere ("Dana Whitfield: Sounds good, let's ship Tuesday."):
# the speaker and what they said go together. App lines like "Status: Transcribing" are short and kept.
_APP_LABELS = ("Error", "Warning", "Status", "Model", "Duration", "Version", "Engine", "Note", "Hint", "Info",
               "Debug", "Title", "Summary", "Transcript", "Problem", "Reason", "Fix", "Tip", "Meeting URL",
               "User report", "Authorization", "Active token", "Workspace", "Notes AI", "Trailmix", "macOS", "Mac")
_SAID = re.compile(
    r"(?m)^(?:\[[\d:]+\][ \t]*)?"                          # an optional [12:34]
    r"(?!(?:" + "|".join(re.escape(w) for w in _APP_LABELS) + r")[ \t]*:)"  # not the app's own "Status:" etc.
    r"[A-Z\u00C0-\u024F][\w\u00C0-\u024F.'\-]*(?:[ \t]+[\w\u00C0-\u024F.'\-]+){0,3}"  # a name, or "Speaker 1"
    r":[ \t]+(?:\S+[ \t]+){3,}\S+[^\n]*$")                # then a sentence: what they said


def scrub_patterns(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return _SAID.sub("[meeting content]", text)


# ── Names Trailmix knows ────────────────────────────────────────────────

def known_names() -> list[str]:
    """Names and words that identify you or the people and organizations in your meetings: your name, speaker
    names, workspace names, and meeting titles. Longest first, so "Dana Whitfield" goes before "Dana"."""
    import settings

    people: set[str] = set()
    mine = settings.get_all().get("your_name", "").strip()
    if mine:
        people.add(mine)
    found: set[str] = set()
    with db.connect() as conn:
        for (names,) in conn.execute("SELECT speaker_names_json FROM meetings WHERE speaker_names_json IS NOT NULL"):
            try:
                people.update(v.strip() for v in json.loads(names).values() if isinstance(v, str) and v.strip())
            except ValueError:
                pass
        found.update(r[0] for r in conn.execute("SELECT name FROM workspaces"))
        found.update(r[0].strip() for r in conn.execute("SELECT title FROM meetings") if r[0] and len(r[0].strip()) > 3)
    # Each part of a person's name too ("Dana Whitfield" → "Dana", "Whitfield"), if it looks like a name.
    for full in people:
        found.add(full)
        found.update(p for p in full.split() if len(p) >= 3 and p[0].isupper() and p.isalpha())
    generic = {"You", "Them", "Default", "Meeting", "Imported meeting", "Personal", "Other"}
    return sorted((n for n in found if n not in generic), key=len, reverse=True)


def scrub_known(text: str, names: list[str]) -> str:
    for name in names:
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "[private]", text)
    return text


# ── The local AI pass ───────────────────────────────────────────────────

SCRUB_PROMPT = """Below is text from a bug report about a meeting-notes app. Before it is posted publicly, remove all
personal information. Copy the text exactly, line by line, except:
- people's names → [name]
- companies, organizations, schools, projects → [organization]
- places and addresses → [place]
- health, money, legal or family details about anyone → [personal detail]
- anything someone said in a meeting, or that summarizes what a meeting was about → [meeting content]
Keep everything else unchanged: the app's buttons, labels and status messages, error messages, numbers,
dates, times, versions, model names (like qwen2.5:7b), and words already in [brackets].
Reply with only the rewritten text.

TEXT:
{text}"""


def scrub_with_ai(text: str, cfg: dict) -> tuple[str, str | None]:
    """(text, model) after the local AI's pass; (text, None) unchanged if no local model can do it. Only a
    model on this Mac is ever used: the text isn't private yet."""
    if not text.strip() or not llm_engine.is_local("ollama", cfg):
        return text, None
    # The most capable model installed here, whatever writes your notes: small ones miss names (in testing,
    # qwen2.5:3b let 14 of 27 personal details through, qwen2.5:7b 6, mostly meeting content).
    model = llm_engine.preferred_ollama_model(llm_engine._ollama_tags(cfg))
    if not model:
        return text, None
    try:
        out = llm_engine.generate("ollama", {**cfg, "ollama_model": model}, SCRUB_PROMPT.format(text=text),
                                  keep_alive="2m", exact=True).strip()
    except llm_engine.LLMError:
        return text, None
    # A model that wrote far more than it was given added something of its own: don't trust it.
    if not out or len(out) > len(text) * 1.5 + 200:
        return text, None
    return out, model


def scrub(text: str, names: list[str], cfg: dict | None = None) -> tuple[str, str | None]:
    """All the layers (see the module docstring). Returns (clean text, the local model used or None)."""
    text = scrub_known(scrub_patterns(text), names)
    model = None
    if cfg is not None:
        text, model = scrub_with_ai(text, cfg)
    return scrub_known(scrub_patterns(text), names), model


# ── Diagnostics ─────────────────────────────────────────────────────────

def _log_tail() -> list[str]:
    """Recent warnings and errors from the engine's log (without request lines)."""
    path = db.DATA_DIR / "logs" / "server.log"
    try:
        lines = path.read_text(errors="replace").splitlines()[-4000:]
    except OSError:
        return []
    keep = [ln for ln in lines if re.search(r"error|exception|traceback|warning|failed|\bWARN", ln, re.I)
            and '" 200 ' not in ln and "HTTP/1.1" not in ln]
    return keep[-LOG_LINES:]


def diagnostics(cfg: dict, meeting_id: int | None) -> dict:
    device = resources.device_advice()
    out = {
        "Trailmix": archive.APP_VERSION,
        "macOS": platform.mac_ver()[0] or platform.platform(),
        "Mac": f"{device['chip'] or platform.machine()}, {device['ram_gb']} GB memory, {device['disk_free_gb']} GB free",
        "Transcription": f"{cfg['transcribe_engine']}, final transcript while recording: {'on' if cfg.get('live_final') else 'off'}",
        "Notes AI": f"{cfg['summary_provider']} ({llm_engine.model_for(cfg['summary_provider'], cfg) or 'none'})",
        "Meetings": db.count_meetings(),
        "Workspaces": len(db.list_workspaces()),
    }
    if meeting_id and (m := db.get_meeting(meeting_id)):
        out["This meeting"] = (f"status {m['status']}, {round((m.get('duration_sec') or 0) / 60)} min, "
                               f"{'both sides' if m.get('has_system') else 'one mic'}, "
                               f"transcribed with {m.get('transcribed_with') or '?'}, notes by {m.get('summary_model') or '?'}"
                               + (f", wait: {m['wait_reason']}" if m.get("wait_reason") else "")
                               + (f", error: {m['error']}" if m.get("error") else "")
                               + (f", notes error: {m['summary_error']}" if m.get("summary_error") else ""))
    return out


# ── The report ──────────────────────────────────────────────────────────

def prepare(description: str, screen: str, page: str, include_screen: bool, include_diagnostics: bool,
            blocks: bool, meeting_id: int | None, cfg: dict) -> dict:
    """The report as it would be posted: {"title", "body", "url", "model"}."""
    names = known_names()
    said, model = scrub(description.strip(), names, cfg)
    parts = [f"**What happened**\n\n{said or '(no description)'}"]
    if include_screen and screen.strip():
        shown, m2 = scrub(screen.strip()[:4000], names, cfg)
        model = model or m2
        parts.append(f"**On screen** ({page or 'Trailmix'}; personal parts hidden)\n\n```\n{shown}\n```")
    if include_diagnostics:
        diag = "\n".join(f"- {k}: {v}" for k, v in diagnostics(cfg, meeting_id).items())
        logs = "\n".join(_log_tail())
        clean_diag, _ = scrub(diag, names)
        parts.append(f"**Diagnostics**\n\n{clean_diag}")
        if logs:
            clean_logs, _ = scrub(logs, names)
            parts.append(f"<details><summary>Recent errors in the log</summary>\n\n```\n{clean_logs}\n```\n</details>")
    parts.append(f"<sub>Sent from Trailmix {archive.APP_VERSION}. Personal information was removed on the sender's Mac"
                 f"{f' (patterns, known names, and {model})' if model else ' (patterns and known names)'}, "
                 "and they checked it before sending.</sub>")
    body = "\n\n".join(parts)
    first = re.split(r"[.\n!?]", said, maxsplit=1)[0].strip()[:70] if said else "Problem report"
    title = f"[Beta {archive.APP_VERSION}]{' [blocks me]' if blocks else ''} {first or 'Problem report'}"
    return {"title": title, "body": body, "model": model, "url": issue_url(title, body)}


def issue_url(title: str, body: str) -> str:
    if len(body) > URL_BODY_MAX:
        body = body[:URL_BODY_MAX] + "\n\n(cut short to fit; the rest is in the copy you can paste)"
    return f"https://github.com/{REPO}/issues/new?title={quote(title)}&body={quote(body)}"
