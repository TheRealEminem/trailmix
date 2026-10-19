"""Catch me up: questions about a meeting while it's still recording, for when you zoned out or got sidetracked.
"What did I miss?" (the last five minutes), "Explain that" (the last thing said, in plainer words), "What's been
decided?", or a question of your own, answered from the meeting so far with timestamps like the ask after a meeting.

The material is what the recording has as it goes (live.py): the live draft, the accurate transcript made while
recording, or Google's live captions, plus the words being said right now. Notes written during the meeting
(live_notes.py) and your own notes come along too, so the start of a long meeting is covered even when only its
latest part fits.

Which AI answers: your notes AI, cloud ones first, since nothing extra then runs on this Mac. A model on this Mac
shares memory with your call app and the live transcript, so on a Mac with less than 32 GB a small installed model
(4B or less) answers instead of a bigger notes model, and when there's none you're told it may slow things down for
a few seconds. A small model is for basic, private use: it sums up and explains well enough, but mixes up what was
decided with what's still open, so "What's been decided?" needs a cloud AI or a bigger model. It reads at most the last 20 minutes, so the answer comes quickly, and lets go of its memory a minute
after the last question. Lockdown mode keeps it on this computer: only AI running here is asked.
"""
import json
from datetime import datetime, timezone

import psutil

import database as db
import live
import live_notes
import llm_engine
import meeting_text
import pricing
import resources
import settings

MISSED_S = 300          # "What did I miss?" covers the last five minutes
EXPLAIN_S = 60          # "Explain that": the last minute or so
LOCAL_RECENT_S = 20 * 60  # a model on this Mac reads at most the last 20 minutes (and the notes so far)
SMALL_MAX_B = 4.5       # "a small model": up to about 4B parameters, roughly 2-3 GB loaded
KEEP_ALIVE = "1m"       # long enough for a follow-up question, short enough not to sit through the call
CHARS_PER_TOKEN = 3.1
PROMPT_TOKENS = 400     # the instructions and the question
ANSWER_TOKENS = 1000    # a short answer, plus the thinking some models do first (billed as output)
HISTORY = 50

KINDS = {"missed": "What did I miss?", "explain": "Explain that", "decided": "What's been decided?"}

SYSTEM = """You help someone who is in a meeting right now and lost track for a moment. You get the live
transcript so far (made as people talk, so words may be misheard and names misspelled), any notes written so far,
and their own notes. Answer at a glance: a few short lines or bullets they can read while the meeting goes on, the
most useful thing first. Use only the material provided; if it doesn't say, say so plainly instead of guessing.
End each point with the timestamp of the moment it comes from, like [12:34], so they can find it in the transcript.
"You" in the transcript is the person asking; say "you" for them."""


class NotReady(Exception):
    """Nothing to answer from, or no AI that may answer: the message says why, in plain words."""


# ── Who answers ─────────────────────────────────────────────────────────

def providers(cfg: dict) -> list[str]:
    """Who may answer, in order: your notes AI and its fallback, cloud ones first. In Lockdown mode only AI on this
    computer (Ollama here, if neither of yours runs here)."""
    order = llm_engine.chain("auto", cfg)
    if settings.locked(cfg):
        here = [p for p in order if llm_engine.runs_here(p, cfg)]
        return here or (["ollama"] if llm_engine.is_local("ollama", cfg) else [])
    return sorted(order, key=lambda p: llm_engine.runs_here(p, cfg))


def _setup(pid: str, cfg: dict) -> dict:
    """How this provider answers: {"cfg": settings to answer with, "small": a small model on this Mac (basic
    questions only), "note", "warning"}. On a Mac under 32 GB, a small installed model stands in for a bigger local
    notes model."""
    out = {"cfg": cfg, "small": False, "note": None, "warning": None}
    if not llm_engine.runs_here(pid, cfg):
        return out
    if pid != "ollama":
        return {**out, "warning": ("This AI runs on this Mac next to your call and the live transcript, which may slow "
                                   "down for a few seconds while it answers.")}
    chosen = llm_engine.model_for("ollama", cfg)
    tags = {m["name"]: m for m in llm_engine._ollama_tags(cfg)}
    if chosen in tags and 0 < llm_engine.ollama_params(tags[chosen]) <= SMALL_MAX_B:
        return {**out, "small": True, "note": _basic(chosen)}
    ram = psutil.virtual_memory().total / resources.GB
    if ram >= live_notes.BIG_RAM_GB:
        return out
    small = llm_engine.small_ollama_model(cfg, SMALL_MAX_B)
    if small:
        return {**out, "cfg": {**cfg, "ollama_model": small["name"]}, "small": True,
                "note": _basic(small["name"], f"rather than {chosen}, so it fits next to your call")}
    size = llm_engine.ollama_model_size_gb(cfg)
    return {**out, "warning": (
        f"{chosen} ({f'about {size * 1.1 + 1.5:.0f} GB' if size else 'several GB'}) loads next to your call and the live "
        f"transcript. With {ram:.0f} GB of memory they may slow down for a few seconds while it answers. A small model "
        "such as qwen2.5:3b in Ollama would answer here instead, for the basic questions.")}


