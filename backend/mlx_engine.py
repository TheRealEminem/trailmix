"""Local transcription with mlx-whisper (Apple Silicon / MLX accelerated). One of local_engine.py's engines:
the one Trailmix.app uses on an Apple silicon Mac.
"""
import gc

import numpy as np

NAME = "MLX"
LIVE_MODEL = "mlx-community/whisper-base-mlx"
FINAL_MODEL = "mlx-community/whisper-large-v3-turbo"
FINAL_MODEL_RAM_GB = 3.0  # rough peak, for the RAM gate


def run(audio: np.ndarray, model: str, prompt: str | None, language: str | None, meeting: bool = False) -> list[dict]:
    import mlx_whisper  # lazy: slow import, and lets the API boot without it

    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=model,
        language=language,
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


def loaded() -> str | None:
    try:
        from mlx_whisper.transcribe import ModelHolder
    except ImportError:
        return None
    return ModelHolder.model_path if ModelHolder.model is not None else None


def available() -> bool:
    """MLX only exists on Apple Silicon Macs."""
    import importlib.util

    return importlib.util.find_spec("mlx_whisper") is not None


def unload() -> None:
    try:
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder
    except ImportError:
        return
    ModelHolder.model = None
    ModelHolder.model_path = None
    gc.collect()
    mx.clear_cache()
