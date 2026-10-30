#!/usr/bin/env python3
"""The release gate on Linux (smoke-test-windows.py's counterpart): runs the built app headless, in a private X
server (Xvfb) and a private sound server (PulseAudio with null sinks: a speaker and a mic fed from files), checks
the app brings its engine up and takes it down with it, then starts the engine inside the app the way the app
does and runs the checks shared with the Mac (scripts/smoke_test.py), records the real mic, all apps' audio and
one app's on its own (--capture-test) while it plays speech into them and checks what it heard, finds a stand-in
Zoom holding the mic, runs the shell's own self-test (audio files standing in for the mic and the meeting:
shortcuts typed with XTest, marks, the live draft, the sound check, the call reminder's cards clicked, Record
pressed in the window, a transcript made while recording) and takes a picture of its window. Nothing touches
your real Trailmix data, display or sound.

  python3 scripts/smoke-test-linux.py dist/Trailmix-0.20.0-linux-x86_64.AppImage
  python3 scripts/smoke-test-linux.py dist/Trailmix-0.20.0-linux-x86_64.AppImage previous/Trailmix-0.19.0-linux-x86_64.AppImage
  python3 scripts/smoke-test-linux.py dist/Trailmix-0.20.0-linux-x86_64.deb      (or .rpm: installs it, as root or with sudo)

With a previous AppImage it first runs that one and lets its own updater put the new one in its place from a
stand-in for GitHub's releases (named as release.yml names them), checking open at login on the way. The rest
then runs on the updated app. Last, it goes back to the previous version (Settings > Updates > Go back), passing
over a newer release that has nothing for Linux, and checks that version opens the meetings the new one left.
A .deb or .rpm install only says an update is out (its package manager installs it): that's checked instead.

Needs Xvfb, PulseAudio (pulseaudio, pactl, paplay, parecord) and, for an AppImage, FUSE or nothing (it's run
with APPIMAGE_EXTRACT_AND_RUN when there's no FUSE). Transcription uses the small whisper-tiny model so it's
quick; models are shared through HF_HOME.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_test  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
PORT = int(os.environ.get("TRAILMIX_SMOKE_PORT", "8797"))
URL = f"http://127.0.0.1:{PORT}"
FEED_PORT = PORT + 1
FEED = f"http://127.0.0.1:{FEED_PORT}"
APP_URL = f"http://127.0.0.1:{PORT + 2}"  # the port the app itself is pointed at
TINY = "ggerganov/whisper.cpp:ggml-tiny.bin"
MACHINES = {"x86_64": 62, "arm64": 183}  # ELF machine types
THEM_WORDS = ["login", "Wednesday", "pricing"]
ME_WORDS = ["launch", "beta", "Tuesday"]


class Failure(Exception):
    pass


def log(text: str) -> None:
    print(f"\n== {text}", flush=True)


def elf_machine(path: Path) -> int:
    with open(path, "rb") as f:
        head = f.read(20)
    return int.from_bytes(head[18:20], "little") if head[:4] == b"\x7fELF" else -1


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


def kill_group(proc: subprocess.Popen) -> None:
    """Ends a process started in a session of its own, and everything in its process group."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def tail(path: Path, lines: int = 60) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def processes_named(name: str, sessions: list[int]) -> list[int]:
    """The processes called `name` in these sessions (the ones this test started its programs in)."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
                session = int(stat[stat.rindex(")") + 2:].split()[3])
                if (entry / "comm").read_text().strip() == name and session in sessions:
                    found.append(int(entry.name))
            except (OSError, ValueError, IndexError):
                pass
    return found


# MARK: A private display and sound server

class Desktop:
    """Xvfb, a D-Bus session and PulseAudio of our own, so nothing of yours is touched (or heard)."""

    def __init__(self, root: Path):
        self.root = root
        self.procs: list[subprocess.Popen] = []
        self.env: dict = {}
        self.pulse: str | None = None

    def start(self) -> None:
        if shutil.which("Xvfb"):
            display = next(n for n in range(97, 200) if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists())
            xvfb = subprocess.Popen(["Xvfb", f":{display}", "-screen", "0", "1440x960x24", "-nolisten", "tcp"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.procs.append(xvfb)
            deadline = time.time() + 15
            while not Path(f"/tmp/.X11-unix/X{display}").exists():
                if time.time() > deadline or xvfb.poll() is not None:
                    raise Failure("Xvfb didn't start")
                time.sleep(0.2)
            self.env["DISPLAY"] = f":{display}"
            print(f"a private X server on :{display}")
        elif os.environ.get("DISPLAY"):
            self.env["DISPLAY"] = os.environ["DISPLAY"]
            print(f"no Xvfb: using your display {self.env['DISPLAY']}")
        else:
            raise Failure("needs Xvfb (or a display)")
        self.env.update({"XDG_SESSION_TYPE": "x11", "GDK_BACKEND": "x11"})
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        self.env["XDG_RUNTIME_DIR"] = str(runtime)
        if shutil.which("dbus-daemon"):
            bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, start_new_session=True)
            self.procs.append(bus)
            self.env["DBUS_SESSION_BUS_ADDRESS"] = bus.stdout.readline().strip()
        self.start_pulse()

    def start_pulse(self) -> None:
        if not shutil.which("pulseaudio"):
            print("no pulseaudio here: the recording checks use files only")
            return
        home = self.root / "pulse-home"
        home.mkdir()
        sock = self.root / "pulse.sock"
        env = {**os.environ, "HOME": str(home), "XDG_RUNTIME_DIR": self.env["XDG_RUNTIME_DIR"], "XDG_CONFIG_HOME": str(home)}
        env.pop("PULSE_SERVER", None)
        modules = [
            f"module-native-protocol-unix socket={sock} auth-anonymous=1",
            "module-null-sink sink_name=speakers sink_properties=device.description=Speakers",
            "module-null-sink sink_name=micfeed sink_properties=device.description=MicFeed",
            "module-remap-source master=micfeed.monitor source_name=mic source_properties=device.description=TestMic",
        ]
        args = ["pulseaudio", "-n", "--daemonize=no", "--exit-idle-time=-1", "--disable-shm=yes", "--log-target=stderr"]
        for m in modules:
            args += ["-L", m]
        pulse = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=open(self.root / "pulse.log", "wb"), start_new_session=True)
        self.procs.append(pulse)
        self.pulse = f"unix:{sock}"
        deadline = time.time() + 15
        while subprocess.run(["pactl", "--server", self.pulse, "info"], capture_output=True).returncode != 0:
            if time.time() > deadline or pulse.poll() is not None:
                print(tail(self.root / "pulse.log", 20), file=sys.stderr)
                raise Failure("PulseAudio didn't start")
            time.sleep(0.3)
        subprocess.run(["pactl", "--server", self.pulse, "set-default-sink", "speakers"], check=True)
        subprocess.run(["pactl", "--server", self.pulse, "set-default-source", "mic"], check=True)
        self.env["PULSE_SERVER"] = self.pulse
        print("a private PulseAudio: a speaker and a mic, both null devices fed from files")

    def play(self, wav: Path, device: str, as_name: str | None = None) -> subprocess.Popen:
        """Plays a WAV into a sink (as a program called `as_name`, to tell one app from another)."""
        program = shutil.which("pacat")
        if as_name:
            copy = self.root / as_name
            if not copy.exists():
                shutil.copy2(program, copy)
            program = str(copy)
        return subprocess.Popen([program, "--playback", "--file-format=wav", f"--device={device}", str(wav)],
                                env={**os.environ, "PULSE_SERVER": self.pulse}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def record_as(self, name: str) -> subprocess.Popen:
        """A program called `name` recording from the mic (a stand-in for a call app holding it)."""
        copy = self.root / name
        shutil.copy2(shutil.which("pacat"), copy)
        return subprocess.Popen([str(copy), "--record", "--device=mic", "--raw"], env={**os.environ, "PULSE_SERVER": self.pulse},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        for proc in reversed(self.procs):
            kill_group(proc)


# MARK: The app, as an AppImage or installed from a package

class App:
    def __init__(self, package: Path, root: Path):
        self.package = package
        self.root = root
        self.kind = {".AppImage": "appimage", ".deb": "deb", ".rpm": "rpm"}.get(package.suffix)
        if not self.kind:
            raise Failure(f"{package.name} isn't an .AppImage, .deb or .rpm")
        found = re.search(r"-linux-([a-z0-9_]+)\.(AppImage|deb|rpm)$", package.name)
        self.arch = found.group(1) if found else "x86_64"
        self.appimage = root / "app" / "Trailmix.AppImage"  # where an AppImage is "installed" (and updated in place)
        self.resources = Path("/usr/lib/Trailmix")
        self.exe = Path("/usr/bin/Trailmix")
        self.env: dict = {}

    def sudo(self) -> list[str]:
        return [] if os.geteuid() == 0 else ["sudo"]

    def install(self, package: Path | None = None) -> None:
        package = package or self.package
        if self.kind == "appimage":
            self.appimage.parent.mkdir(exist_ok=True)
            if package.resolve() != self.appimage.resolve():
                shutil.copy2(package, self.appimage)
            self.appimage.chmod(0o755)
            fuse = Path("/dev/fuse").exists() and (shutil.which("fusermount") or shutil.which("fusermount3"))
            if not fuse or os.environ.get("APPIMAGE_EXTRACT_AND_RUN"):
                self.env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
                print("no FUSE here: the AppImage runs extracting itself first")
            extracted = self.root / "extracted"
            rmtree(extracted)
            extracted.mkdir()
            subprocess.run([str(self.appimage), "--appimage-extract"], cwd=extracted, check=True, stdout=subprocess.DEVNULL, timeout=600)
            self.resources = extracted / "squashfs-root" / "usr" / "lib" / "Trailmix"
            self.exe = extracted / "squashfs-root" / "usr" / "bin" / "Trailmix"
        elif self.kind == "deb":
            subprocess.run([*self.sudo(), "apt-get", "install", "-y", "--no-install-recommends", str(package.resolve())], check=True, timeout=900,
                           env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
        else:
            installer = shutil.which("dnf") or shutil.which("zypper") or shutil.which("yum")
            subprocess.run([*self.sudo(), installer, "install", "-y", str(package.resolve())], check=True, timeout=900)

    def uninstall(self) -> None:
        if self.kind == "deb":
            subprocess.run([*self.sudo(), "apt-get", "remove", "-y", "trailmix"], check=True, timeout=600)
        elif self.kind == "rpm":
            installer = shutil.which("dnf") or shutil.which("zypper") or shutil.which("yum")
            subprocess.run([*self.sudo(), installer, "remove", "-y", "trailmix"], check=True, timeout=600)

    def command(self) -> list[str]:
        return [str(self.appimage)] if self.kind == "appimage" else [str(self.exe)]

    def run(self, args: list[str], env: dict, timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run([*self.command(), *args], env={**env, **self.env}, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")

    def start(self, env: dict, log_path: Path, args: list[str] | None = None) -> subprocess.Popen:
        return subprocess.Popen([*self.command(), *(args or [])], env={**env, **self.env}, stdin=subprocess.DEVNULL,
                                stdout=open(log_path, "ab"), stderr=subprocess.STDOUT, start_new_session=True)

    def version(self, env: dict) -> str:
        return self.run(["--version"], env, 120).stdout.strip()


def rmtree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink()


def check_parts(app: App) -> None:
    r = app.resources
    for part in (app.exe, r / "python" / "bin" / "python3", r / "backend" / "main.py", r / "ui" / "index.html", r / "bin" / "ffmpeg", r / "VERSION"):
        if not part.exists():
            raise Failure(f"the app has no {part}")
    site = next((r / "python" / "lib").glob("python3.*")) / "site-packages"
    whisper = list(site.glob("_pywhispercpp*.so"))
    if not whisper:
        raise Failure("the app has no whisper.cpp")
    parts = [app.exe, r / "python" / "bin" / "python3", r / "bin" / "ffmpeg", *whisper]
    wrong = [str(p) for p in parts if elf_machine(p) != MACHINES[app.arch]]
    if wrong:
        raise Failure(f"not built for {app.arch}: {', '.join(wrong)}")
    not_runnable = [str(p) for p in parts if not os.access(p, os.X_OK) and p.suffix != ".so"]
    if not_runnable:
        raise Failure(f"not executable in the package: {', '.join(not_runnable)}")
    size = sum(p.stat().st_size for p in r.rglob("*") if p.is_file()) / 1e6
    print(f"the app's parts are there and built for {app.arch} ({size:.0f} MB of resources)")


def start_engine(app: App, env: dict, log_path: Path) -> subprocess.Popen:
    """The engine exactly as the app runs it (desktop/src-tauri/src/server.rs), without the app around it."""
    engine_env = {k: v for k, v in env.items() if not k.upper().startswith(("PYTHON", "LD_"))}
    engine_env.update({
        "PATH": f"{app.resources / 'bin'}:/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
        "TRAILMIX_UI_DIR": str(app.resources / "ui"),
    })
    return subprocess.Popen(
        [str(app.resources / "python" / "bin" / "python3"), "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=app.resources / "backend", env=engine_env, stdin=subprocess.DEVNULL, stdout=open(log_path, "ab"), stderr=subprocess.STDOUT,
        start_new_session=True,
    )


# MARK: The stand-in for GitHub, and the recorder's report

def publish(feed: Path, package: Path, name: str) -> list[str]:
    shutil.copy2(package, feed / name)
    digest = hashlib.sha256((feed / name).read_bytes()).hexdigest()
    (feed / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    return [name, f"{name}.sha256"]


def release_json(version: str, files: list[str]) -> dict:
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
    deadline = time.time() + seconds
    while time.time() < deadline:
        lines = tail(update_log, 200).splitlines()
        if any(line.endswith(f" installed {version}") for line in lines):
            return
        problem = next((line for line in lines if f"couldn't update to {version}" in line), None)
        if problem:
            raise Failure(f"the update didn't install: {problem}")
        time.sleep(1)
    raise Failure(f"{version} wasn't installed within {seconds:.0f}s")


def check_open_at_login(config: Path, app: App) -> None:
    """Settings > Open at login, on and off: the autostart entry starts this AppImage (or program), then is gone."""
    entry = config / "autostart" / "trailmix.desktop"
    ask("open-at-login", {"on": True})
    report = wait_report(lambda r: r.get("open_at_login") is True, 15, nudge=False)
    if report.get("open_at_login") is not True:
        raise Failure(f"turning on open at login didn't stick: {report.get('open_at_login')}")
    text = entry.read_text() if entry.is_file() else ""
    target = str(app.appimage if app.kind == "appimage" else app.exe)
    if f'Exec="{target}" --at-login' not in text:
        raise Failure(f"the autostart entry doesn't start {target} at login:\n{text}")
    print(f"open at login: on ({entry})")
    ask("open-at-login", {"on": False})
    report = wait_report(lambda r: r.get("open_at_login") is False, 15, nudge=False)
    if report.get("open_at_login") is not False or entry.exists():
        raise Failure("turning off open at login left it on")
    print("open at login: off")


def run_with_feed(app: App, env: dict, app_log: Path) -> subprocess.Popen:
    """Runs the app with its updater pointed at the stand-in feed, looking soon after it starts and not opening
    the new version once it's in place."""
    feed_env = {**env, "TRAILMIX_UPDATE_URL": f"{FEED}/releases.json", "TRAILMIX_RELEASES_URL": f"{FEED}/releases.json",
                "TRAILMIX_UPDATE_DELAY": "2", "TRAILMIX_UPDATE_NO_RELAUNCH": "1"}
    proc = app.start(feed_env, app_log)
    wait_for(URL, 240, "the app's engine")
    return proc


