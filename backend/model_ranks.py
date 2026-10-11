"""How good each AI model is at Trailmix's jobs (notes, tags, sorting), from 0 to 100, so it knows when the
model you have now is better than the one that did a meeting's work, and can offer to redo it.

The list (assets/model-ranks.json) ships with the app, and a newer copy is fetched from the Trailmix site once
a day (docs/model-ranks.json in the repository), so new models can be ranked without an app update. A local
model that isn't listed is judged by its size ("qwen9:14b" → about 70), which holds within a generation.
"""
import fnmatch
import json
import logging
import math
import re
import threading
import time
from pathlib import Path

import httpx

import database as db

log = logging.getLogger("trailmix.ranks")
BUILT_IN = Path(__file__).parent / "assets" / "model-ranks.json"
REMOTE = "https://therealeminem.github.io/trailmix/model-ranks.json"
REFRESH_S = 24 * 3600
BETTER_BY = 5  # a model must beat the old one by this much to be worth redoing work for
STRONG = 75    # work by an unrecorded model (from before Trailmix kept track) is only redone by a model this good

_lock = threading.Lock()
_ranks: dict | None = None
_fetched_at = 0.0


def _cache() -> Path:
    return db.DATA_DIR / "model-ranks.json"


def _load() -> dict:
    """The newest list we have: the built-in one, or a fetched one with a higher version."""
    global _ranks
    with _lock:
        if _ranks is None:
            best = json.loads(BUILT_IN.read_text())
            try:
                fetched = json.loads(_cache().read_text())
                if fetched.get("version", 0) >= best.get("version", 0) and fetched.get("updated", "") > best.get("updated", ""):
                    best = fetched
            except (OSError, ValueError):
                pass
            _ranks = best
        return _ranks


def refresh() -> None:
    """Fetches the latest list from the Trailmix site (at most once a day; quietly does nothing offline)."""
    global _ranks, _fetched_at
    import settings

    if time.time() - _fetched_at < REFRESH_S or settings.locked():
        return
    _fetched_at = time.time()
    try:
        r = httpx.get(REMOTE, timeout=10)
        data = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return
    if not data or not isinstance(data.get("models"), list):
        return
    current = _load()
    if (data.get("version", 0), data.get("updated", "")) > (current.get("version", 0), current.get("updated", "")):
        _cache().write_text(json.dumps(data))
        with _lock:
            _ranks = data
        log.info("Model rankings updated to %s", data.get("updated"))


def _by_size(model: str) -> float | None:
    """A local model's score from its size tag, e.g. "something:14b" → 71. None if there's no size."""
    found = re.search(r"[:\-_](\d+(?:\.\d+)?)b\b", model.lower())
    if not found:
        return None
    params = float(found.group(1))
    return round(min(82.0, max(20.0, 25 + 12 * math.log2(max(params, 0.5)))), 1)


def score(model: str | None) -> float | None:
    """How good a model is (0-100), or None if it's unknown."""
    if not model:
        return None
    name = model.lower().removeprefix("models/")
    for entry in _load().get("models", []):
        if fnmatch.fnmatch(name, entry["match"].lower()):
            return float(entry["score"])
    return _by_size(name)


def better(now: str | None, before: str | None) -> bool:
    """Is the model you have now clearly better than the one that did the work? Work whose model wasn't
    recorded (or isn't ranked) probably came from a model like yours, so only a strong one redoes it."""
    new = score(now)
    if new is None:
        return False
    old = score(before)
    return new >= STRONG if old is None else new >= old + BETTER_BY
