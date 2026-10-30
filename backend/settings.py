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
    # Imports wait for you to pick which AI transcribes them and writes their notes (and see what each would take)
    # instead of starting with the defaults below. (Recordings use the defaults; notes you rewrite always ask.)
    "ask_before_ai": True,
    # The same meeting from Trailmix and from Granola (or a pasted transcript): merge into one meeting, keeping the
    # recording but preferring the import's transcript and notes | ask on the meeting | keep both (duplicates.py)
    "duplicates": "merge",
    "auto_title": True,
    "auto_workspace": True,             # sort each new meeting into a workspace from its notes (workspaces.py)
    "ask_questions": True,              # quick questions about what the AI wasn't sure of (questions.py)
    "vocabulary": [],                   # names and terms you confirmed, for the speech model and notes AI
    "current_workspace": 0,             # the workspace you're in (0: all); new recordings go there
    # People
    "your_name": "",
    # Summaries
    "summary_provider": "ollama",       # ollama | anthropic | openai | gemini | deepseek | custom | trailmix
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
    "gemini_model": os.getenv("GEMINI_MODEL", "gemini-flash-latest"),   # (gemini-2.5-flash is closed to new accounts)
    "gemini_api_key": "",
    "deepseek_model": "deepseek-chat",
    "deepseek_api_key": "",
    "custom_name": "OpenAI-compatible",
    "custom_base_url": "",
    "custom_model": "",
    "custom_api_key": "",
    "custom_context_tokens": 0,         # the server's context window, where it says (local_ai.py); 0: not known
    # Transcription
    "transcribe_engine": "local",       # local (MLX or whisper.cpp on this computer) | remote (OpenAI-compatible endpoint) | gemini (speaker labels)
                                        # | trailmix (Trailmix Cloud)
    "transcribe_url": "",               # e.g. https://api.openai.com/v1 or http://my-mac:8770/v1
    "local_speech_model": "",           # the accurate local model picked in Settings (local_engine.catalog); "": automatic
    "transcribe_api_key": "",
    "transcribe_model": "whisper-1",
    "transcribe_live_model": "",        # empty = same as transcribe_model
    "gemini_transcribe_model": "gemini-3.5-transcribe",   # the Google model for transcripts made after a recording
    "gemini_mix_tracks": True,          # send your mic and the call mixed as one recording: half the cost (mixdown.py)
    "live_final": _ROOMY,               # transcribe with the accurate model while recording (see live.py)
    "live_notes": "auto",               # notes written during the meeting: auto | on | off (live_notes.py)
    # Trailmix Cloud (cloud.py): this Mac's device token, from signing in, and the account as last seen (plan, level,
    # hours). Set by signing in, never from the settings page.
    "cloud_token": "",
    "cloud_account": {},
    "spend_warn_cents": 50,             # ask before one thing that would cost this much or more (0: never ask); Settings → Usage
    "whats_new_seen": "",               # the version whose "What's new" you last closed
    "setup_done": False,                # the first-run setup was finished or skipped (Welcome.tsx)
    # Attachments (attachments.py): what reads photos and writes the short digest of a file you add to the searchable
    # notes. local: text recognition on this Mac and your local model, nothing leaves the computer | provider: your
    # summary provider (a cloud company or your own endpoint) is sent the photo or the file's text.
    "attachments_engine": "local",
    "attachments_shrink_over_mb": 10,   # offer to shrink a file bigger than this (a HEIC photo is always offered)
    "attachments_always_shrink": False, # shrink those without asking
    # Recordings: days to keep the audio after a meeting (0: delete once it's transcribed; -1: keep forever).
    # "Keep forever" on a meeting overrides it. (audio_store.py)
    "audio_retention_days": 30,
    # Recording laws (recording_laws.py): the region whose entry you read, and the table's date when you read it,
    # so a later change to that entry can be pointed out. Information only; nothing is changed unless you apply it.
    "legal_region": "",
    "legal_seen": "",
    # Lockdown mode: nothing leaves this computer (see locked() and the places that check it)
    "lockdown": False,
    # Anonymous usage stats (telemetry.py): on by default, sent only once you've seen the choice
    "share_stats": True,
    "stats_notice_seen": False,
    "install_id": "",                   # random; a new one each time stats are turned back on
    "stats_last_daily": "",             # the day the daily summary was last sent
    # Import
    "granola_api_key": "",              # grn_… from Granola → Settings → Connectors → API keys (Business plan)
    "fathom_api_key": "",               # Fathom → Settings → API Access
    "fireflies_api_key": "",            # Fireflies → Integrations → Fireflies API
    "auto_import": [],                  # sources whose new meetings come in by themselves (sources.py)
    "auto_import_keep_notes": True,     # with their notes as the summary (off: Trailmix writes new ones)
    "watch_folders": [],                # folders whose new recordings and transcripts come in by themselves (watched.py)
    # Tasks: where "Send to …" on a task makes a ticket (trackers.py). Empty: nowhere, and it isn't offered.
    "task_tracker": "",
    "traverse_url": "http://127.0.0.1:8793",
    "traverse_api_key": "",             # only when Traverse's sign-in is on (its access token)
    "traverse_project": "",             # a Traverse project's id; empty: no project
    # Export
    "auto_export": False,
    "export_dir": str(Path.home() / "Documents" / "Trailmix"),
    "export_formats": ["pdf", "docx"],  # any of pdf, docx (Word), odt (OpenDocument), md, txt
    "export_summary": True,
    "export_transcript": True,
    "export_separate_files": False,   # False = one document containing both
    "export_audio": False,            # also the recording (hard-linked: no extra space on the same disk)
}
SECRETS = {k for k in DEFAULTS if k.endswith("_api_key")} | {"cloud_token"}
INTERNAL = {"cloud_token", "cloud_account"}  # written by the backend itself, not through the settings page
# Environment variables people already use for these keys.
_KEY_ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
}
CHOICES = {
    "summary_provider": ("ollama", "anthropic", "openai", "gemini", "deepseek", "custom", "trailmix"),
    "summary_fallback": ("none", "ollama", "anthropic", "openai", "gemini", "deepseek", "custom", "trailmix"),
    "transcribe_engine": ("local", "remote", "gemini", "trailmix"),
    "live_notes": ("auto", "on", "off"),
    "duplicates": ("merge", "ask", "keep"),
    "attachments_engine": ("local", "provider"),
    "task_tracker": ("", "traverse"),
}


