"""Transcription on this computer: MLX on an Apple silicon Mac (mlx_engine.py), whisper.cpp everywhere else
(whispercpp_engine.py). Everything else in Trailmix calls this module, never an engine directly.

Two models are used at different times so a meeting never competes with a heavy model:
  - LIVE_MODEL     (small, in this process, for a rough draft while the meeting runs)
  - final_model()  (large, in a throwaway subprocess - see transcribe_worker.py - after you stop; or in this
                    process during the meeting when "Final transcript while recording" is on, see live.py).
                    The one picked in Settings (local_speech_model) from catalog(), else the engine's own choice.

TRAILMIX_LOCAL_ENGINE=mlx|whispercpp|none picks one by hand (the first one installed is used otherwise).
"""
import os
import sqlite3
import threading

import numpy as np

import mlx_engine
import model_ranks
import settings
import whispercpp_engine

_ENGINES = {"mlx": mlx_engine, "whispercpp": whispercpp_engine}


def _pick():
    wanted = os.getenv("TRAILMIX_LOCAL_ENGINE", "")
    if wanted:
        return _ENGINES.get(wanted)
    return next((e for e in _ENGINES.values() if e.available()), None)


ENGINE = _pick()
NAME = ENGINE.NAME if ENGINE else None
LIVE_MODEL = os.getenv("TRAILMIX_LIVE_MODEL") or (ENGINE or mlx_engine).LIVE_MODEL
_FINAL_ENV = os.getenv("TRAILMIX_WHISPER_MODEL", "")  # wins over Settings (tests, server installs)
_FINAL_RAM_ENV = os.getenv("TRAILMIX_WHISPER_RAM_GB", "")
LANGUAGE = os.getenv("TRAILMIX_LANGUAGE", "en") or None  # None = auto-detect

# The live model is cached in a process-wide singleton, so live calls must not overlap.
_lock = threading.Lock()

_HALLUCINATIONS = {"you", "thank you.", "thanks for watching!", "thanks for watching.", "bye.", "."}


def model_label(model: str) -> str:
    """A model's short name, as shown, recorded with a transcript and ranked (model_ranks.py): "whisper-large-v3-turbo",
    or "whisper-large-v3-turbo-q5_0" for whisper.cpp's "…:ggml-large-v3-turbo-q5_0.bin"."""
    name = model.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    if name.startswith("ggml-"):
        name = "whisper-" + name.removeprefix("ggml-").removesuffix(".bin")
    return name.removesuffix("-mlx")


def run(audio: np.ndarray, model: str, prompt: str | None = None, meeting: bool = False) -> list[dict]:
    """`prompt`: what was said just before (live chunks are a few seconds long; it gives the model the
    context a long recording would have, for names and words cut at a chunk's start). `meeting`: a call is going
    on, so the engine leaves it room (whisper.cpp uses half the cores rather than all of them)."""
    if ENGINE is None:
        raise RuntimeError("No local transcription engine is installed")
    return ENGINE.run(audio, model, prompt, LANGUAGE, meeting)


def catalog() -> list[dict]:
    """The speech models this engine can run, smallest first, for Settings: [{"id", "label", "size_gb", "ram_gb",
    "speed"}]. A fetched model-ranks.json can list them (recommend.speech.<engine>), so a new Whisper-family model
    can be offered without an app update; otherwise the engine's own list."""
    if ENGINE is None:
        return []
    key = next(k for k, e in _ENGINES.items() if e is ENGINE)
    return model_ranks.speech_models(key) or ENGINE.MODELS


def automatic_model() -> str:
    """The accurate model this engine picks for this computer by itself (TRAILMIX_WHISPER_MODEL wins)."""
    return _FINAL_ENV or (ENGINE or mlx_engine).FINAL_MODEL


def final_model(cfg: dict | None = None) -> str:
    """The accurate model: TRAILMIX_WHISPER_MODEL, else the one picked in Settings if this engine lists it, else
    automatic_model(). `cfg` is settings.get_all(), read here when not given."""
    if _FINAL_ENV:
        return _FINAL_ENV
    if cfg is None:
        try:
            cfg = settings.get_all()
        except sqlite3.Error:  # no settings yet (a fresh data folder): the engine's own choice
            cfg = {}
    chosen = cfg.get("local_speech_model") or ""
    return chosen if chosen and any(m["id"] == chosen for m in catalog()) else automatic_model()


def final_model_ram_gb(cfg: dict | None = None) -> float:
    """About how much memory the accurate model takes, for the RAM gate."""
    if _FINAL_RAM_ENV:
        return float(_FINAL_RAM_ENV)
    model = final_model(cfg)
    entry = next((m for m in catalog() if m["id"] == model), None)
    return float(entry["ram_gb"]) if entry and entry.get("ram_gb") else float((ENGINE or mlx_engine).FINAL_MODEL_RAM_GB)


def filter_hallucinations(segs: list[dict]) -> list[dict]:
    return [s for s in segs if s["text"].lower() not in _HALLUCINATIONS]


def transcribe_live(audio: np.ndarray, prompt: str | None = None) -> list[dict] | None:
    """Rough draft of a short chunk. Returns None if the engine is busy (draft is skipped, not queued)."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        segs = run(audio, LIVE_MODEL, prompt, meeting=True)
    finally:
        _lock.release()
    return filter_hallucinations(segs)


def transcribe_final(audio: np.ndarray, prompt: str | None = None, model: str | None = None) -> list[dict]:
    """The accurate model (final_model() unless given) on a chunk, in this process (final transcript while
    recording). Waits its turn."""
    with _lock:
        return run(audio, model or final_model(), prompt, meeting=True)


def loaded() -> str | None:
    """Which model is in memory in this process, if any."""
    return ENGINE.loaded() if ENGINE else None


def warm(model: str) -> bool:
    """Loads `model` and runs it once on a moment of silence, so a recording's first words come out in about
    a second instead of waiting for the model to load. False if it was busy or failed."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        if loaded() != model:
            run(np.zeros(16000, dtype=np.float32), model)
        return True
    except Exception:
        return False
    finally:
        _lock.release()


def available() -> bool:
    """A local engine is installed: MLX on an Apple silicon Mac, whisper.cpp elsewhere. A server install without
    either uses a remote endpoint instead."""
    return ENGINE is not None


def unload() -> None:
    """Drop the cached model: the small live one, or the accurate one after a final-while-recording meeting."""
    if ENGINE is None:
        return
    with _lock:
        ENGINE.unload()
