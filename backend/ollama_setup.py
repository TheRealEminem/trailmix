"""Getting a local summary model in place: is Ollama there, which model suits this computer, and pulling it
with progress the app can show (instead of a terminal `ollama pull`)."""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

import hardware
import llm_engine
import model_ranks

log = logging.getLogger("trailmix")


# The official Mac app: signed and notarized by Ollama (Infra Technologies, Inc). Trailmix only installs a
# download that carries that signature.
OLLAMA_ZIP = "https://ollama.com/download/Ollama-darwin.zip"
OLLAMA_TEAM = "3MU9H2V9Y9"
# The official Windows installer, signed by Ollama (Authenticode); it installs for the current user into
# %LOCALAPPDATA%\Programs\Ollama. Trailmix only runs a download whose signature is valid and Ollama's.
OLLAMA_SETUP_EXE = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_WINDOWS_SIGNER = "ollama"  # must be in the signer certificate's subject

_lock = threading.Lock()
_pull: dict = {}
_install: dict = {}


INSTALL_DIRS = [Path("/Applications"), Path.home() / "Applications"]  # first that's writable wins


def _windows_app_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"


# Linux: Ollama's own install script needs root (it puts ollama in /usr/local and sets up a system service), so
# Trailmix never runs it: it shows the command for you to run in a terminal, and watches for Ollama answering.
LINUX_INSTALL_COMMAND = "curl -fsSL https://ollama.com/install.sh | sh"
LINUX_INSTALL_PAGE = "https://ollama.com/download/linux"
LINUX_SERVICE_FILES = [Path("/etc/systemd/system/ollama.service"), Path("/usr/lib/systemd/system/ollama.service"),
                       Path("/lib/systemd/system/ollama.service")]
LINUX_BINARIES = [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]


def app_path() -> Path | None:
    """The Ollama app: Ollama.app on a Mac, "ollama app.exe" (the tray app that runs the server) on Windows, the
    ollama command on Linux."""
    if sys.platform.startswith("linux"):
        found = shutil.which("ollama")
        if found:
            return Path(found)
        return next((p for p in LINUX_BINARIES if p.exists()), None)
    if sys.platform == "win32":
        for name in ("ollama app.exe", "ollama.exe"):
            if (_windows_app_dir() / name).exists():
                return _windows_app_dir() / name
        found = shutil.which("ollama")
        return Path(found) if found else None
    for folder in INSTALL_DIRS:
        if (folder / "Ollama.app").exists():
            return folder / "Ollama.app"
    return None


def suggestion() -> dict:
    """{"model", "size_gb", …}: the notes model to suggest for this computer's memory or graphics card. The list
    (model_ranks.recommended_notes) is fetched with the rankings, so it can follow new models."""
    return model_ranks.recommended_notes(hardware.profile()["ai_memory_gb"])


def recommended() -> str:
    return suggestion()["model"]


def status(cfg: dict) -> dict:
    tags = llm_engine._ollama_tags(cfg)
    reachable = bool(tags) or _reachable(cfg)
    with _lock:
        pull = dict(_pull)
    with _lock:
        install = dict(_install)
    installed = app_path() is not None
    return {
        "can_install": sys.platform in ("darwin", "win32"),
        "installed": installed,
        **_linux_commands(installed),
        "install": install or None,
        "reachable": reachable,
        "models": [m["name"] for m in tags],
        "using": llm_engine.model_for("ollama", cfg) if tags else "",
        "recommended": suggestion()["model"],
        "recommended_gb": suggestion().get("size_gb"),
        "pull": pull or None,
    }


def _linux_commands(installed: bool) -> dict:
    """On Linux, what to run yourself (Trailmix runs nothing as root): the install command and Ollama's page for
    it, or, once installed, how to start it. None elsewhere."""
    if not sys.platform.startswith("linux"):
        return {"install_command": None, "install_page": None, "start_command": None}
    service = any(p.exists() for p in LINUX_SERVICE_FILES)
    return {
        "install_command": None if installed else LINUX_INSTALL_COMMAND,
        "install_page": LINUX_INSTALL_PAGE,
        "start_command": ("sudo systemctl start ollama" if service else "ollama serve") if installed else None,
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


# ── Installing Ollama itself (Mac and Windows; Linux shows LINUX_INSTALL_COMMAND instead) ──

def start_install(cfg: dict) -> bool:
    """Downloads, checks and installs the Ollama app, then opens it. Opens it if it's installed but not running."""
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
            app = _download_and_install_windows() if sys.platform == "win32" else _download_and_install()
        _set(status="Starting Ollama", progress=None)
        _open(app)
        for _ in range(60):
            if _reachable({"ollama_url": url}):
                _set(active=False, status="ready", progress=1.0)
                return
            time.sleep(1)
        raise RuntimeError("Ollama was installed but didn't start. Open it from Applications.")
    except Exception as e:
        log.warning("Installing Ollama failed: %s", e)
        _set(active=False, error=str(e)[:300])


def _open(app: Path) -> None:
    """Starts the Ollama app without bringing it to the front."""
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(app)], creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["/usr/bin/open", "-g", str(app)], check=True, timeout=20)  # -g: don't steal focus


def _download(url: str, target: Path) -> None:
    _set(status="Downloading Ollama")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=120)) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"Couldn't download Ollama (HTTP {r.status_code})")
        total = int(r.headers.get("content-length") or 0)
        got = 0
        with open(target, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                got += len(chunk)
                if total:
                    _set(progress=min(0.99, got / total))


def _download_and_install() -> Path:
    work = Path(tempfile.mkdtemp(prefix="trailmix-ollama-"))
    try:
        archive = work / "Ollama-darwin.zip"
        _download(OLLAMA_ZIP, archive)
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


def _download_and_install_windows() -> Path:
    work = Path(tempfile.mkdtemp(prefix="trailmix-ollama-"))
    try:
        setup = work / "OllamaSetup.exe"
        _download(OLLAMA_SETUP_EXE, setup)
        _set(status="Checking Ollama's signature", progress=None)
        _verify_windows(setup)
        _set(status="Installing Ollama")
        subprocess.run([str(setup), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], check=True, timeout=900)
        app = app_path()
        if app is None:
            raise RuntimeError("Ollama's installer finished, but Ollama isn't where it should be")
        return app
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify_windows(setup: Path) -> None:
    """Refuses a download whose Authenticode signature isn't valid, or isn't Ollama's."""
    path = str(setup).replace("'", "''")
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; $s.Status.ToString(); "
              "if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' }")
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=120)
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    problem = signature_problem(lines[0] if lines else "", lines[1] if len(lines) > 1 else "")
    if problem:
        raise RuntimeError(problem)


def signature_problem(status: str, subject: str) -> str | None:
    """Why a Windows download of Ollama isn't trusted (its signature's status and signer, as PowerShell's
    Get-AuthenticodeSignature reports them), or None when it is."""
    if status.strip().lower() != "valid":
        return "The Ollama download failed its signature check, so it wasn't installed"
    if OLLAMA_WINDOWS_SIGNER not in subject.lower():
        return "The Ollama download isn't signed by Ollama, so it wasn't installed"
    return None
