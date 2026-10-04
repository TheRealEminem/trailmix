"""Getting a local summary model in place: is Ollama there, which model suits this Mac, and pulling it
with progress the app can show (instead of a terminal `ollama pull`)."""
import json
import logging
import threading

import httpx
import psutil

import llm_engine

log = logging.getLogger("trailmix")

# Good at following the notes format. 16 GB Macs and up get the 7B; smaller ones the 3B.
RECOMMENDED_BIG = "qwen2.5:7b"
RECOMMENDED_SMALL = "qwen2.5:3b"

_lock = threading.Lock()
_pull: dict = {}


def recommended() -> str:
    return RECOMMENDED_BIG if psutil.virtual_memory().total >= 15 * 1024**3 else RECOMMENDED_SMALL


def status(cfg: dict) -> dict:
    tags = llm_engine._ollama_tags(cfg)
    reachable = bool(tags) or _reachable(cfg)
    with _lock:
        pull = dict(_pull)
    return {
        "reachable": reachable,
        "models": [m["name"] for m in tags],
        "using": llm_engine.model_for("ollama", cfg) if tags else "",
        "recommended": recommended(),
        "pull": pull or None,
    }


def _reachable(cfg: dict) -> bool:
    try:
        return httpx.get(f"{cfg['ollama_url']}/api/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def start_pull(cfg: dict, model: str) -> bool:
    """Starts downloading a model into Ollama in the background. False if a download is already running."""
    with _lock:
        if _pull.get("active"):
            return False
        _pull.clear()
        _pull.update(model=model, active=True, progress=None, status="Starting", error=None)
    threading.Thread(target=_run, args=(cfg["ollama_url"], model), daemon=True, name="ollama-pull").start()
    return True


def _run(url: str, model: str) -> None:
    try:
        with httpx.stream("POST", f"{url}/api/pull", json={"model": model, "stream": True}, timeout=httpx.Timeout(60, read=600)) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"Ollama answered {r.status_code}")
            for line in r.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                with _lock:
                    _pull["status"] = msg.get("status", "")
                    if msg.get("total"):
                        _pull["progress"] = min(1.0, msg.get("completed", 0) / msg["total"])
        with _lock:
            _pull.update(active=False, progress=1.0, status="success")
    except Exception as e:
        log.warning("Pulling %s failed: %s", model, e)
        with _lock:
            _pull.update(active=False, error=f"Couldn't download {model}: {e}")
