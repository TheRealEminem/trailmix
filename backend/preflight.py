"""Before a meeting is processed: which AI could transcribe it and write its notes, and what each choice would take
(passes over the transcript, memory on this Mac, money in the cloud), so you can pick before anything runs.

The estimates are rough on purpose: a transcript's length is known once it exists (an import of text) and guessed from
the recording's length otherwise; token counts are characters / 3.1; prices come from pricing.py and are None where
Trailmix doesn't know them.
"""
import math

import psutil

import cloud
import gemini_client
import llm_engine
import mlx_engine
import pricing
import settings
import transcribe_gemini

GB = 1024**3
CHARS_PER_MINUTE = 1000   # about what a minute of talk comes to as transcript (measured on real meetings: 960-1,045)
CHARS_PER_TOKEN = 3.1
PROMPT_TOKENS = 1200      # instructions, note style and meeting description sent with each request
NOTES_TOKENS = 1500       # the notes written for one part, or the finished notes
EXTRA_CALLS = 3           # a title, tags, and the quick questions: short requests that read the notes, not the transcript
EXTRA_TOKENS = (2500, 150)
ECONOMY_TRANSCRIBE_MODEL = "gemini-3.5-flash-lite"   # a general model asked to transcribe: about a quarter the price


def engine_problem(engine: str, cfg: dict) -> str | None:
    """Why this speech engine can't be used right now, in plain words, or None if it can."""
    if engine not in settings.CHOICES["transcribe_engine"]:
        return "Unknown transcription engine"
    if engine == "gemini":
        if settings.locked(cfg):
            return f"{settings.LOCKDOWN}, so recordings can't be sent to Google"
        if not gemini_client.ready(cfg):
            return "Add a Google API key first (Settings → Transcription)"
    elif engine == "trailmix":
        if settings.locked(cfg):
            return f"{settings.LOCKDOWN}, so recordings can't be sent to Trailmix Cloud"
        if not gemini_client.ready(cfg, "trailmix"):
            return "Sign in to Trailmix Cloud first (Settings → AI providers)"
    elif engine == "remote":
        if not cfg["transcribe_url"]:
            return "Set the transcription endpoint first (Settings → Transcription)"
        if settings.locked(cfg) and not settings.on_this_computer(cfg["transcribe_url"]):
            return f"{settings.LOCKDOWN}, so recordings can't go to a transcription server elsewhere"
    elif not mlx_engine.available():
        return "Transcribing on this Mac needs a Mac with MLX"
    return None


def transcribe_models(engine: str, cfg: dict) -> list[dict]:
    """The speech models worth offering for an engine: [{id, label}]."""
    if engine == "gemini":
        standard = cfg["gemini_transcribe_model"] or transcribe_gemini.MODEL
        out = [{"id": standard, "label": "Standard · labels who said what"}]
        if standard != ECONOMY_TRANSCRIBE_MODEL:
            out.append({"id": ECONOMY_TRANSCRIBE_MODEL, "label": "Economy · about a quarter the price"})
        return out
    if engine == "remote":
        return [{"id": cfg["transcribe_model"], "label": cfg["transcribe_model"]}]
    if engine == "trailmix":
        return [{"id": "transcribe", "label": "Picked by Trailmix Cloud for your plan"}]
    return [{"id": mlx_engine.FINAL_MODEL.rsplit("/", 1)[-1].removesuffix("-mlx"), "label": "Whisper, on this Mac"}]


def transcript_chars(m: dict) -> int:
    """The transcript's length, or a guess from the recording's length when there's no transcript yet."""
    if m.get("transcript"):
        return len(m["transcript"])
    return int((m.get("duration_sec") or 0) / 60 * CHARS_PER_MINUTE)


def notes_passes(chars: int, pid: str, cfg: dict) -> int:
    """How many pieces the transcript is read in: 1 when it fits the model's window, more when it doesn't (each part
    gets notes of its own, then the notes are written up; see llm_engine.summarize)."""
    budget = llm_engine.context_chars(pid, cfg)
    return max(1, math.ceil(chars / budget))


