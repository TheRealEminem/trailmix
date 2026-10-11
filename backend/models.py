"""The speech models, downloaded from Hugging Face the first time they're needed.

Trailmix.app doesn't ship them (the big one is 1.6 GB). The server fetches them in the background as soon as
it starts, and reports progress so the app can show it; a meeting that needs a model still waiting simply
waits for it.
"""
import logging
import os
import threading
from pathlib import Path

# Hugging Face's newer "xet" downloader writes into a side cache and only shows the file when it is complete,
# which would leave the progress bar at 0% for minutes. Plain HTTP downloads grow the file as they go, at the
# same speed. (Must be set before huggingface_hub is first imported.)
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import mlx_engine  # noqa: E402
import settings  # noqa: E402

log = logging.getLogger("trailmix")

# Rough download sizes, for the progress bar until the real total is known.
APPROX_BYTES = {"base": 145e6, "tiny": 75e6, "small": 480e6, "medium": 1.5e9, "large": 1.6e9}


class NotReady(RuntimeError):
    """A speech model is still downloading."""


class _Job:
    def __init__(self, repo: str):
        self.repo = repo
        self.total = 0.0
        self.error: str | None = None
        self.done = threading.Event()


_jobs: dict[str, _Job] = {}
_lock = threading.Lock()


def _hub_cache() -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE

    return Path(HF_HUB_CACHE)


def installed(repo: str) -> bool:
    if Path(repo).exists():  # a model folder on disk
        return True
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(repo, local_files_only=True)
        return True
    except Exception:
        return False


def _size_guess(repo: str) -> float:
    name = repo.lower()
    return next((size for key, size in APPROX_BYTES.items() if key in name), 0.0)


def _downloaded_bytes(repo: str) -> float:
    blobs = _hub_cache() / ("models--" + repo.replace("/", "--")) / "blobs"
    total = 0
    for f in blobs.glob("*"):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _run(job: _Job) -> None:
    try:
        from huggingface_hub import HfApi, snapshot_download

        try:
            info = HfApi().model_info(job.repo, files_metadata=True)
            job.total = float(sum(s.size or 0 for s in info.siblings))
        except Exception:
            job.total = _size_guess(job.repo)
        log.info("Downloading speech model %s", job.repo)
        snapshot_download(job.repo)
        log.info("Speech model %s is ready", job.repo)
    except Exception as e:
        log.warning("Couldn't download %s: %s", job.repo, e)
        job.error = f"Couldn't download the speech model ({type(e).__name__}). Check your internet connection and try again."
    finally:
        job.done.set()


def _locked() -> bool:
    import settings

    return settings.locked()


LOCKED = "Lockdown mode is on, so the speech model can't be downloaded. Turn it off in Settings → Privacy to download it once"


def start(repo: str) -> None:
    """Begins downloading in the background (does nothing if it's installed or already on its way, or in
    Lockdown mode)."""
    if _locked():
        return
    with _lock:
        job = _jobs.get(repo)
        if job and not job.done.is_set():
            return
        if installed(repo):
            return
        job = _jobs[repo] = _Job(repo)
    threading.Thread(target=_run, args=(job,), daemon=True, name=f"download-{repo}").start()


def progress(repo: str) -> float | None:
    """0..1 while downloading, None if not downloading or the size isn't known yet."""
    job = _jobs.get(repo)
    if not job or job.done.is_set() or not job.total:
        return None
    return min(0.99, _downloaded_bytes(repo) / job.total)


def ensure(repo: str, on_progress=None) -> None:
    """Blocks until the model is installed, downloading it if need be. Raises RuntimeError if that fails."""
    if not mlx_engine.available() or installed(repo):
        return
    if _locked():
        raise RuntimeError(LOCKED)
    start(repo)
    job = _jobs[repo]
    while not job.done.wait(1.0):
        if on_progress:
            on_progress(progress(repo))
    if job.error:
        raise RuntimeError(job.error)


def require(repo: str) -> None:
    """For work that can't wait (a live draft): start the download and say so, without blocking."""
    if not mlx_engine.available() or installed(repo):
        return
    if _locked():
        raise NotReady(LOCKED + ".")
    start(repo)
    p = progress(repo)
    raise NotReady("Live draft starts once the speech model has downloaded" + (f" ({p:.0%})." if p is not None else "."))


def wanted() -> list[tuple[str, str, str]]:
    return [
        ("live", mlx_engine.LIVE_MODEL, "Live draft model"),
        ("final", mlx_engine.FINAL_MODEL, "Transcription model"),
    ]


def status() -> dict:
    items = []
    for kind, repo, label in wanted():
        job = _jobs.get(repo)
        downloading = bool(job and not job.done.is_set())
        have = installed(repo)
        items.append({
            "kind": kind, "repo": repo, "label": label, "installed": have, "downloading": downloading,
            "progress": progress(repo), "error": job.error if job and job.done.is_set() and not have else None,
            "size_gb": round((job.total if job and job.total else _size_guess(repo)) / 1e9, 2),
        })
    return {"local": mlx_engine.available() and settings.get_all()["transcribe_engine"] != "remote", "models": items}


def prefetch() -> None:
    """Downloads whichever local models are missing, the small live one first."""
    if not mlx_engine.available() or settings.get_all()["transcribe_engine"] == "remote":
        return
    if os.getenv("TRAILMIX_PREFETCH") == "0":
        return

    def go():
        for _, repo, _ in wanted():
            start(repo)
            job = _jobs.get(repo)
            while job and not job.done.wait(1.0):
                pass

    threading.Thread(target=go, daemon=True, name="model-prefetch").start()
