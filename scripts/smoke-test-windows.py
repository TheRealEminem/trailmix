#!/usr/bin/env python3
"""The release gate on Windows (scripts/smoke-test.sh is the Mac's): installs the built installer silently
into a temporary folder, checks the app brings its engine up and takes it down with it, then starts the
engine inside the install the way the app does and runs the checks shared with the Mac (scripts/smoke_test.py),
the shell's own self-test (audio files standing in for the mic and the meeting: start and stop with the global
shortcut, marks, the live draft, the sound check, the call reminder's cards driven by a stand-in Zoom, the tray's
live draft and Catch me up card asked with no AI and then a stand-in one, Record pressed in the window, a transcript
made while recording) and a look at its window, and last uninstalls it. Nothing touches your real
Trailmix data. (The self-test registers Trailmix's shortcuts, so quit Trailmix before running this on your own PC.)

  python scripts/smoke-test-windows.py dist/Trailmix-0.19.0-windows-x64.exe     (or -windows-arm64.exe, on an ARM PC)
  python scripts/smoke-test-windows.py dist/Trailmix-0.19.0-windows-x64.exe previous/Trailmix-0.18.0-windows-x64.exe

With a previous installer it first installs that one and lets its own updater install the new one from a
stand-in for GitHub's releases (named as release.yml names them), checking open at login on the way (the
Run key, put back as it was afterwards). The rest then runs on the updated app. Last, it goes back to the
previous version (Settings → Updates → Go back), passing over a newer release that has no installer for
Windows, and checks that version opens the meetings the new one left.

Transcription uses the small whisper-tiny model so it's quick; models are shared through HF_HOME.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_test  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
PORT = int(os.environ.get("TRAILMIX_SMOKE_PORT", "8797"))
URL = f"http://127.0.0.1:{PORT}"
FEED_PORT = PORT + 1
FEED = f"http://127.0.0.1:{FEED_PORT}"
APP_URL = f"http://127.0.0.1:{PORT + 2}"  # the port the installed app itself is pointed at
TINY = "ggerganov/whisper.cpp:ggml-tiny.bin"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


MACHINES = {"x64": 0x8664, "arm64": 0xAA64}  # PE header machine types


class Failure(Exception):
    pass


def pe_machine(path: Path) -> int:
    with open(path, "rb") as f:
        head = f.read(4096)
    pe = int.from_bytes(head[0x3C:0x40], "little")
    return int.from_bytes(head[pe + 4:pe + 6], "little")


def check_arch(install: Path, arch: str) -> None:
    """The parts that run are built for this arch: an x64 one in the ARM installer would still pass every other
    check here (Windows on ARM emulates x64), only slowly."""
    site = install / "python" / "Lib" / "site-packages"
    if not list(site.glob("_pywhispercpp*.pyd")):
        raise Failure("the install has no whisper.cpp")
    parts = [install / "Trailmix.exe", install / "python" / "python.exe", install / "bin" / "ffmpeg.exe",
             *(install / "python").glob("python3*.dll"), *site.glob("_pywhispercpp*.pyd"),
             *site.glob("*.dll")]  # (whisper.cpp's, and the runtime DLLs bundled with it)
    wrong = [str(p.relative_to(install)) for p in parts if pe_machine(p) != MACHINES[arch]]
    if wrong:
        raise Failure(f"not built for {arch}: {', '.join(wrong)}")
    print(f"built for {arch}: {len(parts)} binaries checked")


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


def install_silently(installer: Path, folder: Path) -> None:
    subprocess.run([str(installer), "/S", "/NS", f"/D={folder}"], check=True, timeout=900)


# MARK: Versions, the stand-in for GitHub, and the recorder's report

def installer_version(installer: Path) -> str:
    """The version an installer installs, from its version resource (the NSIS build writes it)."""
    done = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"(Get-Item -LiteralPath '{installer}').VersionInfo.ProductVersion"],
        capture_output=True, text=True, timeout=60)
    version = done.stdout.strip()
    if not version:
        found = re.match(r"Trailmix-(.+)-windows-[a-z0-9_]+\.exe$", installer.name)
        version = found.group(1) if found else ""
    if not version:
        raise Failure(f"couldn't tell which version {installer.name} is")
    return version


def app_version(exe: Path) -> str:
    done = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


def arch_of(installer: Path) -> str:
    found = re.search(r"-windows-([a-z0-9_]+)\.exe$", installer.name)
    return found.group(1) if found else "x64"


def publish(feed: Path, installer: Path, name: str) -> list[str]:
    """Puts an installer in the stand-in feed under a release's name, with its checksum as release.yml makes it."""
    shutil.copy2(installer, feed / name)
    digest = hashlib.sha256((feed / name).read_bytes()).hexdigest()
    (feed / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    return [name, f"{name}.sha256"]


def release_json(version: str, files: list[str]) -> dict:
    """GitHub's JSON for a release of these files."""
    return {"tag_name": f"v{version}", "prerelease": False, "draft": False, "html_url": f"{FEED}/",
            "assets": [{"name": f, "browser_download_url": f"{FEED}/{f}"} for f in files]}


def recorder() -> dict:
    try:
        return smoke_test.get(f"{URL}/api/recorder", timeout=5)
    except Exception:
        return {}


def ask(path: str, body: dict | None = None) -> None:
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(f"{URL}/api/recorder/{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except urllib.error.HTTPError as e:
        raise Failure(f"asking the app to {path} failed: {e.code} {e.read().decode('utf-8', 'replace')[:200]}") from e


def wait_report(test, seconds: float, nudge: bool = True) -> dict:
    """Waits for the recorder's report to pass `test`, asking it to check for updates now and then (as Check
    for updates does, in case its first look came before everything was up)."""
    deadline = time.time() + seconds
    rounds = 0
    report = {}
    while time.time() < deadline:
        report = recorder()
        if report.get("available") and test(report):
            return report
        rounds += 1
        if nudge and rounds % 10 == 5 and report.get("available"):
            try:
                ask("check-update")
            except Failure:
                pass
        time.sleep(1)
    return report


def wait_installed(update_log: Path, version: str, seconds: float) -> None:
    """The updater's helper writes "installed <version>" (or "couldn't install <version>") when it's done."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        lines = tail(update_log, 200).splitlines()
        if any(line.endswith(f" installed {version}") for line in lines):
            return
        problem = next((line for line in lines if f"couldn't install {version}" in line), None)
        if problem:
            raise Failure(f"the update didn't install: {problem}")
        time.sleep(1)
    raise Failure(f"{version} wasn't installed within {seconds:.0f}s")


def run_value(key: str):
    """The "Trailmix" value under a key in HKEY_CURRENT_USER, as (data, type), or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, found_kind = winreg.QueryValueEx(k, "Trailmix")
            return (value, found_kind)
    except OSError:
        return None


def put_back(key: str, saved) -> None:
    try:
        if saved is None:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "Trailmix")
        else:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "Trailmix", 0, saved[1], saved[0])
    except OSError:
        pass


