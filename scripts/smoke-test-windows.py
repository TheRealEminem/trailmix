#!/usr/bin/env python3
"""The release gate on Windows (scripts/smoke-test.sh is the Mac's): installs the built installer silently
into a temporary folder, checks the app brings its engine up and takes it down with it, then starts the
engine inside the install the way the app does and runs the checks shared with the Mac (scripts/smoke_test.py),
the shell's own self-test (audio files standing in for the mic and the meeting: hotkey-style start and stop,
marks, the live draft, Record pressed in the window, a transcript made while recording) and a look at its
window, and last uninstalls it. Nothing touches your real Trailmix data.

  python scripts/smoke-test-windows.py dist/Trailmix-0.19.0-windows-x64.exe

Transcription uses the small whisper-tiny model so it's quick; models are shared through HF_HOME.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_test  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
PORT = int(os.environ.get("TRAILMIX_SMOKE_PORT", "8797"))
URL = f"http://127.0.0.1:{PORT}"
APP_URL = f"http://127.0.0.1:{PORT + 2}"  # the port the installed app itself is pointed at
TINY = "ggerganov/whisper.cpp:ggml-tiny.bin"


class Failure(Exception):
    pass


def log(text: str) -> None:
    print(f"\n== {text}", flush=True)


def health(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def wait_for(url: str, seconds: float, what: str) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if health(url):
            return
        time.sleep(1)
    raise Failure(f"{what} didn't answer at {url}")


def wait_gone(url: str, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not health(url):
            return True
        time.sleep(0.5)
    return False


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def tail(path: Path, lines: int = 60) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def start_engine(install: Path, env: dict, log_path: Path) -> subprocess.Popen:
    """The engine exactly as the app runs it (desktop/src-tauri/src/server.rs), without the app around it."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    engine_env = {k: v for k, v in env.items() if not k.upper().startswith("PYTHON")}
    engine_env.update({
        "PATH": f"{install / 'bin'};{system_root}\\System32;{system_root}",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
        "TRAILMIX_UI_DIR": str(install / "ui"),
    })
    out = open(log_path, "ab")
    return subprocess.Popen(
        [str(install / "python" / "python.exe"), "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=install / "backend", env=engine_env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    installer = Path(argv[1]).resolve()
    if not installer.is_file():
        print(f"SMOKE TEST FAILED: {installer} isn't there", file=sys.stderr)
        return 1
    if health(URL) or health(APP_URL):
        print(f"SMOKE TEST FAILED: something is already running at {URL} or {APP_URL}", file=sys.stderr)
        return 1

    root = Path(tempfile.mkdtemp(prefix="trailmix-smoke-"))
    install = root / "app"
    data = root / "data"
    app_data = root / "app-data"
    data.mkdir()
    app_data.mkdir()
    hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")  # share downloaded models between runs
    # The environment the app gets: hermetic, small models, no windows.
    env = {
        **os.environ,
        "HF_HOME": hf_home, "TRAILMIX_DATA_DIR": str(data), "TRAILMIX_SERVER_URL": URL, "TRAILMIX_NO_OPEN": "1",
        "TRAILMIX_WHISPER_MODEL": TINY, "TRAILMIX_LIVE_MODEL": TINY, "TRAILMIX_WHISPER_RAM_GB": "0.5",
        # Not under test: a nearly full disk would park the first recording's notes (and the transcript behind them).
        "TRAILMIX_SWAP_DISK_FREE": "0",
        # No notes AI, as on GitHub's runners.
        "OLLAMA_URL": "http://127.0.0.1:9",
    }
    engine_log = root / "server.log"
    app_log = root / "app.log"
    me, them = SCRIPTS / "fixtures" / "me.wav", SCRIPTS / "fixtures" / "them.wav"
    processes: list[subprocess.Popen] = []
    exe = install / "Trailmix.exe"
    try:
        log(f"Installing {installer.name} silently into {install}")
        subprocess.run([str(installer), "/S", "/NS", f"/D={install}"], check=True, timeout=900)
        for part in (exe, install / "python" / "python.exe", install / "backend" / "main.py", install / "ui" / "index.html",
                     install / "bin" / "ffmpeg.exe", install / "VERSION"):
            if not part.exists():
                raise Failure(f"the install has no {part.relative_to(install)}")
        print(f"installed: {sum(p.stat().st_size for p in install.rglob('*') if p.is_file()) / 1e6:.0f} MB")

        log("The app starts its engine, and takes it down with it")
        app_env = {**env, "TRAILMIX_SERVER_URL": APP_URL, "TRAILMIX_DATA_DIR": str(app_data)}
        app = subprocess.Popen([str(exe)], env=app_env, stdin=subprocess.DEVNULL, stdout=open(app_log, "wb"), stderr=subprocess.STDOUT)
        processes.append(app)
        wait_for(APP_URL, 180, "the app's engine")
        print("the app's engine answered")
        kill_tree(app)  # as if the app crashed: its engine must go too, not linger on the port
        if not wait_gone(APP_URL, 30):
            raise Failure("the engine outlived the app")
        print("the engine stopped with the app")

        log("The engine, started as the app starts it")
        engine = start_engine(install, env, engine_log)
        processes.append(engine)
        wait_for(URL, 180, "the engine")

        log("The checks shared with the Mac")
        smoke_test.check_engine(URL, str(install / "ui"))
        smoke_test.transcribe(URL, str(them), ["login", "Wednesday", "pricing"])
        smoke_test.meetings(URL)

        log("The shell's self-test (audio files stand in for the mic and the meeting)")
        done = subprocess.run([str(exe), "--self-test", URL, str(me), str(them)], env=env, capture_output=True, text=True,
                              timeout=1500, encoding="utf-8", errors="replace")
        print(done.stdout)
        if done.stderr.strip():
            print(done.stderr[-4000:], file=sys.stderr)
        if done.returncode != 0:
            raise Failure(f"the shell's self-test failed (exit {done.returncode})")

        log("The window shows the web app")
        shot = root / "window.png"
        done = subprocess.run([str(exe), "--window-test", URL, str(shot), "6"], env=env, capture_output=True, text=True,
                              timeout=180, encoding="utf-8", errors="replace")
        print(done.stdout.strip())
        if done.returncode != 0 or not shot.is_file() or shot.stat().st_size < 1000:
            raise Failure("the window didn't show the web app")
        shutil.copy2(shot, installer.parent / "smoke-window.png")  # kept with the build, to look at
        print(f"saved {installer.parent / 'smoke-window.png'}")

        log("Uninstalling")
        kill_tree(engine)
        if not wait_gone(URL, 30):
            raise Failure("the engine didn't stop")
        subprocess.run([str(install / "uninstall.exe"), "/S", f"_?={install}"], check=True, timeout=600)
        if exe.exists():
            raise Failure("uninstalling left Trailmix.exe behind")
        print("uninstalled")
    except (Failure, smoke_test.Failed, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"SMOKE TEST FAILED: {e}", file=sys.stderr)
        print("--- server log (last 60 lines)", file=sys.stderr)
        print(tail(engine_log), file=sys.stderr)
        print("--- the app's own engine log", file=sys.stderr)
        print(tail(app_data / "logs" / "server.log", 30), file=sys.stderr)
        print("--- the app's output", file=sys.stderr)
        print(tail(app_log, 30), file=sys.stderr)
        return 1
    finally:
        for proc in processes:
            kill_tree(proc)
    print("\nSmoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
