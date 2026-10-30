import hardware
import llm_engine
import model_ranks
import resources

GB = 1024**3


def test_windows_graphics_come_from_the_display_class_with_their_real_memory():
    devices = [
        # qwMemorySize is right past 4 GB (WMI's AdapterRAM stops at 4).
        {"DriverDesc": "NVIDIA GeForce RTX 4070", "ProviderName": "NVIDIA", "MatchingDeviceId": "pci\\ven_10de&dev_2786",
         "HardwareInformation.qwMemorySize": 12 * GB},
        # Some drivers store it as bytes.
        {"DriverDesc": "AMD Radeon RX 7900 XTX", "ProviderName": "Advanced Micro Devices, Inc.",
         "HardwareInformation.qwMemorySize": (24 * GB).to_bytes(8, "little")},
        {"DriverDesc": "Intel(R) UHD Graphics 770", "ProviderName": "Intel Corporation",
         "HardwareInformation.MemorySize": 128 * 1024**2},
        {"DriverDesc": "Microsoft Basic Display Adapter", "ProviderName": "Microsoft"},
        {"DriverDesc": "Parsec Virtual Display Adapter", "ProviderName": "Parsec Cloud, Inc."},
        {"ProviderName": "NVIDIA"},  # no name: not a device worth listing
    ]
    gpus = hardware.windows_gpus(devices)
    assert [(g["name"], g["vendor"], g["vram_gb"]) for g in gpus] == [
        ("NVIDIA GeForce RTX 4070", "nvidia", 12.0),
        ("AMD Radeon RX 7900 XTX", "amd", 24.0),
        ("Intel(R) UHD Graphics 770", "intel", 0.1),
    ]
    assert hardware.windows_npu([{}, {"DriverDesc": " Intel(R)  AI Boost "}]) == "Intel(R) AI Boost"
    assert hardware.windows_npu([]) == ""


def test_linux_reads_the_cpu_nvidia_smi_drm_cards_and_accel_npus():
    assert hardware.linux_cpu("processor\t: 0\nmodel name\t: AMD Ryzen 9  7950X 16-Core Processor\n") == \
        "AMD Ryzen 9 7950X 16-Core Processor"
    assert hardware.linux_cpu("processor : 0\nHardware : BCM2835\n") == "BCM2835"
    assert hardware.linux_cpu("processor : 0\nCPU implementer : 0x41\n") == ""
    assert [(g["name"], g["vram_gb"]) for g in hardware.nvidia_smi_gpus("NVIDIA GeForce RTX 4090, 24564\nnonsense\n")] == \
        [("NVIDIA GeForce RTX 4090", 24.0)]
    cards = [{"vendor": "0x1002", "vram": str(16 * GB)}, {"vendor": "0x8086", "vram": ""}, {"vendor": "0x1af4"}]
    assert [(g["name"], g["vendor"], g["vram_gb"]) for g in hardware.linux_drm_gpus(cards)] == \
        [("AMD graphics", "amd", 16.0), ("Intel graphics", "intel", None)]
    assert hardware.linux_npu(["intel_vpu"]) == "Intel NPU"
    assert hardware.linux_npu(["something_new"]) == "NPU"
    assert hardware.linux_npu([]) == ""


def test_apple_silicon_shares_its_memory_with_its_gpu_and_an_intel_mac_runs_models_on_its_cpu():
    cpu, gpus, npu = hardware.mac_hardware("Apple M4 Pro")
    assert (cpu, npu) == ("Apple M4 Pro", "Apple Neural Engine")
    assert gpus[0]["unified"] and gpus[0]["vendor"] == "apple"
    assert hardware.mac_hardware("Intel(R) Core(TM) i7-8850H CPU @ 2.60GHz") == \
        ("Intel(R) Core(TM) i7-8850H CPU @ 2.60GHz", [], "")


