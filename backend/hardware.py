"""What this computer has for running AI models, read the same way on a Mac, Windows and Linux: its CPU, memory,
graphics chips (and how much memory of their own they have) and any NPU. resources.device_advice() turns it into
setup advice, and model_ranks.recommended_notes() into which local notes model to suggest.

Every reading is best effort: what can't be read is left out, never guessed. The parsers take plain text and
values, so the tests can give them any OS's output on any OS.
"""
import functools
import logging
import platform
import re
import shutil
import subprocess
from pathlib import Path

import psutil

import platform_words

log = logging.getLogger("trailmix")

GB = 1024**3
# On Apple silicon the graphics chip shares the main memory; Metal lets a model have about this much of it.
APPLE_GPU_SHARE = 0.7
# Without a graphics card a model runs on the CPU, in memory it shares with everything else, and slowly.
CPU_SHARE = 0.6
# A graphics card with less memory of its own than this is an integrated chip's: models run on the CPU anyway.
MIN_VRAM_GB = 4.0
# The graphics cards Ollama runs models on (CUDA and ROCm). Others are listed, but don't count as room for AI.
OLLAMA_GPU_VENDORS = ("nvidia", "amd")

_PCI_VENDORS = {"10de": "nvidia", "1002": "amd", "1022": "amd", "8086": "intel", "5143": "qualcomm", "106b": "apple"}
_NAME_VENDORS = (("nvidia", ("nvidia", "geforce", "quadro")), ("amd", ("amd", "radeon")), ("intel", ("intel",)),
                 ("qualcomm", ("qualcomm", "adreno", "snapdragon")), ("apple", ("apple",)))
_VENDOR_LABELS = {"nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel", "qualcomm": "Qualcomm", "apple": "Apple"}
# Linux NPU drivers (the accel subsystem), by the name of the driver.
_NPU_DRIVERS = {"intel_vpu": "Intel NPU", "amdxdna": "AMD NPU", "qaic": "Qualcomm Cloud AI"}

# Windows device classes, under HKEY_LOCAL_MACHINE.
WIN_CPU = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
WIN_DISPLAY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
WIN_NPU = r"SYSTEM\CurrentControlSet\Control\Class\{f01a9d53-3ff6-48d2-9f97-c8a7004be10c}"  # ComputeAccelerator


def _tidy(text: str) -> str:
    return " ".join(str(text or "").split())


def vendor_of(name: str, pci_id: str = "") -> str:
    """"nvidia", "amd", "intel", "qualcomm", "apple" or "other", from a PCI id ("PCI\\VEN_10DE&…", "0x10de") or the
    chip's name."""
    found = re.search(r"(?:ven_|0x)([0-9a-f]{4})", pci_id.lower())
    if found and found.group(1) in _PCI_VENDORS:
        return _PCI_VENDORS[found.group(1)]
    lower = name.lower()
    return next((vendor for vendor, words in _NAME_VENDORS if any(w in lower for w in words)), "other")


def _gpu(name: str, vendor: str, vram_bytes: int | None = None, unified: bool = False) -> dict:
    return {"name": _tidy(name), "vendor": vendor, "vram_gb": round(vram_bytes / GB, 1) if vram_bytes else None,
            "unified": unified}


# ── Windows ─────────────────────────────────────────────────────────────

def _number(value) -> int:
    """A registry number, stored as a number or as little-endian bytes depending on the driver."""
    if isinstance(value, bytes):
        return int.from_bytes(value[:8], "little")
    return value if isinstance(value, int) else 0


def windows_gpus(devices: list[dict]) -> list[dict]:
    """Graphics chips from the display class's device keys (their registry values). The memory comes from
    HardwareInformation.qwMemorySize, which is right past 4 GB, unlike WMI's AdapterRAM. Microsoft's own adapters
    (basic display, remote desktop) and virtual displays are left out."""
    gpus = []
    for d in devices:
        name = _tidy(d.get("DriverDesc"))
        if not name or _tidy(d.get("ProviderName")).lower() == "microsoft" or re.search(r"virtual|remote", name, re.I):
            continue
        memory = _number(d.get("HardwareInformation.qwMemorySize")) or _number(d.get("HardwareInformation.MemorySize"))
        gpus.append(_gpu(name, vendor_of(name, str(d.get("MatchingDeviceId") or "")), memory))
    return gpus


def windows_npu(devices: list[dict]) -> str:
    """The first NPU from the ComputeAccelerator class, e.g. "Intel(R) AI Boost" ("" if there's none)."""
    return next((name for d in devices if (name := _tidy(d.get("DriverDesc")))), "")


def _registry_class(path: str) -> list[dict]:
    """The values of each device (0000, 0001…) under a device class key."""
    import winreg

    devices = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except OSError:
        return devices
    with root:
        for i in range(256):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            if not sub.isdigit():  # "Properties" and "Configuration" aren't devices (and aren't readable)
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    values = {}
                    for j in range(512):
                        try:
                            name, value, _ = winreg.EnumValue(key, j)
                        except OSError:
                            break
                        values[name] = value
                    devices.append(values)
            except OSError:
                continue
    return devices


def _read_windows() -> tuple[str, list[dict], str]:
    import winreg

    cpu = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WIN_CPU) as key:
            cpu = _tidy(winreg.QueryValueEx(key, "ProcessorNameString")[0])
    except OSError:
        pass
    return cpu, windows_gpus(_registry_class(WIN_DISPLAY)), windows_npu(_registry_class(WIN_NPU))


# ── Linux ───────────────────────────────────────────────────────────────

def linux_cpu(cpuinfo: str) -> str:
    """The CPU's name from /proc/cpuinfo ("" where it has none, as on many ARM boards)."""
    for key in ("model name", "Hardware", "Model"):
        found = re.search(rf"^{key}\s*:\s*(.+)$", cpuinfo, re.M)
        if found:
            return _tidy(found.group(1))
    return ""