def _env_default(key: str, default):
    raw = os.getenv(f"TRAILMIX_{key.upper()}") or (os.getenv(_KEY_ENV[key]) if key in _KEY_ENV else None)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
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


def update(changes: dict, internal: bool = False) -> dict:
    for key, value in changes.items():
        if key not in DEFAULTS or (key in INTERNAL and not internal):
            raise ValueError(f"Unknown setting: {key}")
        if not isinstance(value, type(DEFAULTS[key])):
            raise ValueError(f"Setting {key} must be a {type(DEFAULTS[key]).__name__}")
        if isinstance(value, bool) and not isinstance(DEFAULTS[key], bool):
            raise ValueError(f"Setting {key} must be a {type(DEFAULTS[key]).__name__}")
        if key == "audio_retention_days" and value < -1:
            raise ValueError("audio_retention_days must be -1 (forever), 0 (after transcription) or a number of days")
        if key == "custom_context_tokens" and value < 0:
            raise ValueError("custom_context_tokens must be 0 (not known) or a number of tokens")
        if key == "spend_warn_cents" and value < 0:
            raise ValueError("spend_warn_cents must be 0 (never ask) or a number of cents")
        if key == "gemini_transcribe_model" and not value.strip():
            raise ValueError("gemini_transcribe_model can't be empty")
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{key} must be one of {', '.join(CHOICES[key])}")
        if key == "auto_import":
            import sources
            if any(n not in sources.SOURCES for n in value):
                raise ValueError(f"auto_import can include {', '.join(sources.SOURCES)}")
        if key == "watch_folders" and any(not isinstance(f, str) or not os.path.isabs(f) for f in value):
            raise ValueError("watch_folders must be full paths")
        if key == "export_formats" and any(f not in ("pdf", "docx", "odt", "md", "txt") for f in value):
            raise ValueError("export_formats can include pdf, docx, odt, md and txt")
    if changes.get("share_stats") is False:
        changes["install_id"] = ""  # turning stats back on later starts a new, unconnected ID
    if "export_dir" in changes:
        folder = Path(changes["export_dir"]).expanduser()
        if not folder.is_absolute():
            raise ValueError("Export folder must be an absolute path (or start with ~)")
    for key in ("ollama_url", "custom_base_url", "transcribe_url", "traverse_url"):
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