def test_room_for_ai_is_a_macs_shared_memory_a_cards_own_or_a_share_of_a_pcs():
    apple = hardware.mac_hardware("Apple M4")[1]
    assert hardware.ai_memory(16, apple) == (11.2, "gpu")
    rtx = hardware.windows_gpus([{"DriverDesc": "NVIDIA GeForce RTX 4070", "HardwareInformation.qwMemorySize": 12 * GB}])
    assert hardware.ai_memory(64, rtx) == (12.0, "gpu")
    # An integrated chip, a small card, or one Ollama doesn't run models on: the CPU, with part of the memory.
    small = hardware.windows_gpus([{"DriverDesc": "NVIDIA GeForce MX450", "HardwareInformation.qwMemorySize": 2 * GB}])
    arc = hardware.windows_gpus([{"DriverDesc": "Intel(R) Arc(TM) A770", "HardwareInformation.qwMemorySize": 16 * GB}])
    assert hardware.ai_memory(32, small) == (19.2, "cpu")
    assert hardware.ai_memory(32, arc) == (19.2, "cpu")
    assert hardware.ai_memory(16, []) == (9.6, "cpu")
    assert hardware.gpu_label(rtx + small) == "NVIDIA GeForce RTX 4070 (12 GB)"
    assert hardware.gpu_label(apple) == ""
    assert hardware.gpu_label(small) == ""


def test_the_profile_reads_this_computer_and_survives_a_reader_that_fails(monkeypatch):
    p = hardware.profile()
    assert {"os", "arch", "cpu", "cores", "ram_gb", "gpus", "npu", "ai_memory_gb", "ai_runs_on"} <= set(p)
    assert p["ram_gb"] > 0 and p["ai_memory_gb"] > 0

    def broken():
        raise PermissionError("no access")

    monkeypatch.setitem(hardware._READERS, p["os"], broken)
    hardware.profile.cache_clear()
    try:
        fallback = hardware.profile()
        assert fallback["cpu"] == "" and fallback["gpus"] == [] and fallback["ai_runs_on"] == "cpu"
    finally:
        hardware.profile.cache_clear()


def test_the_suggested_notes_model_is_the_biggest_that_fits(monkeypatch, tmp_path):
    assert model_ranks.recommended_notes(5.6)["model"] == "qwen2.5:3b"   # an 8 GB Mac
    assert model_ranks.recommended_notes(11.2)["model"] == "qwen2.5:7b"  # a 16 GB Mac
    assert model_ranks.recommended_notes(16.8)["model"] == "qwen2.5:14b"  # a 24 GB Mac
    # A fetched list's suggestions win; one from before suggestions were listed falls back to the built-in ones.
    monkeypatch.setattr(model_ranks, "_load", lambda: {"recommend": {"notes": [{"model": "new:4b", "min_gb": 0},
                                                                               {"model": "new:30b", "min_gb": 24}]}})
    assert model_ranks.recommended_notes(30)["model"] == "new:30b"
    assert model_ranks.recommended_notes(2)["model"] == "new:4b"
    monkeypatch.setattr(model_ranks, "_load", lambda: {"models": []})
    assert model_ranks.recommended_notes(11.2)["model"] == "qwen2.5:7b"
    empty = tmp_path / "model-ranks.json"
    empty.write_text('{"models": []}')
    monkeypatch.setattr(model_ranks, "BUILT_IN", empty)
    assert model_ranks.recommended_notes(11.2)["model"] == "qwen2.5:7b"
    assert model_ranks.recommended_notes(1)["model"] == "qwen2.5:3b"


def _room(monkeypatch, gb: float):
    monkeypatch.setattr(hardware, "profile", lambda: {"ai_memory_gb": gb})