def nvidia_smi_gpus(out: str) -> list[dict]:
    """From `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits` (memory in MiB)."""
    gpus = []
    for line in out.splitlines():
        name, _, mib = line.rpartition(",")
        try:
            gpus.append(_gpu(name, "nvidia", int(float(mib) * 1024**2)))
        except ValueError:
            continue
    return gpus


def linux_drm_gpus(cards: list[dict]) -> list[dict]:
    """Graphics chips from /sys/class/drm: each card's PCI vendor ("0x1002") and, for AMD, its own memory in bytes
    (mem_info_vram_total). There's no name there, so the vendor's stands in."""
    gpus = []
    for card in cards:
        vendor = vendor_of("", card.get("vendor") or "")
        if vendor == "other":
            continue
        vram = card.get("vram")
        gpus.append(_gpu(f"{_VENDOR_LABELS.get(vendor, vendor)} graphics", vendor, int(vram) if vram else None))
    return gpus


def linux_npu(drivers: list[str]) -> str:
    """An NPU's name from the drivers of /sys/class/accel's devices ("" if there's none)."""
    return next((_NPU_DRIVERS.get(d, "NPU") for d in drivers if d), "")


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _read_linux() -> tuple[str, list[dict], str]:
    cpu = linux_cpu(_read(Path("/proc/cpuinfo")))
    gpus = []
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5).stdout
            gpus = nvidia_smi_gpus(out)
        except (OSError, subprocess.TimeoutExpired):
            pass
    cards = []
    for card in sorted(Path("/sys/class/drm").glob("card*")):
        if re.fullmatch(r"card\d+", card.name):  # not its connectors (card0-HDMI-A-1)
            cards.append({"vendor": _read(card / "device" / "vendor"), "vram": _read(card / "device" / "mem_info_vram_total")})
    have_nvidia = any(g["vendor"] == "nvidia" for g in gpus)  # nvidia-smi already named them, with their memory
    gpus += [g for g in linux_drm_gpus(cards) if not (have_nvidia and g["vendor"] == "nvidia")]
    drivers = [(dev / "device" / "driver").resolve().name for dev in sorted(Path("/sys/class/accel").glob("accel*"))
               if (dev / "device" / "driver").exists()]
    return cpu, gpus, linux_npu(drivers)


# ── Mac ─────────────────────────────────────────────────────────────────

def _sysctl(name: str) -> str:
    try:
        return subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def mac_hardware(brand: str) -> tuple[str, list[dict], str]:
    """From the CPU's brand string ("Apple M4", "Intel(R) Core(TM) i7…"). Apple silicon's graphics chip shares its
    memory and every one has a Neural Engine. An Intel Mac's graphics aren't read: Ollama doesn't use them."""
    cpu = _tidy(brand)
    if cpu.startswith("Apple"):
        return cpu, [_gpu(f"{cpu} GPU", "apple", unified=True)], "Apple Neural Engine"
    return cpu, [], ""


# ── Together ────────────────────────────────────────────────────────────

def ai_memory(ram_gb: float, gpus: list[dict]) -> tuple[float, str]:
    """(GB, "gpu" | "cpu"): how much memory a local AI model can have here, and what runs it. Apple silicon: a share
    of the memory it shares with its graphics chip. An NVIDIA or AMD card: its own memory (Ollama runs a model there,
    much faster than on the CPU). Otherwise a share of main memory, on the CPU."""
    if any(g["unified"] for g in gpus):
        return round(ram_gb * APPLE_GPU_SHARE, 1), "gpu"
    cards = [g["vram_gb"] for g in gpus if g["vendor"] in OLLAMA_GPU_VENDORS and (g["vram_gb"] or 0) >= MIN_VRAM_GB]
    if cards:
        return max(cards), "gpu"
    return round(ram_gb * CPU_SHARE, 1), "cpu"


def gpu_label(gpus: list[dict]) -> str:
    """The graphics card worth naming, e.g. "NVIDIA GeForce RTX 4070 (12 GB)": the one with the most memory of its
    own. "" when there's none: a Mac's is part of its chip, and an integrated one makes no difference here."""
    cards = [g for g in gpus if not g["unified"] and (g["vram_gb"] or 0) >= MIN_VRAM_GB]
    if not cards:
        return ""
    best = max(cards, key=lambda g: g["vram_gb"])
    return f"{best['name']} ({best['vram_gb']:.0f} GB)"


_READERS = {"macos": lambda: mac_hardware(_sysctl("machdep.cpu.brand_string")), "windows": _read_windows,
            "linux": _read_linux}


@functools.cache
def profile() -> dict:
    """{"os", "arch", "cpu", "cores", "ram_gb", "gpus": [{"name", "vendor", "vram_gb", "unified"}], "npu",
    "ai_memory_gb", "ai_runs_on": "gpu" | "cpu"}. Read once per run: none of it changes while Trailmix runs."""
    which = platform_words.os_id()
    cpu, gpus, npu = "", [], ""
    if which in _READERS:
        try:
            cpu, gpus, npu = _READERS[which]()
        except Exception:  # an unusual machine only means Trailmix knows less about it
            log.exception("Reading this computer's hardware failed")
    ram = psutil.virtual_memory().total / GB
    room, runs_on = ai_memory(ram, gpus)
    return {"os": which, "arch": platform.machine().lower(), "cpu": cpu,
            "cores": psutil.cpu_count(logical=False) or psutil.cpu_count() or 0, "ram_gb": round(ram, 1),
            "gpus": gpus, "npu": npu, "ai_memory_gb": room, "ai_runs_on": runs_on}
