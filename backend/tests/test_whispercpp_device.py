"""Saying what whisper.cpp runs on (the GPU builds for Windows x64 and Linux load ggml's backends themselves): the
first GPU, as whisper.cpp picks it, else the CPU; and never in the way of a transcript."""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import whispercpp_engine as engine

CPU = ("CPU", "Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz", 0)
NVIDIA = ("Vulkan0", "NVIDIA GeForce RTX 3060", 1)
INTEL = ("Vulkan1", "Intel(R) UHD Graphics 770", 2)


@pytest.fixture
def gpu_build(monkeypatch, tmp_path):
    """A GPU build whose ggml has the given devices (no whisper.cpp needed)."""
    def with_devices(*devices):
        monkeypatch.setattr(engine, "_load_backends", lambda: None)
        monkeypatch.setattr(engine, "_ggml_folder", lambda: tmp_path)
        monkeypatch.setattr(engine, "_devices", lambda folder: list(devices))
        monkeypatch.setitem(sys.modules, "_pywhispercpp", SimpleNamespace(
            whisper_print_system_info=lambda: "WHISPER : COREML = 0 | OPENVINO = 0 |\n CPU : SSE3 = 1 | AVX2 = 1 | OPENMP = 1 |"))
    return with_devices


def test_a_graphics_card_is_named():
    assert engine.describe([CPU, NVIDIA]) == "the GPU (NVIDIA GeForce RTX 3060)"


def test_the_first_gpu_is_the_one_whisper_cpp_uses_even_an_integrated_one():
    assert engine.describe([CPU, INTEL, NVIDIA]) == "the integrated GPU (Intel(R) UHD Graphics 770)"
    assert engine.describe([NVIDIA, INTEL]) == "the GPU (NVIDIA GeForce RTX 3060)"


def test_without_a_gpu_it_is_the_cpu():
    assert engine.describe([CPU]) == "the CPU"
    assert engine.describe([]) == "the CPU"


def test_a_gpu_without_a_description_goes_by_its_name():
    assert engine.describe([("Vulkan0", " ", 1)]) == "the GPU (Vulkan0)"


def test_device_on_a_gpu_build(gpu_build):
    gpu_build(CPU, NVIDIA)
    assert engine.device() == "the GPU (NVIDIA GeForce RTX 3060)"
    gpu_build(CPU)
    assert engine.device() == "the CPU"


def test_builds_without_gpu_support_run_on_the_cpu(monkeypatch):
    monkeypatch.setattr(engine, "_load_backends", lambda: None)
    monkeypatch.setattr(engine, "_ggml_folder", lambda: None)
    assert engine.device() == "the CPU"


def test_device_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "_load_backends", lambda: None)
    monkeypatch.setattr(engine, "_ggml_folder", lambda: tmp_path)

    def broken(folder: Path):
        raise OSError("no ggml-base.dll")

    monkeypatch.setattr(engine, "_devices", broken)
    assert engine.device() == "an unknown device (couldn't tell: no ggml-base.dll)"


def test_system_info_names_the_device_and_keeps_whisper_cpps_own(gpu_build):
    gpu_build(CPU, NVIDIA)
    info = engine.system_info()
    assert info.startswith("on the GPU (NVIDIA GeForce RTX 3060); devices: CPU (Intel(R) Xeon(R)")
    assert "Vulkan0 (NVIDIA GeForce RTX 3060)" in info and "CPU : SSE3 = 1 | AVX2 = 1" in info


def test_loading_the_backends_says_what_it_runs_on_once(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(engine, "_backends_loaded", False)
    monkeypatch.setattr(engine, "_ggml_folder", lambda: None)  # (a wheel: nothing to load)
    monkeypatch.setattr(engine, "system_info", lambda: "on the CPU; CPU : AVX2 = 1 |")
    with caplog.at_level(logging.INFO, logger=engine.__name__):
        engine._load_backends()
        engine._load_backends()
    said = [r.getMessage() for r in caplog.records if r.name == engine.__name__]
    assert said == ["whisper.cpp on the CPU; CPU : AVX2 = 1 |"]


def test_not_being_able_to_say_is_never_in_the_way(monkeypatch, caplog):
    monkeypatch.setattr(engine, "_backends_loaded", False)
    monkeypatch.setattr(engine, "_ggml_folder", lambda: None)

    def broken():
        raise RuntimeError("ctypes said no")

    monkeypatch.setattr(engine, "system_info", broken)
    with caplog.at_level(logging.INFO, logger=engine.__name__):
        engine._load_backends()  # doesn't raise
    assert engine._backends_loaded
    assert any("couldn't say what it runs on: ctypes said no" in r.getMessage() for r in caplog.records)


def test_the_worker_without_logging_says_it_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(engine.log, "hasHandlers", lambda: False)
    engine._say("whisper.cpp on the CPU")
    assert capsys.readouterr().err == "whisper.cpp on the CPU\n"
