"""User settings, stored in SQLite so the backend (pipeline, exporter, providers) can act on them.

Any default can be preset from the environment as TRAILMIX_<KEY> (e.g. TRAILMIX_TRANSCRIBE_ENGINE=remote),
which is handy for a server/Docker deployment. Values saved in the UI win over the environment.
API keys are write-only from the UI's point of view: they're never sent back, only a hint (…1234).
"""
import json
import os
from pathlib import Path

import psutil

import database as db

# Final transcript while recording: on by default with 16 GB of memory or more, where the accurate speech
# model (about 2 GB) sits comfortably next to a call; with 8 GB the meeting gets a light draft and the
# accurate transcript afterwards.
_ROOMY = psutil.virtual_memory().total >= 15 * 1024**3

DEFAULTS = {
    # Two-step flow: when off, the meeting waits for you to start that step.
    "auto_transcribe": True,
    "auto_summarize": True,
    "auto_title": True,
    "auto_workspace": True,             # sort each new meeting into a workspace from its notes (workspaces.py)
    "ask_questions": True,              # quick questions about what the AI wasn't sure of (questions.py)
    "vocabulary": [],                   # names and terms you confirmed, for the speech model and notes AI
    "current_workspace": 0,             # the workspace you're in (0: all); new recordings go there
    # People
    "your_name": "",
    # Summaries
    "summary_provider": "ollama",       # ollama | anthropic | openai | gemini | deepseek | custom
    "summary_fallback": "none",         # a second provider to try if the first fails, or "none"
    "summary_template": "general",
    "custom_template": "",
    # AI providers
    "ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
    "ollama_model": os.getenv("OLLAMA_MODEL", ""),   # empty = first installed model
    "anthropic_model": "claude-opus-5-5",
    "anthropic_api_key": "",
    "openai_model": "gpt-5-mini",
    "openai_api_key": "",
    "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    "gemini_api_key": "",
    "deepseek_model": "deepseek-chat",
    "deepseek_api_key": "",
    "custom_name": "OpenAI-compatible",
    "custom_base_url": "",
    "custom_model": "",
    "custom_api_key": "",
    # Transcription
    "transcribe_engine": "local",       # local (MLX on this Mac) | remote (OpenAI-compatible endpoint)
    "transcribe_url": "",               # e.g. https://api.openai.com/v1 or http://my-mac:8770/v1
    "transcribe_api_key": "",
    "transcribe_model": "whisper-1",
    "transcribe_live_model": "",        # empty = same as transcribe_model
    "live_final": _ROOMY,               # transcribe with the accurate model while recording (see live.py)
    "live_notes": "auto",               # notes written during the meeting: auto | on | off (live_notes.py)
    "whats_new_seen": "",               # the version whose "What's new" you last closed
    # Lockdown mode: nothing leaves this computer (see locked() and the places that check it)
    "lockdown": False,
    # Anonymous usage stats (telemetry.py): on by default, sent only once you've seen the choice
    "share_stats": True,
    "stats_notice_seen": False,
    "install_id": "",                   # random; a new one each time stats are turned back on
    "stats_last_daily": "",             # the day the daily summary was last sent
    # Import
    "granola_api_key": "",              # grn_… from Granola → Settings → Connectors → API keys (Business plan)
    # Export
    "auto_export": False,
    "export_dir": str(Path.home() / "Documents" / "Trailmix"),
    "export_formats": ["pdf", "docx"],  # any of pdf, docx (Word), odt (OpenDocument), md, txt
    "export_summary": True,
    "export_transcript": True,
    "export_separate_files": False,   # False = one document containing both
    "export_audio": False,            # also the recording (hard-linked: no extra space on the same disk)
}
SECRETS = {k for k in DEFAULTS if k.endswith("_api_key")}
# Environment variables people already use for these keys.
_KEY_ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
}
CHOICES = {
    "summary_provider": ("ollama", "anthropic", "openai", "gemini", "deepseek", "custom"),
    "summary_fallback": ("none", "ollama", "anthropic", "openai", "gemini", "deepseek", "custom"),
    "transcribe_engine": ("local", "remote"),
    "live_notes": ("auto", "on", "off"),
}


def _env_default(key: str, default):
    raw = os.getenv(f"TRAILMIX_{key.upper()}") or (os.getenv(_KEY_ENV[key]) if key in _KEY_ENV else None)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    return raw


def get_all() -> dict:
    """Every setting with its effective value, secrets included. For backend use only."""
    with db.connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: json.loads(r["value"]) for r in rows}
    out = {}
    for k, default in DEFAULTS.items():
        v = stored.get(k)
        if v is None or (k in SECRETS and v == ""):  # unset (or a cleared key): fall back to the environment
            v = _env_default(k, default)
        out[k] = v
    return out


def public() -> dict:
    """Settings for the UI: secrets blanked, with a short hint of what's stored."""
    s = get_all()
    hints = {}
    for k in SECRETS:
        v = s[k]
        hints[k] = f"…{v[-4:]}" if len(v) >= 8 else ("set" if v else None)
        s[k] = ""
    s["secret_hints"] = hints
    return s


def update(changes: dict) -> dict:
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown setting: {key}")
        if not isinstance(value, type(DEFAULTS[key])):
            raise ValueError(f"Setting {key} must be a {type(DEFAULTS[key]).__name__}")
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{key} must be one of {', '.join(CHOICES[key])}")
        if key == "export_formats" and any(f not in ("pdf", "docx", "odt", "md", "txt") for f in value):
            raise ValueError("export_formats can include pdf, docx, odt, md and txt")
    if changes.get("share_stats") is False:
        changes["install_id"] = ""  # turning stats back on later starts a new, unconnected ID
    if "export_dir" in changes:
        folder = Path(changes["export_dir"]).expanduser()
        if not folder.is_absolute():
            raise ValueError("Export folder must be an absolute path (or start with ~)")
    for key in ("ollama_url", "custom_base_url", "transcribe_url"):
        v = changes.get(key)
        if v and not v.startswith(("http://", "https://")):
            raise ValueError(f"{key.replace('_', ' ')} must start with http:// or https://")
        if v:
            changes[key] = v.rstrip("/")
    with db.connect() as conn:
        for key, value in changes.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value.strip() if key in SECRETS else value)),
            )
    return public()


LOCKDOWN = "Lockdown mode is on (Settings → Privacy)"


def locked(cfg: dict | None = None) -> bool:
    """Lockdown mode: nothing leaves this computer. No usage stats, no automatic update checks, no model
    downloads or ranking updates, and only AI and transcription that run on this computer."""
    return bool((cfg or get_all()).get("lockdown"))


def on_this_computer(url: str) -> bool:
    from urllib.parse import urlparse

    return (urlparse(url or "").hostname or "") in ("localhost", "127.0.0.1", "::1")
