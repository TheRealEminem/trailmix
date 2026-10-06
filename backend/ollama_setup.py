"""Getting a local summary model in place: is Ollama there, which model suits this Mac, and pulling it
with progress the app can show (instead of a terminal `ollama pull`)."""
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import psutil

import llm_engine

log = logging.getLogger("trailmix")

# Good at following the notes format. 16 GB Macs and up get the 7B; smaller ones the 3B.
RECOMMENDED_BIG = "qwen2.5:7b"
RECOMMENDED_SMALL = "qwen2.5:3b"

# The official Mac app: signed and notarized by Ollama (Infra Technologies, Inc). Trailmix only installs a
# download that carries that signature.
OLLAMA_ZIP = "https://ollama.com/download/Ollama-darwin.zip"
OLLAMA_TEAM = "3MU9H2V9Y9"

_lock = threading.Lock()
_pull: dict = {}
_install: dict = {}


INSTALL_DIRS = [Path("/Applications"), Path.home() / "Applications"]  # first that's writable wins


def app_path() -> Path | None:
    for folder in INSTALL_DIRS:
        if (folder / "Ollama.app").exists():
            return folder / "Ollama.app"
    return None


def recommended() -> str:
    return RECOMMENDED_BIG if psutil.virtual_memory().total >= 15 * 1024**3 else RECOMMENDED_SMALL


def status(cfg: dict) -> dict:
    tags = llm_engine._ollama_tags(cfg)
    reachable = bool(tags) or _reachable(cfg)
    with _lock:
        pull = dict(_pull)
    with _lock:
        install = dict(_install)
    return {
        "can_install": sys.platform == "darwin",
        "installed": app_path() is not None,
        "install": install or None,
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


# ── Installing Ollama itself (Mac) ──────────────────────────────────────

def start_install(cfg: dict) -> bool:
    """Downloads, checks and installs Ollama.app, then opens it. Opens it if it's installed but not running."""
    with _lock:
        if _install.get("active"):
            return False
        _install.clear()
        _install.update(active=True, progress=None, status="Starting", error=None)
    threading.Thread(target=_install_run, args=(cfg["ollama_url"],), daemon=True, name="ollama-install").start()
    return True


def _set(**fields) -> None:
    with _lock:
        _install.update(fields)


def _install_run(url: str) -> None:
    try:
        app = app_path()
        if app is None:
            app = _download_and_install()
        _set(status="Starting Ollama", progress=None)
        subprocess.run(["/usr/bin/open", "-g", str(app)], check=True, timeout=20)  # -g: don't steal focus
        for _ in range(60):
            if _reachable({"ollama_url": url}):
                _set(active=False, status="ready", progress=1.0)
                return
            time.sleep(1)
        raise RuntimeError("Ollama was installed but didn't start. Open it from Applications.")
    except Exception as e:
        log.warning("Installing Ollama failed: %s", e)
        _set(active=False, error=str(e)[:300])


def _download_and_install() -> Path:
    work = Path(tempfile.mkdtemp(prefix="trailmix-ollama-"))
    try:
        archive = work / "Ollama-darwin.zip"
        _set(status="Downloading Ollama")
        with httpx.stream("GET", OLLAMA_ZIP, follow_redirects=True, timeout=httpx.Timeout(60, read=120)) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"Couldn't download Ollama (HTTP {r.status_code})")
            total = int(r.headers.get("content-length") or 0)
            got = 0
            with open(archive, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        _set(progress=min(0.99, got / total))
        _set(status="Checking Ollama's signature", progress=None)
        subprocess.run(["/usr/bin/ditto", "-xk", str(archive), str(work / "x")], check=True, timeout=300)
        new = work / "x" / "Ollama.app"
        if not new.exists():
            raise RuntimeError("The Ollama download didn't contain Ollama.app")
        _verify(new)
        _set(status="Installing Ollama")
        for folder in INSTALL_DIRS:  # /Applications, or ~/Applications for someone who isn't an admin
            try:
                folder.mkdir(exist_ok=True)
                dest = folder / "Ollama.app"
                shutil.move(str(new), str(dest))
                return dest
            except PermissionError:
                continue
        raise RuntimeError("Couldn't put Ollama in Applications (no permission)")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify(app: Path) -> None:
    """Refuses anything not signed by Ollama's Apple developer team, or that Gatekeeper wouldn't accept."""
    check = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
    if check.returncode != 0:
        raise RuntimeError("The Ollama download failed its signature check, so it wasn't installed")
    info = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True).stderr
    if f"TeamIdentifier={OLLAMA_TEAM}" not in info:
        raise RuntimeError("The Ollama download isn't signed by Ollama, so it wasn't installed")
    gate = subprocess.run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)], capture_output=True, text=True)
    if gate.returncode != 0:
        raise RuntimeError("macOS didn't accept the Ollama download, so it wasn't installed")
