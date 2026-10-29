"""Transcription on this computer: MLX on an Apple silicon Mac (mlx_engine.py), whisper.cpp everywhere else
(whispercpp_engine.py). Everything else in Trailmix calls this module, never an engine directly.

Two models are used at different times so a meeting never competes with a heavy model:
  - LIVE_MODEL  (small, in this process, for a rough draft while the meeting runs)
  - FINAL_MODEL (large, in a throwaway subprocess - see transcribe_worker.py - after you stop; or in this
                 process during the meeting when "Final transcript while recording" is on, see live.py)

TRAILMIX_LOCAL_ENGINE=mlx|whispercpp|none picks one by hand (the first one installed is used otherwise).
"""
import os
import threading

import numpy as np

import mlx_engine
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
FINAL_MODEL = os.getenv("TRAILMIX_WHISPER_MODEL") or (ENGINE or mlx_engine).FINAL_MODEL
FINAL_MODEL_RAM_GB = float(os.getenv("TRAILMIX_WHISPER_RAM_GB") or (ENGINE or mlx_engine).FINAL_MODEL_RAM_GB)
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


def transcribe_final(audio: np.ndarray, prompt: str | None = None) -> list[dict]:
    """The accurate model on a chunk, in this process (final transcript while recording). Waits its turn."""
    with _lock:
        return run(audio, FINAL_MODEL, prompt, meeting=True)


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
