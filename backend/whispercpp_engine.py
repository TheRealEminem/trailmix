"""Local transcription with whisper.cpp (through pywhispercpp). One of local_engine.py's engines: the one used
where MLX isn't, so an Intel Mac, Windows (x64 and ARM) and Linux. It runs on the CPU (AVX2 / NEON), or the GPU
where the build has one.

Models are single ggml files from Hugging Face, named "<repo>:<file>" (see models.py).
"""
import gc
import math
import os
import zlib

import numpy as np

NAME = "whisper.cpp"
REPO = "ggerganov/whisper.cpp"
LIVE_MODEL = f"{REPO}:ggml-base.bin"
FINAL_MODEL = f"{REPO}:ggml-large-v3-turbo-q5_0.bin"  # a third the size of the full one, nearly as accurate
FINAL_MODEL_RAM_GB = 1.5

_model = None
_model_name: str | None = None


def _threads() -> int:
    import psutil

    return max(1, min(8, psutil.cpu_count(logical=False) or os.cpu_count() or 4))


def _load(model: str):
    global _model, _model_name
    if _model_name != model:
        from pywhispercpp.model import Model

        import models

        unload()
        _model = Model(models.path(model), redirect_whispercpp_logs_to=None, n_threads=_threads())
        _model_name = model
    return _model


def _compression_ratio(text: str) -> float:
    """As Whisper works it out: a line that repeats itself compresses well."""
    data = text.encode()
    return len(data) / len(zlib.compress(data)) if data else 0.0


def run(audio: np.ndarray, model: str, prompt: str | None, language: str | None) -> list[dict]:
    found = _load(model).transcribe(
        np.ascontiguousarray(audio, dtype=np.float32),
        extract_probability=True,
        language=language or "auto",
        no_context=True,  # avoids repetition loops on long recordings
        initial_prompt=prompt or "",
    )
    segments = []
    for seg in found:
        text = seg.text.strip()
        if not text:
            continue
        # The geometric mean of the token probabilities, so its log is Whisper's average log probability.
        p = seg.probability
        segments.append({
            "start": seg.t0 / 100, "end": seg.t1 / 100, "text": text,
            "avg_logprob": math.log(p) if p and p > 0 and not math.isnan(p) else 0.0,
            "no_speech_prob": 0.0,  # whisper.cpp drops those segments itself (no_speech_thold)
            "compression_ratio": _compression_ratio(text),
        })
    return segments


def loaded() -> str | None:
    return _model_name if _model is not None else None


def available() -> bool:
    import importlib.util

    return importlib.util.find_spec("pywhispercpp") is not None


def unload() -> None:
    global _model, _model_name
    _model, _model_name = None, None
    gc.collect()