def _basic(model: str, why: str = "") -> str:
    return (f"Basic and private: {model}, a small model on this Mac, answers{' ' + why if why else ''}. "
            f'"{KINDS["decided"]}" needs a cloud AI or a bigger model.')


def kinds_off(setup: dict) -> dict:
    """Quick questions this model isn't good enough for, with why: a small model mixes up what was decided with what's
    still open (measured with qwen2.5:3b; qwen2.5:7b got it right)."""
    if not setup["small"]:
        return {}
    return {"decided": "Needs a cloud AI or a bigger model: a small one mixes up what's decided with what's still open"}


def _material_chars(pid: str, cfg: dict) -> int:
    return llm_engine.context_chars(pid, cfg) - PROMPT_TOKENS * 3


# ── What it reads ───────────────────────────────────────────────────────

def _lines(session: "live.LiveSession") -> list[dict]:
    """The live transcript so far, oldest first, with the words being said right now at the end."""
    lines = sorted(list(session.drafts), key=lambda d: d["start"])
    lines += [{**d, "interim": True} for d in sorted(session.interim.values(), key=lambda d: d["start"]) if d.get("text")]
    return lines


def _transcript(lines: list[dict], names: dict) -> list[str]:
    turns = meeting_text.turns([{**d, "end": d["start"] + 5} for d in lines if not d.get("interim")])
    out = [f"[{meeting_text.fmt(t['start'])}] {names.get(t['speaker'], t['speaker']) + ': ' if t.get('speaker') else ''}{t['text']}"
           for t in turns]
    out += [f"[{meeting_text.fmt(d['start'])}] {names.get(d['speaker'], d['speaker']) + ': ' if d.get('speaker') else ''}"
            f"{d['text']} (still talking)" for d in lines if d.get("interim")]
    return out


def material(session: "live.LiveSession", m: dict, cfg: dict, limit: int, since: float = 0.0) -> str:
    """The meeting so far as the AI reads it: notes written so far, your notes, then as much of the transcript from
    `since` seconds as fits in `limit` characters (the latest kept)."""
    names = meeting_text.speaker_names(m, cfg)
    parts = [f"MEETING: {m.get('title') or 'Recording'} (still going, {meeting_text.fmt(session.duration)} so far)"]
    if session.has_system:
        parts.append(f'"{names["You"]}" is the person asking; "{names["Them"]}" is everyone else on the call.')
    notes = live_notes.status(session.meeting_id)["notes"]
    if notes:
        parts.append("NOTES SO FAR:\n" + "\n\n".join(
            f"{meeting_text.fmt(n['start'])} to {meeting_text.fmt(n['end'])}:\n{n['text']}" for n in notes))
    mine = meeting_text.my_notes_lines(m)
    if mine:
        parts.append("MY NOTES:\n" + "\n".join(mine))
    room = limit - sum(len(p) + 2 for p in parts) - 40
    lines = _transcript([d for d in _lines(session) if d["start"] >= since], names)
    kept: list[str] = []
    for line in reversed(lines):
        if room - len(line) - 1 < 0:
            break
        kept.append(line)
        room -= len(line) + 1
    kept.reverse()
    cut = " (the start is left out: see the notes so far)" if len(kept) < len(lines) or since > 0 else ""
    parts.append(f"TRANSCRIPT SO FAR{cut}:\n" + ("\n".join(kept) or "(nothing said yet)"))
    return "\n\n".join(parts)


def question_for(kind: str, session: "live.LiveSession", text: str = "") -> str:
    """The question put to the AI for a quick button (or yours, as typed)."""
    now = session.duration
    if kind == "missed":
        return (f"I wasn't paying attention for the last few minutes, from [{meeting_text.fmt(max(0.0, now - MISSED_S))}] "
                f"to now ([{meeting_text.fmt(now)}]). What did I miss? The gist in a few bullets, most important first, "
                "then anything I may be asked about or need to respond to.")
    if kind == "explain":
        return (f"Explain the last thing that was said (around [{meeting_text.fmt(max(0.0, now - EXPLAIN_S))}] to now) in "
                "plainer words. Skip filler like \"okay\" or \"yeah\" and take the last real point. First say what was "
                "said in one plain sentence, then explain each technical term or acronym in it (the transcript may have "
                "misheard one: say what was most likely meant), then why it matters here. Keep it short.")
    if kind == "decided":
        return ("What's been decided so far in this meeting, and what's still open? Two short lists. A decision is "
                "something the people on the call agreed to or settled; a proposal, a question or something put off "
                "is still open. Name who is doing what next only when someone said so. If nothing has been decided "
                "yet, say so.")
    return text


def _since(pid: str, cfg: dict, session: "live.LiveSession") -> float:
    """Where in the recording the material starts: the top, or for a model on this Mac the last 20 minutes."""
    return max(0.0, session.duration - LOCAL_RECENT_S) if llm_engine.runs_here(pid, cfg) else 0.0