def count_meetings() -> int:
    found = smoke_test.get(f"{URL}/api/meetings")
    return len(found if isinstance(found, list) else found["meetings"])


# MARK: Recording for real

def check_capture(app: App, desk: Desktop, env: dict, root: Path) -> None:
    """The real sources a recording uses, on the private sound server: speech played into the mic and the
    speaker comes back from each, and one app's audio comes on its own (another app plays meanwhile)."""
    if not desk.pulse:
        print("(no sound server: skipped)")
        return
    me, them = SCRIPTS / "fixtures" / "me.wav", SCRIPTS / "fixtures" / "them.wav"
    zoom = desk.record_as("zoom")  # a stand-in Zoom holding the mic, for the call reminder's list
    time.sleep(1)
    listed = app.run(["--list-audio"], env, 60)
    print(listed.stdout.strip())
    if not re.search(r"\[zoom pid \d+\]\s+IN USE\s+\[Zoom\]", listed.stdout):
        raise Failure("--list-audio doesn't see the stand-in Zoom using the mic")
    caps = re.search(r"Recorder capabilities: (.*)", listed.stdout)
    caps = caps.group(1).split(", ") if caps else []
    for wanted in ("system_audio", "per_app_audio", "hotkeys", "call_reminder", "sound_check"):
        if wanted not in caps:
            raise Failure(f"the recorder doesn't offer {wanted} here: {caps}")
    out = root / "capture"
    # Once it's recording: your side into the mic; the meeting from a "call app" and, at the same time, something
    # else from another app.
    capture = subprocess.Popen([*app.command(), "--capture-test", "8", str(out), "callapp"], env={**env, **app.env},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    lines: list[str] = []
    for line in capture.stdout:
        lines.append(line)
        if line.startswith("app:") or "FAILED" in line:
            break
    players = [desk.play(me, "micfeed"), desk.play(them, "speakers", as_name="callapp"), desk.play(me, "speakers", as_name="otherapp")]
    try:
        rest, errors = capture.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        capture.kill()
        rest, errors = capture.communicate()
    done = subprocess.CompletedProcess(capture.args, capture.returncode, "".join(lines) + rest, errors)
    for p in [*players, zoom]:
        if p.poll() is None:
            p.kill()
        p.wait()
    print(done.stdout.strip())
    if done.returncode != 0:
        print(done.stderr[-2000:], file=sys.stderr)
        raise Failure("recording the real sources failed")
    levels = {m.group(1): float(m.group(2)) for m in re.finditer(r"^(\w+): [\d.]+ s, level ([\d.]+)", done.stdout, re.M)}
    for name in ("mic", "apps", "app"):
        if levels.get(name, 0) < 0.3:
            raise Failure(f"the {name} recording is silent ({levels})")
    # Each recording, transcribed: the mic and the one app are what was played into them, and only that.
    for name, wav_words in (("mic", ME_WORDS), ("app", THEM_WORDS)):
        smoke_test.transcribe(URL, str(out / f"{name}.wav"), wav_words)
    print("the mic, all apps' audio and one app's on its own were all recorded")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    package = Path(argv[1]).resolve()
    previous = Path(argv[2]).resolve() if len(argv) > 2 else None
    for given in (package, previous):
        if given is not None and not given.is_file():
            print(f"SMOKE TEST FAILED: {given} isn't there", file=sys.stderr)
            return 1
    if health(URL) or health(APP_URL):
        print(f"SMOKE TEST FAILED: something is already running at {URL} or {APP_URL}", file=sys.stderr)
        return 1

    root = Path(tempfile.mkdtemp(prefix="trailmix-smoke-")).resolve()
    data, app_data, feed, config = root / "data", root / "app-data", root / "feed", root / "config"
    for folder in (data, app_data, feed, config):
        folder.mkdir()
    hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    desk = Desktop(root)
    app = App(package, root)
    engine_log, app_log = root / "server.log", root / "app.log"
    update_log = data / "logs" / "update.log"
    me, them = SCRIPTS / "fixtures" / "me.wav", SCRIPTS / "fixtures" / "them.wav"
    processes: list[subprocess.Popen] = []
    new_version = old_version = ""
    installed = False
    try:
        log("A private display and sound server")
        desk.start()
        env = {
            **{k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "PULSE_SERVER")},
            **desk.env,
            "HF_HOME": hf_home, "TRAILMIX_DATA_DIR": str(data), "TRAILMIX_SERVER_URL": URL, "TRAILMIX_NO_OPEN": "1",
            "TRAILMIX_WHISPER_MODEL": TINY, "TRAILMIX_LIVE_MODEL": TINY, "TRAILMIX_WHISPER_RAM_GB": "0.5",
            "TRAILMIX_SWAP_DISK_FREE": "0", "OLLAMA_URL": "http://127.0.0.1:9",
            "XDG_CONFIG_HOME": str(config),  # (open at login's autostart entry goes here, not into yours)
        }
        if not desk.pulse:
            env.pop("PULSE_SERVER", None)
        for name in ("TRAILMIX_UPDATE_URL", "TRAILMIX_RELEASES_URL", "TRAILMIX_UPDATE_DELAY", "TRAILMIX_UPDATE_NO_RELAUNCH", "APPIMAGE", "APPDIR"):
            env.pop(name, None)

        if previous and app.kind == "appimage":
            log(f"Running the previous version, {previous.name}")
            app.install(previous)
            installed = True
            old_version = app.version(env)
            # The new one's version, from its own --version (run once from the file as given).
            done = subprocess.run([str(package), "--version"], env={**env, **app.env}, capture_output=True, text=True, timeout=120)
            new_version = done.stdout.strip()
            print(f"previous version: {old_version}, new version: {new_version}")
            if not old_version or not new_version or old_version == new_version:
                raise Failure(f"can't tell the versions apart ({old_version!r}, {new_version!r})")
            new_files = publish(feed, package, f"Trailmix-{new_version}-linux-{app.arch}.AppImage")
            (feed / "releases.json").write_text(json.dumps([release_json(new_version, new_files)]))
            server = subprocess.Popen([sys.executable, "-m", "http.server", str(FEED_PORT), "--bind", "127.0.0.1", "--directory", str(feed)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            processes.append(server)
            old_app = run_with_feed(app, env, app_log)
            processes.append(old_app)
            report = wait_report(lambda r: (r.get("update") or {}).get("state") == "available", 90)
            if (report.get("update") or {}).get("state") != "available":
                raise Failure(f"the previous version never offered the update: {json.dumps(report)[:600]}\n{tail(update_log, 20)}")
            caps = report.get("capabilities") or []
            if "auto_update" not in caps or "open_at_login" not in caps:
                raise Failure(f"the AppImage doesn't report auto_update and open_at_login: {caps}")
            if report.get("platform") != f"linux-{app.arch}":
                raise Failure(f"it says it's {report.get('platform')}, not linux-{app.arch}")
            print(f"it offers {report['update'].get('version')}")

            log("Open at login, on and off")
            check_open_at_login(config, app)

            log("Updating it the way you would: Update now")
            ask("update")
            try:
                old_app.wait(timeout=300)
            except subprocess.TimeoutExpired:
                raise Failure(f"the previous version didn't quit to update (it reports {json.dumps(recorder().get('update'))})")
            wait_installed(update_log, new_version, 60)
            got = app.version(env)
            if got != new_version:
                raise Failure(f"after updating, the AppImage is {got or 'unknown'}, not {new_version}")
            if not wait_gone(URL, 30):
                raise Failure("the previous version's engine outlived it")
            print(f"updated {old_version} -> {got}, in place: {app.appimage}")
            app.install(app.appimage)  # (looks inside the updated one from here on)
        else:
            log(f"Installing {package.name}")
            app.install()
            installed = True
        check_parts(app)

        log("The app starts its engine, and takes it down with it")
        app_env = {**env, "TRAILMIX_SERVER_URL": APP_URL, "TRAILMIX_DATA_DIR": str(app_data)}
        first = app.start(app_env, app_log)
        processes.append(first)
        wait_for(APP_URL, 240, "the app's engine")
        print("the app's engine answered")
        for pid in processes_named("Trailmix", [first.pid]):
            os.kill(pid, signal.SIGKILL)  # as if the app crashed: its engine must go too, not linger on the port
        kill_group(first)
        if not wait_gone(APP_URL, 30):
            raise Failure("the engine outlived the app")
        print("the engine stopped with the app")

        if app.kind != "appimage":
            log("A package install says when there's an update, and doesn't install it itself")
            others = release_json("999.0.0", ["Trailmix-999.0.0-linux-x86_64.AppImage", "Trailmix-999.0.0-linux-arm64.AppImage",
                                              "Trailmix-999.0.0-linux-x86_64.AppImage.sha256", "Trailmix-999.0.0-linux-arm64.AppImage.sha256"])
            (feed / "releases.json").write_text(json.dumps([others]))
            server = subprocess.Popen([sys.executable, "-m", "http.server", str(FEED_PORT), "--bind", "127.0.0.1", "--directory", str(feed)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            processes.append(server)
            packaged = run_with_feed(app, env, app_log)
            processes.append(packaged)
            report = wait_report(lambda r: (r.get("update") or {}).get("state") == "available", 90)
            update = report.get("update") or {}
            if update.get("state") != "available" or update.get("manual") is not True or "auto_update" in (report.get("capabilities") or []):
                raise Failure(f"it should say 999.0.0 is out, to download: {json.dumps(report)[:600]}")
            print(f"it says {update.get('version')} is out, to download from {update.get('notes')}")
            log("Open at login, on and off")
            check_open_at_login(config, app)
            kill_group(packaged)
            if not wait_gone(URL, 30):
                raise Failure("the engine outlived the app")

        log("The engine, started as the app starts it")
        engine = start_engine(app, env, engine_log)
        processes.append(engine)
        wait_for(URL, 240, "the engine")

        log("The checks shared with the Mac")
        smoke_test.check_engine(URL, str(app.resources / "ui"))
        smoke_test.transcribe(URL, str(them), THEM_WORDS)
        smoke_test.meetings(URL)

        log("Recording the real mic and other apps (on the private sound server)")
        check_capture(app, desk, env, root)

        log("The shell's self-test (audio files stand in for the mic and the meeting, a stand-in Zoom for a call)")
        done = app.run(["--self-test", URL, str(me), str(them), f"--shots={root}"], env, 1500)
        print(done.stdout)
        if done.stderr.strip():
            print(done.stderr[-4000:], file=sys.stderr)
        for card in ("smoke-reminder.png", "smoke-reminder-stop.png"):
            if (root / card).is_file():
                shutil.copy2(root / card, package.parent / card.replace(".png", f"-linux-{app.arch}.png"))
        if done.returncode != 0:
            try:
                found = smoke_test.get(f"{URL}/api/meetings")
                for m in (found if isinstance(found, list) else found.get("meetings", [])):
                    print(f"  meeting {m.get('id')}: {m.get('status')} {str(m.get('error') or m.get('status_detail') or '')[:200]}", file=sys.stderr)
            except Exception as e:
                print(f"  (couldn't list the meetings: {e})", file=sys.stderr)
            raise Failure(f"the shell's self-test failed (exit {done.returncode})")
        if f"linux-{app.arch})" not in done.stdout:
            raise Failure(f"the app doesn't say it's linux-{app.arch}")

        log("The window shows the web app")
        shot = root / "window.png"
        done = app.run(["--window-test", URL, str(shot), "6"], env, 180)
        print(done.stdout.strip())
        if done.returncode != 0 or not shot.is_file() or shot.stat().st_size < 1000:
            print(done.stderr[-3000:], file=sys.stderr)
            raise Failure("the window didn't show the web app")
        kept = package.parent / f"smoke-window-linux-{app.arch}.png"
        shutil.copy2(shot, kept)
        print(f"saved {kept}")

        meetings_before = count_meetings()
        kill_group(engine)
        if not wait_gone(URL, 30):
            raise Failure("the engine didn't stop")

        if previous and app.kind == "appimage":
            log(f"Going back to {old_version} the way you would: Settings > Updates > Go back")
            old_files = publish(feed, previous, f"Trailmix-{old_version}-linux-{app.arch}.AppImage")
            others = release_json("999.0.0", ["Trailmix-999.0.0-macos-arm64.dmg", "Trailmix-999.0.0-macos-arm64.dmg.sha256",
                                              "Trailmix-999.0.0-windows-x64.exe", "Trailmix-999.0.0-windows-x64.exe.sha256"])
            (feed / "releases.json").write_text(json.dumps([others, release_json(new_version, new_files), release_json(old_version, old_files)]))
            new_app = run_with_feed(app, env, app_log)
            processes.append(new_app)
            report = wait_report(lambda r: (r.get("update") or {}).get("previous") == old_version
                                 and (r.get("update") or {}).get("state") in ("up-to-date", "available"), 60)
            update = report.get("update") or {}
            if update.get("previous") != old_version:
                raise Failure(f"it never offered to go back to {old_version}: {json.dumps(report)[:600]}\n{tail(update_log, 20)}")
            if update.get("state") == "available":
                raise Failure(f"it offered an update with nothing for Linux: {json.dumps(update)}")
            ask("go-back")
            try:
                new_app.wait(timeout=300)
            except subprocess.TimeoutExpired:
                raise Failure(f"it didn't quit to go back (it reports {json.dumps(recorder().get('update'))})")
            wait_installed(update_log, old_version, 60)
            got = app.version(env)
            if got != old_version:
                raise Failure(f"after going back, the AppImage is {got or 'unknown'}, not {old_version}")
            if not wait_gone(URL, 30):
                raise Failure("the new version's engine outlived it")

            log(f"{old_version} opens the meetings {new_version} left")
            app.install(app.appimage)
            engine = start_engine(app, env, engine_log)
            processes.append(engine)
            wait_for(URL, 240, "the engine")
            meetings_after = count_meetings()
            if meetings_after != meetings_before:
                raise Failure(f"{old_version} sees {meetings_after} meetings; {new_version} had {meetings_before}")
            print(f"went back {new_version} -> {got}, with all {meetings_after} meetings")
            kill_group(engine)
            if not wait_gone(URL, 30):
                raise Failure("the engine didn't stop")

        if app.kind != "appimage":
            log("Uninstalling")
            app.uninstall()
            installed = False
            if app.exe.exists():
                raise Failure(f"uninstalling left {app.exe} behind")
            print("uninstalled")
    except (Failure, smoke_test.Failed, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, StopIteration) as e:
        print(f"SMOKE TEST FAILED: {e}", file=sys.stderr)
        print("--- server log (last 60 lines)", file=sys.stderr)
        print(tail(engine_log), file=sys.stderr)
        print("--- the app's own engine log", file=sys.stderr)
        print(tail(app_data / "logs" / "server.log", 30), file=sys.stderr)
        print("--- the updating app's engine log", file=sys.stderr)
        print(tail(data / "logs" / "server.log", 30), file=sys.stderr)
        print("--- the update log", file=sys.stderr)
        print(tail(update_log, 40), file=sys.stderr)
        print("--- the app's output", file=sys.stderr)
        print(tail(app_log, 40), file=sys.stderr)
        return 1
    finally:
        for proc in processes:
            kill_group(proc)
        for pid in processes_named("Trailmix", [p.pid for p in processes]):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        desk.stop()
        if installed and app.kind != "appimage":
            try:
                app.uninstall()
            except Exception:
                pass
    done_with = f", updated from {old_version} and back again" if previous and app.kind == "appimage" else ""
    print(f"\nSmoke test passed ({new_version or package.name}{done_with}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
