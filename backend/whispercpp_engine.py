"""Local transcription with whisper.cpp (through pywhispercpp). One of local_engine.py's engines: the one used
where MLX isn't, so an Intel Mac, Windows (x64 and ARM) and Linux. It runs on the CPU (AVX2 / NEON), or the GPU
where the build has one.

Models are single ggml files from Hugging Face, named "<repo>:<file>" (see models.py).
"""
import gc
import logging
import math
import os
import platform
import threading
import time
import zlib
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

NAME = "whisper.cpp"
REPO = "ggerganov/whisper.cpp"
LIVE_MODEL = f"{REPO}:ggml-base.bin"
TURBO = (f"{REPO}:ggml-large-v3-turbo-q5_0.bin", 1.5)  # a third the size of the full one, nearly as accurate
SMALL = (f"{REPO}:ggml-small-q5_1.bin", 0.6)


def pick_final(machine: str, cores: int) -> tuple[str, float]:
    """(model, RAM GB) for the final transcript. An Intel CPU with only a few cores (a dual- or quad-core Intel Mac)
    would take longer than the meeting with large-v3-turbo, so it gets the small model, several times faster; ARM
    chips and bigger Intel ones get turbo."""
    return SMALL if machine.lower() in ("x86_64", "amd64") and cores < 6 else TURBO


def _cores() -> int:
    import psutil

    return psutil.cpu_count(logical=False) or os.cpu_count() or 4


FINAL_MODEL, FINAL_MODEL_RAM_GB = pick_final(platform.machine(), _cores())

_model = None
_model_name: str | None = None
_backends_loaded = False
_backends_lock = threading.Lock()


def _threads(meeting: bool = False) -> int:
    """All the cores (up to 8) after a meeting; half of them during one, so the call, the recorder and the window
    aren't starved on a slower computer."""
    return max(1, min(8, _cores() // 2 if meeting else _cores()))


def _ggml_folder() -> Path | None:
    """Where the whisper.cpp built with its backends as DLLs of their own keeps them (the Windows x64 build: a CPU one
    for each generation of x64 CPUs, and Vulkan for the GPU), next to the extension; None for the builds with them
    built in (the wheels, the Mac's, ARM's), whose ggml has another name."""
    import _pywhispercpp

    folder = Path(_pywhispercpp.__file__).parent
    return folder if (folder / "ggml.dll").is_file() and (folder / "ggml-base.dll").is_file() else None


def _devices(folder: Path) -> list[tuple[str, str, int]]:
    """ggml's devices, as (name, description, type: 0 CPU, 1 GPU, 2 integrated GPU, 3 accelerator)."""
    import ctypes

    ggml, base = ctypes.CDLL(str(folder / "ggml.dll")), ctypes.CDLL(str(folder / "ggml-base.dll"))
    ggml.ggml_backend_dev_count.restype = ctypes.c_size_t
    ggml.ggml_backend_dev_get.restype, ggml.ggml_backend_dev_get.argtypes = ctypes.c_void_p, [ctypes.c_size_t]
    for fn in (base.ggml_backend_dev_name, base.ggml_backend_dev_description):
        fn.restype, fn.argtypes = ctypes.c_char_p, [ctypes.c_void_p]
    base.ggml_backend_dev_type.argtypes = [ctypes.c_void_p]
    found = []
    for i in range(ggml.ggml_backend_dev_count()):
        dev = ggml.ggml_backend_dev_get(i)
        found.append((base.ggml_backend_dev_name(dev).decode(errors="replace"),
                      base.ggml_backend_dev_description(dev).decode(errors="replace"), base.ggml_backend_dev_type(dev)))
    return found


def _load_backends() -> None:
    """Once: a whisper.cpp with its backends as DLLs has none until they're loaded, the best CPU one for this PC and
    Vulkan where a graphics driver provides it (vulkan-1.dll; without one, or without a GPU, it's left out)."""
    global _backends_loaded
    with _backends_lock:
        if _backends_loaded:
            return
        folder = _ggml_folder()
        if folder:
            import ctypes

            ctypes.CDLL(str(folder / "ggml.dll")).ggml_backend_load_all_from_path(str(folder).encode())
        _backends_loaded = True
    log.info("whisper.cpp: %s", system_info())


def system_info() -> str:
    """What whisper.cpp runs on (the first GPU it found, as it picks one, or the CPU) and what it has, for the log."""
    import _pywhispercpp

    _load_backends()
    info = " ".join(_pywhispercpp.whisper_print_system_info().split())
    folder = _ggml_folder()
    if not folder:
        return info
    devices = _devices(folder)
    gpus = [f"{name} ({about})" for name, about, kind in devices if kind in (1, 2)]
    listed = ", ".join(f"{name} ({about})" for name, about, _ in devices) or "none"
    return f"runs on {gpus[0] if gpus else 'the CPU'}; devices: {listed}; {info}"


def _load(model: str):
    global _model, _model_name
    if _model_name != model:
        from pywhispercpp.model import Model

        import models

        _load_backends()
        unload()
        _model = Model(models.path(model), redirect_whispercpp_logs_to=None, n_threads=_threads())
        _model_name = model
    return _model


def _compression_ratio(text: str) -> float:
    """As Whisper works it out: a line that repeats itself compresses well."""
    data = text.encode()
    return len(data) / len(zlib.compress(data)) if data else 0.0


def _meeting_limits(seconds: float) -> dict:
    """During a meeting a chunk must come back quickly, since Stop waits for the one being transcribed: no retries at
    higher temperatures (whisper.cpp's fallback, which on some audio retried one chunk for 40 seconds), and a deadline
    of twice the audio's length (at least 15 seconds), after which it stops with what it has."""
    deadline = time.monotonic() + max(15.0, 2 * seconds)
    return {"temperature_inc": 0.0, "abort_callback": lambda: time.monotonic() > deadline}


def run(audio: np.ndarray, model: str, prompt: str | None, language: str | None, meeting: bool = False) -> list[dict]:
    limits = _meeting_limits(len(audio) / 16000) if meeting else {}
    found = _load(model).transcribe(
        np.ascontiguousarray(audio, dtype=np.float32),
        n_threads=_threads(meeting),
        **limits,
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
