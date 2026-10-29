import importlib
import math
from types import SimpleNamespace

import numpy as np
import pytest

import local_engine
import models
import whispercpp_engine


@pytest.fixture
def pick(monkeypatch):
    """Reloads local_engine with TRAILMIX_LOCAL_ENGINE set, and puts it back afterwards."""
    def choose(name: str):
        monkeypatch.setenv("TRAILMIX_LOCAL_ENGINE", name)
        return importlib.reload(local_engine)
    yield choose
    monkeypatch.delenv("TRAILMIX_LOCAL_ENGINE", raising=False)
    importlib.reload(local_engine)


def test_whisper_cpp_is_used_where_asked_with_its_own_models(pick):
    engine = pick("whispercpp")
    assert engine.ENGINE is whispercpp_engine and engine.NAME == "whisper.cpp"
    assert (engine.FINAL_MODEL, engine.FINAL_MODEL_RAM_GB) in (whispercpp_engine.TURBO, whispercpp_engine.SMALL)


def test_a_small_intel_cpu_gets_the_small_model():
    assert whispercpp_engine.pick_final("x86_64", 2)[0].endswith("ggml-small-q5_1.bin")  # a dual-core Intel Mac
    assert whispercpp_engine.pick_final("AMD64", 4) == whispercpp_engine.SMALL           # Windows reports it so
    assert whispercpp_engine.pick_final("x86_64", 8) == whispercpp_engine.TURBO
    assert whispercpp_engine.pick_final("arm64", 4) == whispercpp_engine.TURBO           # Windows ARM, Linux arm64


def test_no_engine_means_not_available(pick):
    engine = pick("none")
    assert not engine.available() and engine.loaded() is None
    engine.unload()  # nothing to do, and no error
    with pytest.raises(RuntimeError):
        engine.run(np.zeros(16000, dtype=np.float32), engine.FINAL_MODEL)


def test_model_labels_are_the_same_whichever_engine():
    assert local_engine.model_label("mlx-community/whisper-large-v3-turbo") == "whisper-large-v3-turbo"
    assert local_engine.model_label("mlx-community/whisper-base-mlx") == "whisper-base"
    assert local_engine.model_label("ggerganov/whisper.cpp:ggml-large-v3-turbo-q5_0.bin") == "whisper-large-v3-turbo-q5_0"
    assert local_engine.model_label("whisper-1") == "whisper-1"
    import model_ranks  # ranked like the MLX build of the same model
    assert model_ranks.score("whisper-large-v3-turbo-q5_0") == model_ranks.score("whisper-large-v3-turbo")


def test_a_model_is_a_whole_repo_or_one_file_in_one():
    assert models._split("mlx-community/whisper-base-mlx") == ("mlx-community/whisper-base-mlx", None)
    assert models._split("ggerganov/whisper.cpp:ggml-base.bin") == ("ggerganov/whisper.cpp", "ggml-base.bin")
    assert models._split(r"C:\Models\ggml-base.bin") == (r"C:\Models\ggml-base.bin", None)  # a Windows path


def test_a_single_file_model_downloads_just_that_file(monkeypatch):
    hub = pytest.importorskip("huggingface_hub")
    asked = []
    monkeypatch.setattr(hub, "hf_hub_download", lambda repo, file, **kw: asked.append((repo, file, kw)) or "/m/f.bin")
    monkeypatch.setattr(hub, "snapshot_download", lambda *a, **kw: pytest.fail("downloaded the whole repo"))
    assert models.path("ggerganov/whisper.cpp:ggml-base.bin") == "/m/f.bin"
    assert models.installed("ggerganov/whisper.cpp:ggml-base.bin")
    assert asked == [("ggerganov/whisper.cpp", "ggml-base.bin", {}),
                     ("ggerganov/whisper.cpp", "ggml-base.bin", {"local_files_only": True})]


def test_whisper_cpp_segments_come_out_as_whisper_ones(monkeypatch):
    found = [SimpleNamespace(t0=150, t1=420, text=" Hello there. ", probability=0.5),
             SimpleNamespace(t0=420, t1=430, text="  ", probability=0.9),
             SimpleNamespace(t0=500, t1=900, text="Again", probability=float("nan"))]
    calls = []
    fake = SimpleNamespace(transcribe=lambda audio, **kw: calls.append(kw) or found)
    monkeypatch.setattr(whispercpp_engine, "_load", lambda model: fake)
    segs = whispercpp_engine.run(np.zeros(16000, dtype=np.float32), "m", "Priya", None)
    assert [(s["start"], s["end"], s["text"]) for s in segs] == [(1.5, 4.2, "Hello there."), (5.0, 9.0, "Again")]
    assert segs[0]["avg_logprob"] == pytest.approx(math.log(0.5)) and segs[1]["avg_logprob"] == 0.0
    assert segs[0]["no_speech_prob"] == 0.0 and segs[0]["compression_ratio"] > 0
    assert calls[0]["language"] == "auto" and calls[0]["initial_prompt"] == "Priya" and calls[0]["no_context"]


def test_whisper_cpp_leaves_a_meeting_room(monkeypatch):
    monkeypatch.setattr(whispercpp_engine, "_cores", lambda: 8)
    calls = []
    monkeypatch.setattr(whispercpp_engine, "_load", lambda m: SimpleNamespace(transcribe=lambda a, **kw: calls.append(kw) or []))
    whispercpp_engine.run(np.zeros(1600, dtype=np.float32), "m", None, "en", meeting=True)
    whispercpp_engine.run(np.zeros(1600, dtype=np.float32), "m", None, "en")
    assert [c["n_threads"] for c in calls] == [4, 8]
    monkeypatch.setattr(whispercpp_engine, "_cores", lambda: 1)
    assert whispercpp_engine._threads(meeting=True) == 1