def check_open_at_login(exe: Path) -> None:
    """Settings → Open at login, on and off: the Run key starts this Trailmix.exe, then doesn't."""
    saved = (run_value(RUN_KEY), run_value(APPROVED_KEY))
    try:
        ask("open-at-login", {"on": True})
        report = wait_report(lambda r: r.get("open_at_login") is True, 15, nudge=False)
        if report.get("open_at_login") is not True:
            raise Failure(f"turning on open at login didn't stick: {report.get('open_at_login')}")
        value = run_value(RUN_KEY)
        if not value or str(exe).lower() not in str(value[0]).lower() or "--at-login" not in str(value[0]):
            raise Failure(f"the Run key doesn't start {exe} at login: {value}")
        print(f"open at login: on ({value[0]})")
        ask("open-at-login", {"on": False})
        report = wait_report(lambda r: r.get("open_at_login") is False, 15, nudge=False)
        if report.get("open_at_login") is not False or run_value(RUN_KEY) is not None:
            raise Failure("turning off open at login left it on")
        print("open at login: off")
    finally:
        put_back(RUN_KEY, saved[0])
        put_back(APPROVED_KEY, saved[1])


def run_with_feed(exe: Path, env: dict, app_log: Path) -> subprocess.Popen:
    """Runs the installed app with its updater pointed at the stand-in feed, looking soon after it starts and
    not opening the new version once it's installed (as smoke-test.sh runs the Mac app)."""
    app_env = {**env, "TRAILMIX_UPDATE_URL": f"{FEED}/releases.json", "TRAILMIX_RELEASES_URL": f"{FEED}/releases.json",
               "TRAILMIX_UPDATE_DELAY": "2", "TRAILMIX_UPDATE_NO_RELAUNCH": "1"}
    app = subprocess.Popen([str(exe)], env=app_env, stdin=subprocess.DEVNULL, stdout=open(app_log, "ab"), stderr=subprocess.STDOUT)
    wait_for(URL, 180, "the app's engine")
    return app