def notes_plan(meetings: list[dict], pid: str, cfg: dict) -> dict:
    """What writing these meetings' notes with this provider (and the model in cfg) would take."""
    model = llm_engine.model_for(pid, cfg)
    rows, calls, tokens_in, tokens_out = [], 0, 0, 0
    for m in meetings:
        chars = transcript_chars(m)
        parts = notes_passes(chars, pid, cfg)
        reads = parts + 1 if parts > 1 else 1   # the parts, then the write-up
        tin = int(chars / CHARS_PER_TOKEN) + reads * PROMPT_TOKENS + (parts * NOTES_TOKENS if parts > 1 else 0)
        tout = (parts * NOTES_TOKENS if parts > 1 else 0) + NOTES_TOKENS
        tin, tout = tin + EXTRA_TOKENS[0], tout + EXTRA_TOKENS[1]
        rows.append({"id": m["id"], "chars": chars, "parts": parts, "estimated": not m.get("transcript")})
        calls += reads + EXTRA_CALLS
        tokens_in += tin
        tokens_out += tout
    local = llm_engine.is_local(pid, cfg)
    cost = None if local else 0.0 if pid == "trailmix" else pricing.text_cost(model, tokens_in, tokens_out)
    vm = psutil.virtual_memory()
    return {
        "provider": pid, "label": llm_engine.label(pid, cfg), "model": model, "local": local,
        "runs_here": llm_engine.runs_here(pid, cfg), "meetings": rows, "calls": calls,
        "tokens_in": tokens_in, "tokens_out": tokens_out, "cost": 0.0 if local else cost,
        "window_chars": llm_engine.context_chars(pid, cfg),
        "ram_needed_gb": round(llm_engine.ollama_ram_needed_gb(cfg), 1) if local else None,
        "ram_free_gb": round(vm.available / GB, 1), "ram_total_gb": round(vm.total / GB, 1),
    }


def transcribe_plan(meetings: list[dict], engine: str, model: str, cfg: dict) -> dict:
    """What transcribing these meetings' recordings with this engine and model would take."""
    mixed = engine == "gemini" and bool(cfg["gemini_mix_tracks"])
    minutes = sum(pricing.meeting_minutes(m["duration_sec"], bool(m["has_system"]), mixed=mixed) for m in meetings)
    vm = psutil.virtual_memory()
    return {
        "engine": engine, "model": model, "minutes": round(minutes, 1),
        "cost": pricing.cost(minutes, engine, model), "per_minute": pricing.per_minute(engine, model),
        "ram_needed_gb": mlx_engine.FINAL_MODEL_RAM_GB if engine == "local" else None,
        "ram_free_gb": round(vm.available / GB, 1),
    }


ENGINE_LABELS = {"local": "This Mac", "gemini": "Google Gemini", "remote": "Your endpoint", "trailmix": "Trailmix Cloud"}


def preflight(meetings: list[dict], cfg: dict, engine: str | None = None, transcribe_model: str | None = None,
              provider: str | None = None, model: str | None = None) -> dict:
    """Every choice for these meetings, and the plan for the one picked (the defaults in Settings when not given)."""
    needs_transcript = [m for m in meetings if not m["transcribed"]]
    engine = engine or cfg["transcribe_engine"]
    provider = provider or cfg["summary_provider"]
    plan_cfg = dict(cfg)
    if model:
        plan_cfg[llm_engine.model_key(provider)] = model
    out = {
        "meetings": [{"id": m["id"], "title": m["title"], "minutes": round((m["duration_sec"] or 0) / 60, 1),
                      "needs_transcript": not m["transcribed"]} for m in meetings],
        "defaults": {"engine": cfg["transcribe_engine"], "provider": cfg["summary_provider"],
                     "model": llm_engine.model_for(cfg["summary_provider"], cfg), "template": cfg["summary_template"],
                     "transcribe_model": transcribe_models(cfg["transcribe_engine"], cfg)[0]["id"]},
        "notes": {
            "options": [{"provider": pid, "label": llm_engine.label(pid, cfg), "kind": info["kind"],
                         "local": llm_engine.runs_here(pid, cfg), "model": llm_engine.model_for(pid, cfg),
                         **dict(zip(("ready", "reason"), llm_engine.configured(pid, cfg)))}
                        for pid, info in llm_engine.PROVIDERS.items()],
            "plan": notes_plan(meetings, provider, plan_cfg),
        },
        "transcribe": None,
        "as_of": pricing.AS_OF,
        "cloud": cloud.account(cfg=cfg),  # Trailmix Cloud's plan and hours, when signed in: no money to show there
    }
    if needs_transcript:
        models = transcribe_models(engine, cfg)
        out["transcribe"] = {
            "options": [{"engine": e, "label": ENGINE_LABELS[e], "local": e == "local", "ready": not engine_problem(e, cfg),
                         "reason": engine_problem(e, cfg) or "", "models": transcribe_models(e, cfg)}
                        for e in settings.CHOICES["transcribe_engine"]],
            "plan": transcribe_plan(needs_transcript, engine, transcribe_model or models[0]["id"], cfg),
        }
    return out
