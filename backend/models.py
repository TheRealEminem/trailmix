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

import local_engine
import settings
import contextlib

log = logging.getLogger("trailmix")

# Rough download sizes, for the progress bar until the real total is known.
APPROX_BYTES = {"base": 145e6, "tiny": 75e6, "small": 480e6, "medium": 1.5e9, "large": 1.6e9}


class NotReady(RuntimeError):
    """A speech model is still downloading."""


class _Job:
    def __init__(self, repo: str):
        self.repo = repo
        self.total = 0.0
        self.before = 0.0
        self.error: str | None = None
        self.done = threading.Event()


_jobs: dict[str, _Job] = {}
_lock = threading.Lock()


def _hub_cache() -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE

    return Path(HF_HUB_CACHE)


def _split(model: str) -> tuple[str, str | None]:
    """(repo, file): a model is a whole Hugging Face repo (MLX models), or one file in one, "<repo>:<file>"
    (whisper.cpp's ggml models, which all share a repo)."""
    repo, _, file = model.rpartition(":")
    return (repo, file) if repo and "/" in repo and "/" not in file else (model, None)


def path(model: str) -> str:
    """Where `model` is on disk (downloading it if need be): its folder, or for a single-file model the file."""
    if Path(model).exists():
        return model
    from huggingface_hub import hf_hub_download, snapshot_download

    repo, file = _split(model)
    return hf_hub_download(repo, file) if file else snapshot_download(repo)


def installed(repo: str) -> bool:
    if Path(repo).exists():  # a model folder or file on disk
        return True
    try:
        from huggingface_hub import hf_hub_download, snapshot_download

        name, file = _split(repo)
        if file:
            hf_hub_download(name, file, local_files_only=True)
        else:
            snapshot_download(name, local_files_only=True)
        return True
    except Exception:
        return False


def _size_guess(repo: str) -> float:
    name = repo.lower()
    return next((size for key, size in APPROX_BYTES.items() if key in name), 0.0)


def _downloaded_bytes(repo: str) -> float:
    blobs = _hub_cache() / ("models--" + _split(repo)[0].replace("/", "--")) / "blobs"
    total = 0
    for f in blobs.glob("*"):
        with contextlib.suppress(OSError):
            total += f.stat().st_size
    return total


def _run(job: _Job) -> None:
    try:
        from huggingface_hub import HfApi

        repo, file = _split(job.repo)
        job.before = _downloaded_bytes(job.repo)  # other models' files from the same repo, already here
        try:
            info = HfApi().model_info(repo, files_metadata=True)
            job.total = float(sum(s.size or 0 for s in info.siblings if file in (None, s.rfilename)))
        except Exception:
            job.total = _size_guess(job.repo)
        log.info("Downloading speech model %s", job.repo)
        path(job.repo)
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
    return min(0.99, max(0.0, _downloaded_bytes(repo) - job.before) / job.total)


def ensure(repo: str, on_progress=None) -> None:
    """Blocks until the model is installed, downloading it if need be. Raises RuntimeError if that fails."""
    if not local_engine.available() or installed(repo):
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
    if not local_engine.available() or installed(repo):
        return
    if _locked():
        raise NotReady(LOCKED + ".")
    start(repo)
    p = progress(repo)
    raise NotReady("Live draft starts once the speech model has downloaded" + (f" ({p:.0%})." if p is not None else "."))


def wanted() -> list[tuple[str, str, str]]:
    return [
        ("live", local_engine.LIVE_MODEL, "Live draft model"),
        ("final", local_engine.final_model(), "Transcription model"),
    ]


def choices(cfg: dict) -> list[dict]:
    """The accurate models this engine offers (local_engine.catalog), each with whether it's downloaded, whether
    it's the one used, and how it's rated (model_ranks.py)."""
    import model_ranks

    using = local_engine.final_model(cfg)
    return [{**m, "installed": installed(m["id"]), "in_use": m["id"] == using,
             "score": model_ranks.score(local_engine.model_label(m["id"]))} for m in local_engine.catalog()]


def remove(repo: str) -> None:
    """Deletes a downloaded speech model from the list that isn't in use, to give its disk space back. Raises
    ValueError for anything else."""
    if repo not in {m["id"] for m in local_engine.catalog()}:
        raise ValueError("Trailmix only removes the speech models it lists")
    if repo in {r for _, r, _ in wanted()}:
        raise ValueError("That speech model is in use")
    job = _jobs.get(repo)
    if job and not job.done.is_set():
        raise ValueError("That speech model is still downloading")
    from huggingface_hub import scan_cache_dir

    name, file = _split(repo)
    cache = scan_cache_dir(_hub_cache())
    for cached in cache.repos:
        if cached.repo_id != name:
            continue
        if file is None:  # a whole repo (MLX models)
            cache.delete_revisions(*[r.commit_hash for r in cached.revisions]).execute()
            continue
        for revision in cached.revisions:  # one file of a shared repo (whisper.cpp's): its link and its blob
            for f in revision.files:
                if f.file_name == file:
                    f.file_path.unlink(missing_ok=True)
                    f.blob_path.unlink(missing_ok=True)
    log.info("Removed speech model %s", repo)


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
    cfg = settings.get_all()
    return {"local": local_engine.available() and cfg["transcribe_engine"] == "local", "models": items,
            "choices": choices(cfg), "automatic": local_engine.automatic_model(), "chosen": cfg["local_speech_model"]}


def prefetch(asked: bool = False) -> None:
    """Downloads whichever local models are missing, the small live one first. `asked`: someone asked for it (a
    model picked in Settings), so TRAILMIX_PREFETCH=0, which only stops the download at startup, doesn't apply."""
    if not local_engine.available() or settings.get_all()["transcribe_engine"] != "local":
        return
    if os.getenv("TRAILMIX_PREFETCH") == "0" and not asked:
        return

    def go():
        for _, repo, _ in wanted():
            start(repo)
            job = _jobs.get(repo)
            while job and not job.done.wait(1.0):
                pass

    threading.Thread(target=go, daemon=True, name="model-prefetch").start()