# ── Before you ask: who answers, and what it costs ──────────────────────

def history(m: dict) -> list[dict]:
    """The questions asked of this meeting while it was recording."""
    return [e for e in (json.loads(m["qa_json"]) if m.get("qa_json") else []) if e.get("live")]


def plan(session: "live.LiveSession", m: dict, cfg: dict) -> dict:
    """What asking would take right now: {ready, reason, provider, label, model, local, cost, note, warning, history}.
    `cost` is about what one question costs in dollars (0 on this computer, None where the price isn't known)."""
    out = {"ready": False, "reason": None, "provider": None, "label": None, "model": None, "local": False,
           "cost": None, "note": None, "warning": None, "off": {}, "kinds": KINDS, "history": history(m), "as_of": pricing.AS_OF}
    if not (session.draft or session.final):
        return {**out, "reason": ("Catch me up reads the live transcript, and this recording doesn't have one. Turn on "
                                  "the live draft or captions before you start recording.")}
    order = providers(cfg)
    if not order:
        return {**out, "reason": (f"{settings.LOCKDOWN}, so only AI on this computer can answer, and none is set up. "
                                  "Install Ollama, or choose an AI on this computer in Settings → AI.")}
    errors = []
    for pid in order:
        ok, why = llm_engine.configured(pid, cfg)
        if ok:
            break
        errors.append(why)
    else:
        return {**out, "reason": "; ".join(errors)}
    setup = _setup(pid, cfg)
    model = llm_engine.model_for(pid, setup["cfg"])
    local = llm_engine.runs_here(pid, cfg)
    cost = 0.0
    if not local:
        since = _since(pid, cfg, session)
        chars = min(_material_chars(pid, cfg), len(material(session, m, cfg, 10**9, since)))
        cost = (pricing.text_cost(model, int(chars / CHARS_PER_TOKEN) + PROMPT_TOKENS, ANSWER_TOKENS)
                if pid == "gemini" else None)
    return {**out, "ready": True, "provider": pid, "label": llm_engine.label(pid, cfg), "model": model,
            "local": local, "cost": cost, "note": setup["note"], "warning": setup["warning"], "off": kinds_off(setup)}


# ── Asking ──────────────────────────────────────────────────────────────

def ask(session: "live.LiveSession", m: dict, cfg: dict, kind: str, text: str = "") -> dict:
    """Answers a quick button or your question from the meeting so far, and keeps it with the meeting's questions:
    {q, a, provider, label, at, live, kind, t}. Raises NotReady, or llm_engine.LLMError when every AI failed."""
    if kind not in KINDS and kind != "question":
        raise NotReady("Pick one of the quick questions, or type your own")
    if kind == "question" and not text.strip():
        raise NotReady("Type a question first")
    before = plan(session, m, cfg)
    if not before["ready"]:
        raise NotReady(before["reason"])
    if kind in before["off"]:
        raise NotReady(before["off"][kind])
    if not _lines(session):
        raise NotReady("Nothing has been transcribed yet. Ask again once people have said a few words.")
    question = question_for(kind, session, text.strip())

    usage: dict = {}  # the tokens Gemini reports, for the spend tally

    def answer(pid: str) -> str:
        setup = _setup(pid, cfg)
        if kind in kinds_off(setup):
            raise llm_engine.LLMError(kinds_off(setup)[kind])
        if llm_engine.runs_here(pid, cfg) and resources.memory_pressure() >= 4:
            raise llm_engine.LLMError("macOS reports critically low memory, so the model on this Mac waits. Ask again "
                                      "in a moment.")
        context = material(session, m, cfg, _material_chars(pid, cfg), _since(pid, cfg, session))
        return llm_engine.generate(pid, setup["cfg"], f"{context}\n\nQUESTION: {question}", system=SYSTEM,
                                   keep_alive=KEEP_ALIVE, usage=usage)

    errors = []
    for pid in providers(cfg):
        try:
            reply, used = answer(pid), pid
            break
        except llm_engine.LLMError as e:
            errors.append(f"{llm_engine.label(pid, cfg)}: {e}")
    else:
        raise llm_engine.LLMError("; ".join(errors))
    if used == "gemini":
        model = cfg["gemini_model"]
        spent = (pricing.text_cost(model, usage["in"], usage["out"]) if usage.get("in")  # what Google billed
                 else before["cost"] if before["provider"] == "gemini" else None)      # else the estimate
        db.log_usage(session.meeting_id, "catch up", "gemini", model, 0, spent)
    entry = {"q": KINDS.get(kind, question), "a": reply.strip(), "provider": used, "label": llm_engine.label(used, cfg),
             "at": datetime.now(timezone.utc).isoformat(), "live": True, "kind": kind, "t": round(session.duration, 1)}
    fresh = db.get_meeting(session.meeting_id) or m  # another question may have been saved meanwhile
    saved = json.loads(fresh["qa_json"]) if fresh.get("qa_json") else []
    db.update_meeting(session.meeting_id, qa_json=(saved + [entry])[-HISTORY:])
    return entry