def test_ollama_picks_the_biggest_installed_model_that_fits_and_a_bigger_context_with_room(monkeypatch):
    tags = [
        {"name": "qwen2.5:7b", "size": int(4.7e9), "details": {"parameter_size": "7.6B"}},
        {"name": "qwen2.5:14b", "size": int(9.0e9), "details": {"parameter_size": "14.8B"}},
        {"name": "qwen2.5:32b", "size": int(19.9e9), "details": {"parameter_size": "32.8B"}},
    ]
    assert llm_engine.preferred_ollama_model(tags, 11.2) == "qwen2.5:7b"
    assert llm_engine.preferred_ollama_model(tags, 33.6) == "qwen2.5:32b"
    assert llm_engine.preferred_ollama_model(tags, None) == "qwen2.5:14b"  # another machine's Ollama: up to ~14B
    assert llm_engine.preferred_ollama_model(tags[2:], 11.2) == "qwen2.5:32b"  # the only one is still used

    here, there = {"ollama_url": "http://localhost:11434"}, {"ollama_url": "http://studio.local:11434"}
    _room(monkeypatch, 33.6)
    assert llm_engine.ollama_num_ctx(here) == llm_engine.OLLAMA_BIG_CTX
    assert llm_engine.ollama_num_ctx(there) == llm_engine.OLLAMA_NUM_CTX
    assert llm_engine.context_chars("ollama", here) > llm_engine.context_chars("ollama", there)
    _room(monkeypatch, 11.2)
    assert llm_engine.ollama_num_ctx(here) == llm_engine.OLLAMA_NUM_CTX


def test_ollama_is_asked_not_to_think_and_for_the_context_that_fits(monkeypatch):
    sent = {}

    class Reply:
        def json(self):
            return {"response": "Notes."}

    def post(url, **kw):
        sent.update(kw["json"])
        return Reply()

    monkeypatch.setattr(llm_engine, "_post", post)
    _room(monkeypatch, 44.8)
    cfg = {"ollama_url": "http://127.0.0.1:11434", "ollama_model": "qwen3:14b"}
    assert llm_engine._ollama(cfg, "Hi", None, "0") == "Notes."
    assert sent["think"] is False
    assert sent["options"]["num_ctx"] == llm_engine.OLLAMA_BIG_CTX


def _advice(monkeypatch, *, ram: float, cpu: str, gpus: list[dict]):
    room, runs_on = hardware.ai_memory(ram, gpus)
    monkeypatch.setattr(hardware, "profile", lambda: {"cpu": cpu, "gpus": gpus, "ai_memory_gb": room, "ai_runs_on": runs_on})

    class Memory:
        total = ram * GB

    class Disk:
        total, free = 1000 * GB, 400 * GB

    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: Memory())
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda path: Disk())
    return resources.device_advice()


def test_setup_advice_names_the_model_it_suggests_and_the_card_that_runs_it(monkeypatch):
    mac = _advice(monkeypatch, ram=16, cpu="Apple M4", gpus=hardware.mac_hardware("Apple M4")[1])
    assert (mac["verdict"], mac["gpu"], mac["notes_model"]) == ("local", "", "qwen2.5:7b")
    assert "mid-sized notes model (qwen2.5:7b)" in mac["notes"][0]

    big = _advice(monkeypatch, ram=36, cpu="Apple M3 Pro", gpus=hardware.mac_hardware("Apple M3 Pro")[1])
    assert big["notes_model"] == "qwen2.5:14b"
    assert "room for a larger local notes model (qwen2.5:14b)" in big["notes"][0]

    rtx = hardware.windows_gpus([{"DriverDesc": "NVIDIA GeForce RTX 4070", "HardwareInformation.qwMemorySize": 12 * GB}])
    pc = _advice(monkeypatch, ram=32, cpu="AMD Ryzen 7 7800X3D 8-Core Processor", gpus=rtx)
    assert (pc["chip"], pc["gpu"], pc["notes_model"]) == ("AMD Ryzen 7 7800X3D 8-Core Processor",
                                                          "NVIDIA GeForce RTX 4070 (12 GB)", "qwen2.5:7b")
    assert pc["notes"][0].startswith("The NVIDIA GeForce RTX 4070 (12 GB) graphics card runs a mid-sized notes model")

    small = _advice(monkeypatch, ram=8, cpu="Apple M1", gpus=hardware.mac_hardware("Apple M1")[1])
    assert (small["verdict"], small["notes_model"]) == ("mixed", "qwen2.5:3b")
    assert any("M1" in n for n in small["notes"])