def count_meetings() -> int:
    found = smoke_test.get(f"{URL}/api/meetings")
    return len(found if isinstance(found, list) else found["meetings"])


def main(argv: list[str]) -> int:
    # The console's code page can't show everything the app and its logs print: never fail over that.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    installer = Path(argv[1]).resolve()
    previous = Path(argv[2]).resolve() if len(argv) > 2 else None
    for given in (installer, previous):
        if given is not None and not given.is_file():
            print(f"SMOKE TEST FAILED: {given} isn't there", file=sys.stderr)
            return 1
    if health(URL) or health(APP_URL):
        print(f"SMOKE TEST FAILED: something is already running at {URL} or {APP_URL}", file=sys.stderr)
        return 1

    root = Path(tempfile.mkdtemp(prefix="trailmix-smoke-")).resolve()  # (the long name, not RUNNER~1)
    install = root / "app"
    data = root / "data"
    app_data = root / "app-data"
    feed = root / "feed"
    for folder in (data, app_data, feed):
        folder.mkdir()
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
    for name in ("TRAILMIX_UPDATE_URL", "TRAILMIX_RELEASES_URL", "TRAILMIX_UPDATE_DELAY", "TRAILMIX_UPDATE_NO_RELAUNCH"):
        env.pop(name, None)
    engine_log = root / "server.log"
    app_log = root / "app.log"
    update_log = data / "logs" / "update.log"
    me, them = SCRIPTS / "fixtures" / "me.wav", SCRIPTS / "fixtures" / "them.wav"
    processes: list[subprocess.Popen] = []
    exe = install / "Trailmix.exe"
    new_version = old_version = ""
    arch = arch_of(installer)
    try:
        if previous:
            new_version = installer_version(installer)
            log(f"Installing the previous version from {previous.name} silently into {install}")
            install_silently(previous, install)
            old_version = app_version(exe) or installer_version(previous)
            print(f"previous version: {old_version}, new version: {new_version}")
            if old_version == new_version:
                raise Failure("the previous installer is the same version as the new one")

            # A stand-in for GitHub's releases, offering the new installer under the name release.yml gives it.
            new_files = publish(feed, installer, f"Trailmix-{new_version}-windows-{arch}.exe")
            (feed / "releases.json").write_text(json.dumps([release_json(new_version, new_files)]))
            server = subprocess.Popen([sys.executable, "-m", "http.server", str(FEED_PORT), "--bind", "127.0.0.1", "--directory", str(feed)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(server)

            log("Running the previous version: it finds the update")
            old_app = run_with_feed(exe, env, app_log)
            processes.append(old_app)
            report = wait_report(lambda r: (r.get("update") or {}).get("state") == "available", 90)
            if (report.get("update") or {}).get("state") != "available":
                raise Failure(f"the previous version never offered the update: {json.dumps(report)[:600]}\n{tail(update_log, 20)}")
            caps = report.get("capabilities") or []
            if "auto_update" not in caps or "open_at_login" not in caps:
                raise Failure(f"the installed app doesn't report auto_update and open_at_login: {caps}")
            print(f"it offers {report['update'].get('version')}")

            log("Open at login, on and off")
            check_open_at_login(exe)

            log("Updating it the way you would: Update now")
            ask("update")
            try:
                old_app.wait(timeout=240)
            except subprocess.TimeoutExpired:
                raise Failure(f"the previous version didn't quit to update (it reports {json.dumps(recorder().get('update'))})")
            wait_installed(update_log, new_version, 600)
            got = app_version(exe)
            if got != new_version:
                raise Failure(f"after updating, Trailmix.exe is {got or 'unknown'}, not {new_version}")
            if not wait_gone(URL, 30):
                raise Failure("the previous version's engine outlived it")
            print(f"updated {old_version} -> {got}")
        else:
            log(f"Installing {installer.name} silently into {install}")
            install_silently(installer, install)
        for part in (exe, install / "python" / "python.exe", install / "backend" / "main.py", install / "ui" / "index.html",
                     install / "bin" / "ffmpeg.exe", install / "VERSION", install / "uninstall.exe"):
            if not part.exists():
                raise Failure(f"the install has no {part.relative_to(install)}")
        print(f"installed: {sum(p.stat().st_size for p in install.rglob('*') if p.is_file()) / 1e6:.0f} MB")
        check_arch(install, arch)

        log("The app starts its engine, and takes it down with it")
        app_env = {**env, "TRAILMIX_SERVER_URL": APP_URL, "TRAILMIX_DATA_DIR": str(app_data)}
        app = subprocess.Popen([str(exe)], env=app_env, stdin=subprocess.DEVNULL, stdout=open(app_log, "ab"), stderr=subprocess.STDOUT)
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

        log("An iPhone photo read on this PC (text recognition; its model downloads the first time)")
        smoke_test.read_photo(URL, str(SCRIPTS.parent / "backend" / "tests" / "fixtures" / "ocr-line.heic"),
                              ["roadmap", "Pricing", "Q3"])

        log("The shell's self-test (audio files stand in for the mic and the meeting, a stand-in Zoom for a call)")
        done = subprocess.run([str(exe), "--self-test", URL, str(me), str(them), f"--shots={root}"], env=env, capture_output=True,
                              text=True, timeout=1500, encoding="utf-8", errors="replace")
        print(done.stdout)
        if done.stderr.strip():
            print(done.stderr[-4000:], file=sys.stderr)
        # the call reminder's cards and the recording card, kept with the build to look at
        for card in ("smoke-reminder.png", "smoke-reminder-stop.png", "smoke-live-draft.png", "smoke-catch-up-no-ai.png", "smoke-catch-up.png"):
            if (root / card).is_file():
                shutil.copy2(root / card, installer.parent / card)
        if done.returncode != 0:
            raise Failure(f"the shell's self-test failed (exit {done.returncode})")
        if f"windows-{arch})" not in done.stdout:
            raise Failure(f"the app doesn't say it's windows-{arch}")
        if "ok    the sound check heard both sides" not in done.stdout:
            raise Failure("the self-test didn't run the sound check")
        if "ok    the answer is in the card" not in done.stdout or "ok    the card says so, in the same words" not in done.stdout:
            raise Failure("the self-test didn't ask Catch me up from the tray")

        log("The window shows the web app")
        shot = root / "window.png"
        done = subprocess.run([str(exe), "--window-test", URL, str(shot), "6"], env=env, capture_output=True, text=True,
                              timeout=180, encoding="utf-8", errors="replace")
        print(done.stdout.strip())
        if done.returncode != 0 or not shot.is_file() or shot.stat().st_size < 1000:
            raise Failure("the window didn't show the web app")
        kept = installer.parent / f"smoke-window-{arch}.png"
        shutil.copy2(shot, kept)  # kept with the build, to look at
        print(f"saved {kept}")

        meetings_before = count_meetings()
        kill_tree(engine)
        if not wait_gone(URL, 30):
            raise Failure("the engine didn't stop")

        if previous:
            log(f"Going back to {old_version} the way you would: Settings > Updates > Go back")
            # The stand-in now lists both versions, as GitHub does, plus a newer release built only for other
            # computers, which the updater has to pass over rather than offer.
            old_files = publish(feed, previous, f"Trailmix-{old_version}-windows-{arch}.exe")
            others = release_json("999.0.0", ["Trailmix-999.0.0-macos-arm64.dmg", "Trailmix-999.0.0-macos-arm64.dmg.sha256",
                                              "Trailmix-999.0.0-linux-x86_64.AppImage"])
            (feed / "releases.json").write_text(json.dumps([others, release_json(new_version, new_files), release_json(old_version, old_files)]))
            new_app = run_with_feed(exe, env, app_log)
            processes.append(new_app)
            report = wait_report(lambda r: (r.get("update") or {}).get("previous") == old_version
                                 and (r.get("update") or {}).get("state") in ("up-to-date", "available"), 60)
            update = report.get("update") or {}
            if update.get("previous") != old_version:
                raise Failure(f"it never offered to go back to {old_version}: {json.dumps(report)[:600]}\n{tail(update_log, 20)}")
            if update.get("state") == "available":
                raise Failure(f"it offered an update with no installer for Windows: {json.dumps(update)}")
            ask("go-back")
            try:
                new_app.wait(timeout=240)
            except subprocess.TimeoutExpired:
                raise Failure(f"it didn't quit to go back (it reports {json.dumps(recorder().get('update'))})")
            wait_installed(update_log, old_version, 600)
            got = app_version(exe)
            if got != old_version:
                raise Failure(f"after going back, Trailmix.exe is {got or 'unknown'}, not {old_version}")
            if not wait_gone(URL, 30):
                raise Failure("the new version's engine outlived it")

            log(f"{old_version} opens the meetings {new_version} left")
            engine = start_engine(install, env, engine_log)
            processes.append(engine)
            wait_for(URL, 180, "the engine")
            meetings_after = count_meetings()
            if meetings_after != meetings_before:
                raise Failure(f"{old_version} sees {meetings_after} meetings; {new_version} had {meetings_before}")
            print(f"went back {new_version} -> {got}, with all {meetings_after} meetings")
            kill_tree(engine)
            if not wait_gone(URL, 30):
                raise Failure("the engine didn't stop")

        log("Uninstalling")
        subprocess.run([str(install / "uninstall.exe"), "/S", f"_?={install}"], check=True, timeout=600)
        if exe.exists():
            raise Failure("uninstalling left Trailmix.exe behind")
        print("uninstalled")
    except (Failure, smoke_test.Failed, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        print(f"SMOKE TEST FAILED: {e}", file=sys.stderr)
        print("--- server log (last 60 lines)", file=sys.stderr)
        print(tail(engine_log), file=sys.stderr)
        print("--- the app's own engine log", file=sys.stderr)
        print(tail(app_data / "logs" / "server.log", 30), file=sys.stderr)
        if previous:
            print("--- the updating app's engine log", file=sys.stderr)
            print(tail(data / "logs" / "server.log", 30), file=sys.stderr)
            print("--- the update log", file=sys.stderr)
            print(tail(update_log, 40), file=sys.stderr)
        print("--- the app's output", file=sys.stderr)
        print(tail(app_log, 30), file=sys.stderr)
        return 1
    finally:
        for proc in processes:
            kill_tree(proc)
    done_with = f", updated from {old_version} and back again" if previous else ""
    print(f"\nSmoke test passed ({new_version or installer.name}{done_with}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
