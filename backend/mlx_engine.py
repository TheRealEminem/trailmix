"""Local transcription with mlx-whisper (Apple Silicon / MLX accelerated).

Two models are used at different times so a meeting never competes with a heavy model:
  - LIVE_MODEL  (small, in this process, for a rough draft while the meeting runs)
  - FINAL_MODEL (large, in a throwaway subprocess - see transcribe_worker.py - after you stop; or in this
                 process during the meeting when "Final transcript while recording" is on, see live.py)
"""
import gc
import os
import threading

import numpy as np

LIVE_MODEL = os.getenv("TRAILMIX_LIVE_MODEL", "mlx-community/whisper-base-mlx")
FINAL_MODEL = os.getenv("TRAILMIX_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
FINAL_MODEL_RAM_GB = float(os.getenv("TRAILMIX_WHISPER_RAM_GB", "3.0"))  # rough peak, for the RAM gate
LANGUAGE = os.getenv("TRAILMIX_LANGUAGE", "en") or None  # None = auto-detect

# The live model is cached in a process-wide singleton, so live calls must not overlap.
_lock = threading.Lock()

_HALLUCINATIONS = {"you", "thank you.", "thanks for watching!", "thanks for watching.", "bye.", "."}


def run(audio: np.ndarray, model: str, prompt: str | None = None) -> list[dict]:
    """`prompt`: what was said just before (live chunks are a few seconds long; it gives the model the
    context a long recording would have, for names and words cut at a chunk's start)."""
    import mlx_whisper  # lazy: slow import, and lets the API boot without it

    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=model,
        language=LANGUAGE,
        condition_on_previous_text=False,  # avoids repetition loops on long recordings
        initial_prompt=prompt or None,
        verbose=None,
    )
    segments = []
    for seg in result.get("segments", []):
        text = seg["text"].strip()
        if not text or (seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -1.0):
            continue
        segments.append({
            "start": float(seg["start"]), "end": float(seg["end"]), "text": text,
            # Kept for cleanup.drop_hallucinations, which removes them before saving.
            "avg_logprob": float(seg.get("avg_logprob", 0)), "no_speech_prob": float(seg.get("no_speech_prob", 0)),
            "compression_ratio": float(seg.get("compression_ratio", 0)),
        })
    return segments


def filter_hallucinations(segs: list[dict]) -> list[dict]:
    return [s for s in segs if s["text"].lower() not in _HALLUCINATIONS]


def transcribe_live(audio: np.ndarray, prompt: str | None = None) -> list[dict] | None:
    """Rough draft of a short chunk. Returns None if the engine is busy (draft is skipped, not queued)."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        segs = run(audio, LIVE_MODEL, prompt)
    finally:
        _lock.release()
    return filter_hallucinations(segs)


def transcribe_final(audio: np.ndarray, prompt: str | None = None) -> list[dict]:
    """The accurate model on a chunk, in this process (final transcript while recording). Waits its turn."""
    with _lock:
        return run(audio, FINAL_MODEL, prompt)


def loaded() -> str | None:
    """Which model is in memory in this process, if any."""
    try:
        from mlx_whisper.transcribe import ModelHolder
    except ImportError:
        return None
    return ModelHolder.model_path if ModelHolder.model is not None else None


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
    """MLX only exists on Apple Silicon Macs; a Linux server install uses a remote endpoint instead."""
    import importlib.util

    return importlib.util.find_spec("mlx_whisper") is not None


def unload() -> None:
    """Drop the cached model: the small live one, or the accurate one after a final-while-recording meeting."""
    try:
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder
    except ImportError:
        return

    with _lock:
        ModelHolder.model = None
        ModelHolder.model_path = None
        gc.collect()
        mx.clear_cache()
