"""whisper.cpp itself, on real speech: run with TRAILMIX_ENGINE_TEST=1 where pywhispercpp is installed
(requirements-whispercpp.txt). It downloads the tiny model (78 MB) the first time."""
import os
import wave
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pywhispercpp")
pytestmark = pytest.mark.skipif(os.getenv("TRAILMIX_ENGINE_TEST") != "1", reason="set TRAILMIX_ENGINE_TEST=1")

import models  # noqa: E402
import whispercpp_engine  # noqa: E402

SPEECH = Path(__file__).resolve().parents[2] / "scripts" / "fixtures" / "them.wav"
TINY = "ggerganov/whisper.cpp:ggml-tiny.bin"


def test_tiny_model_transcribes_the_fixture():
    with wave.open(str(SPEECH)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    models.path(TINY)
    try:
        segs = whispercpp_engine.run(audio, TINY, None, "en")
    finally:
        whispercpp_engine.unload()
    text = " ".join(s["text"] for s in segs).lower()
    assert all(w in text for w in ("login", "wednesday", "pricing")), text
    assert all(s["end"] > s["start"] for s in segs) and all(s["avg_logprob"] < 0 for s in segs)
