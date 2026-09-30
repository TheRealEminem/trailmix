"""System resource readout and the RAM gate that guards the heavy models."""
import os
import shutil

import psutil

import database as db
import llm_engine
import mlx_engine
import settings

GB = 1024**3
# Extra free RAM to keep for macOS and your other apps on top of what a model needs.
HEADROOM_GB = float(os.getenv("TRAILMIX_RAM_HEADROOM_GB", "2"))
psutil.cpu_percent(interval=None)  # prime: the first call always returns 0.0


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
        "audio_gb": round(dir_size(db.AUDIO_DIR) / GB, 2),
        "headroom_gb": HEADROOM_GB,
        "final_model_gb": mlx_engine.FINAL_MODEL_RAM_GB,
        "ollama_model_gb": round(llm_engine.ollama_model_size_gb(settings.get_all()) or 0, 1),
    }


def check(required_gb: float, what: str) -> str | None:
    """Returns None if there's enough free RAM, otherwise a human-readable reason to wait."""
    available = psutil.virtual_memory().available / GB
    needed = required_gb + HEADROOM_GB
    if available >= needed:
        return None
    return (
        f"Only {available:.1f} GB of RAM is free. {what} needs about {required_gb:.1f} GB "
        f"plus {HEADROOM_GB:.0f} GB headroom. Close some apps, or proceed anyway."
    )
