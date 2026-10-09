"""System resource readout and the RAM gate that guards the heavy models."""
import os
import shutil
import subprocess
import sys

import psutil

import database as db
import llm_engine
import mlx_engine
import settings

GB = 1024**3
# Extra free RAM to keep for macOS and your other apps on top of what a model needs.
HEADROOM_GB = float(os.getenv("TRAILMIX_RAM_HEADROOM_GB", "2"))
# Short on free RAM, a model still runs: macOS pages other memory out to swap on disk. That's only a bad idea
# when the disk is nearly full (swap needs room) or macOS already reports critical memory pressure.
SWAP_DISK_FREE = float(os.getenv("TRAILMIX_SWAP_DISK_FREE", "0.10"))  # share of the disk that must be free
psutil.cpu_percent(interval=None)  # prime: the first call always returns 0.0


def memory_pressure() -> int:
    """macOS's own verdict: 1 normal, 2 warning, 4 critical (1 where it can't be read)."""
    if sys.platform != "darwin":
        return 1
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        return int(out or 1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 1


def _disk_free_share() -> float:
    usage = shutil.disk_usage(db.DATA_DIR)
    return usage.free / usage.total if usage.total else 1.0


def dir_size(path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def snapshot() -> dict:
    vm, swap = psutil.virtual_memory(), psutil.swap_memory()
    return {
        "ram_total_gb": round(vm.total / GB, 1),
        "ram_available_gb": round(vm.available / GB, 1),
        "ram_used_percent": vm.percent,
        "swap_used_gb": round(swap.used / GB, 1),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "disk_free_gb": round(shutil.disk_usage(db.DATA_DIR).free / GB, 1),
        "disk_free_percent": round(_disk_free_share() * 100),
        "swap_disk_free_percent": round(SWAP_DISK_FREE * 100),
        "memory_pressure": memory_pressure(),
        "audio_gb": round(dir_size(db.AUDIO_DIR) / GB, 2),
        "headroom_gb": HEADROOM_GB,
        "final_model_gb": mlx_engine.FINAL_MODEL_RAM_GB,
        "ollama_model_gb": round(llm_engine.ollama_model_size_gb(settings.get_all()) or 0, 1),
    }


def has_room(required_gb: float) -> bool:
    """Enough free memory right now for a model this size, with the usual headroom (no swapping)."""
    return psutil.virtual_memory().available / GB >= required_gb + HEADROOM_GB


def check(required_gb: float, what: str) -> str | None:
    """Returns None if the model can run now, otherwise a human-readable reason to wait.

    Enough free RAM: go. Not enough: still go, letting macOS swap, unless that would be dire: the model
    doesn't fit in this Mac's memory at all, the disk has under 10% free for swap, or macOS already reports
    critical memory pressure."""
    vm = psutil.virtual_memory()
    available, total = vm.available / GB, vm.total / GB
    needed = required_gb + HEADROOM_GB
    if available >= needed:
        return None
    what = what.lower()
    if required_gb > total - 2:
        return (f"Waiting for memory: {what} needs about {required_gb:.1f} GB, more than this Mac can spare. "
                "Pick a smaller model in Settings, or proceed anyway.")
    if _disk_free_share() < SWAP_DISK_FREE:
        return (f"Waiting for memory: {available:.1f} GB is free and {what} needs about {needed:.1f} GB, and with "
                f"under {SWAP_DISK_FREE:.0%} of the disk free macOS has little room to swap. "
                "Free up some disk space or close some apps, or proceed anyway.")
    if memory_pressure() >= 4:
        return (f"Waiting for memory: macOS reports critical memory pressure and {what} needs about {needed:.1f} GB. "
                "It starts by itself when that eases. Close some apps, or proceed anyway.")
    return None
