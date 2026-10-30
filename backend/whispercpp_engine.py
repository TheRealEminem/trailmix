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
import sys
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
# The accurate models it offers in Settings, smallest first (download size and rough peak memory in GB).
MODELS = [
    {"id": SMALL[0], "label": "Whisper Small", "size_gb": 0.2, "ram_gb": SMALL[1], "speed": "Fastest, least accurate"},
    {"id": TURBO[0], "label": "Whisper Large v3 Turbo", "size_gb": 0.6, "ram_gb": TURBO[1], "speed": "Fast and accurate"},
    {"id": f"{REPO}:ggml-large-v3-turbo.bin", "label": "Whisper Large v3 Turbo (full size)", "size_gb": 1.6,
     "ram_gb": 2.5, "speed": "A little more accurate, slower"},
    {"id": f"{REPO}:ggml-large-v3-q5_0.bin", "label": "Whisper Large v3", "size_gb": 1.1, "ram_gb": 2.0,
     "speed": "Most accurate, about three times as slow"},
]

_model = None
_model_name: str | None = None
_backends_loaded = False
_backends_lock = threading.Lock()


def _threads(meeting: bool = False) -> int:
    """All the cores (up to 8) after a meeting; half of them during one, so the call, the recorder and the window
    aren't starved on a slower computer."""
    return max(1, min(8, _cores() // 2 if meeting else _cores()))


# ggml's own libraries and its Vulkan backend, in a whisper.cpp built with its backends as libraries of their own
# (the GPU builds, see _ggml_folder).
_GGML = ("ggml.dll", "ggml-base.dll") if sys.platform == "win32" else ("libggml.so", "libggml-base.so")
_VULKAN = "ggml-vulkan.dll" if sys.platform == "win32" else "libggml-vulkan.so"


def _ggml_folder() -> Path | None:
    """Where the whisper.cpp built with its backends as libraries of their own keeps them (the GPU builds for Windows
    x64 and Linux: a CPU one for each generation of CPUs, and Vulkan for the GPU), next to the extension; None for
    the builds with them built in (pywhispercpp's wheels, the Intel Mac's, Windows ARM's), which run on the CPU.
    Told apart by Vulkan's backend: the Linux wheel has stray copies of libggml.so next to the extension too, which
    aren't the ones it uses."""
    import _pywhispercpp

    folder = Path(_pywhispercpp.__file__).parent
    return folder if all((folder / name).is_file() for name in (*_GGML, _VULKAN)) else None


def _devices(folder: Path) -> list[tuple[str, str, int]]:
    """ggml's devices, as (name, description, type: 0 CPU, 1 GPU, 2 integrated GPU, 3 accelerator)."""
    import ctypes

    ggml, base = (ctypes.CDLL(str(folder / name)) for name in _GGML)
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
    """Once: a whisper.cpp with its backends as libraries of their own has none until they're loaded: the best CPU
    one for this computer, and Vulkan where a graphics driver provides it (vulkan-1.dll, libvulkan.so.1; without
    one it doesn't load, and without a GPU it has no devices). whisper.cpp itself never loads them."""
    global _backends_loaded
    with _backends_lock:
        if _backends_loaded:
            return
        folder = _ggml_folder()
        if folder:
            import ctypes

            ctypes.CDLL(str(folder / _GGML[0])).ggml_backend_load_all_from_path(str(folder).encode())
        _backends_loaded = True
    try:
        _say(f"whisper.cpp {system_info()}")
    except Exception as e:  # (only the log's: never in the way of a transcript)
        _say(f"whisper.cpp: couldn't say what it runs on: {e}")


def _say(text: str) -> None:
    """To the server log: through logging in the server, and on stderr in the transcription worker (which has no
    logging set up; the server sends its stderr to the log)."""
    if log.hasHandlers():
        log.info(text)
    else:
        print(text, file=sys.stderr, flush=True)


def describe(devices: list[tuple[str, str, int]]) -> str:
    """What whisper.cpp runs on, in words, given ggml's devices: the first GPU it finds, as whisper.cpp picks one
    (a graphics card, or the graphics built into the processor), else the CPU."""
    for name, about, kind in devices:
        if kind in (1, 2):
            return f"the {'GPU' if kind == 1 else 'integrated GPU'} ({about.strip() or name})"
    return "the CPU"


def device() -> str:
    """What whisper.cpp runs on, in words: "the GPU (NVIDIA GeForce RTX 3060)", "the integrated GPU (...)" or "the
    CPU" (always, for the builds without GPU support). Never raises: it's for the log and for a tester."""
    try:
        _load_backends()
        folder = _ggml_folder()
        return describe(_devices(folder)) if folder else "the CPU"
    except Exception as e:
        return f"an unknown device (couldn't tell: {e})"


def system_info() -> str:
    """What whisper.cpp runs on (see device) and what it has, for the log: "on the CPU; devices: CPU (Intel(R)
    Xeon(R) ...); WHISPER : ... CPU : SSE3 = 1 | ... AVX2 = 1 | ..."."""
    import _pywhispercpp

    _load_backends()
    info = " ".join(_pywhispercpp.whisper_print_system_info().split())
    folder = _ggml_folder()
    if not folder:
        return f"on the CPU; {info}"
    devices = _devices(folder)
    listed = ", ".join(f"{name} ({about.strip()})" for name, about, _ in devices) or "none"
    return f"on {describe(devices)}; devices: {listed}; {info}"


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
