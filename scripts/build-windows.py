#!/usr/bin/env python3
"""Builds the Windows installer: dist/Trailmix-<version>-windows-x64.exe (and its .sha256), the Windows
counterpart of scripts/build-app.sh. The installer holds the desktop shell (desktop/, a Tauri app), the
Trailmix server with its own Python, whisper.cpp and ffmpeg, and the web app, so nothing else needs installing.

  python scripts/build-windows.py                  build the installer for the version in VERSION
  set TRAILMIX_VERSION=0.19.0-build.7 ...          another version (semver: X.Y.Z, with an optional -suffix)
  python scripts/build-windows.py --stage-only     only stage build/windows (no Rust build, no installer)

Needs: Windows, Python 3.9+, Node.js, Rust (cargo) and internet access (downloads the Python runtime once
into build/cache, plus Python packages and Rust crates). The installer is unsigned for now.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = os.environ.get("TRAILMIX_VERSION") or (ROOT / "VERSION").read_text().strip()
OUT = Path(os.environ.get("TRAILMIX_OUT") or ROOT / "dist")
CACHE = ROOT / "build" / "cache"
STAGE = ROOT / "build" / "windows"  # what gets bundled next to Trailmix.exe (see desktop/src-tauri/tauri.conf.json)

# python-build-standalone (https://github.com/astral-sh/python-build-standalone): a relocatable CPython.
PY_VERSION = "3.14.7"
PY_RELEASE = "20260929"
PY_SHA256 = "6378385d5e42ec0874656e5efd707fb92334a6972b1aca90fd254eb924a8f404"
PY_FILE = f"cpython-{PY_VERSION}+{PY_RELEASE}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
PY_URL = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PY_RELEASE}/{PY_FILE.replace('+', '%2B')}"


def say(text: str) -> None:
    print(f"> {text}", flush=True)


def die(text: str) -> None:
    print(f"x {text}", file=sys.stderr)
    sys.exit(1)


def run(args, **kwargs) -> None:
    kwargs.setdefault("check", True)
    subprocess.run(args, **kwargs)


def tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        die(f"Building needs {name} on the PATH.")
    return found


def download_python() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / PY_FILE
    if not archive.exists():
        say(f"Downloading the Python runtime ({PY_VERSION}, once)…")
        part = archive.with_suffix(".part")
        with urllib.request.urlopen(PY_URL) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f)
        part.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != PY_SHA256:
        archive.unlink()
        die("The downloaded Python runtime failed its checksum; try again.")
    return archive


def extract(archive: Path, into: Path) -> None:
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(into, filter="data")
        else:
            tar.extractall(into)


def rmtree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


def trim(python: Path) -> None:
    """Drops what the app never uses from the Python runtime (as build-app.sh does)."""
    lib = python / "Lib"
    site = lib / "site-packages"
    for pattern in ("pip", "pip-*", "setuptools", "setuptools-*", "pkg_resources", "_distutils_hack", "distutils-precedence.pth"):
        for p in site.glob(pattern):
            rmtree(p)
    for name in ("test", "idlelib", "tkinter", "turtledemo", "ensurepip"):
        rmtree(lib / name)
    for name in ("tcl", "include", "libs", "Scripts"):
        rmtree(python / name)
    for p in list((python / "DLLs").glob("_tkinter*")) + list(python.glob("tcl*.dll")) + list(python.glob("tk*.dll")):
        rmtree(p)
    for p in list(site.rglob("tests")) + list(site.rglob("test")):
        if p.is_dir():
            rmtree(p)
    for p in list(python.rglob("__pycache__")):
        rmtree(p)


def stage() -> None:
    npm = tool("npm")
    say("Building the web app…")
    if not (ROOT / "frontend" / "node_modules").is_dir():
        run([npm, "ci", "--silent"], cwd=ROOT / "frontend")
    run([npm, "run", "-s", "build"], cwd=ROOT / "frontend", stdout=subprocess.DEVNULL)

    archive = download_python()
    rmtree(STAGE)
    STAGE.mkdir(parents=True)
    say("Unpacking the Python runtime…")
    extract(archive, STAGE)  # -> build/windows/python
    python = STAGE / "python"
    py = python / "python.exe"
    if not py.is_file():
        die(f"No python.exe in {python}")

    say("Installing Python packages…")
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
    run([str(py), "-m", "pip", "install", "-q", "--no-compile", "-r", str(ROOT / "backend" / "requirements-whispercpp.txt")], env=env)

    # ffmpeg: a static build from the imageio-ffmpeg wheel (includes the Opus encoder).
    with tempfile.TemporaryDirectory() as tmp:
        run([str(py), "-m", "pip", "install", "-q", "--no-compile", "--no-deps", "--target", tmp, "imageio-ffmpeg"], env=env)
        found = sorted(Path(tmp).glob("imageio_ffmpeg/binaries/ffmpeg-win-*.exe"))
        if not found:
            die("The imageio-ffmpeg wheel had no Windows ffmpeg in it")
        (STAGE / "bin").mkdir()
        shutil.copy2(found[-1], STAGE / "bin" / "ffmpeg.exe")

    say("Trimming…")
    trim(python)

    backend = STAGE / "backend"
    backend.mkdir()
    for p in (ROOT / "backend").glob("*.py"):
        shutil.copy2(p, backend / p.name)
    if (ROOT / "backend" / "assets").is_dir():
        shutil.copytree(ROOT / "backend" / "assets", backend / "assets")
    shutil.copytree(ROOT / "frontend" / "dist", STAGE / "ui")
    shutil.copy2(ROOT / "VERSION", STAGE / "VERSION")

    # Compile ahead of time, never re-checked against the source: the app folder isn't written to while
    # it runs, and starts faster.
    run([str(py), "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash", str(python / "Lib"), str(backend)],
        stdout=subprocess.DEVNULL)
    size = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file()) / 1e6
    say(f"Staged {STAGE} ({size:.0f} MB)")


def build_installer() -> None:
    npm = tool("npm")
    npx = tool("npx")
    tool("cargo")
    desktop = ROOT / "desktop"
    say("Building the desktop shell and the installer…")
    if not (desktop / "node_modules").is_dir():
        run([npm, "ci", "--silent"], cwd=desktop)
    override = ROOT / "build" / "windows-version.json"
    override.write_text(json.dumps({"version": VERSION}))
    run([npx, "tauri", "build", "--config", str(override)], cwd=desktop)
    bundles = sorted((desktop / "src-tauri" / "target" / "release" / "bundle" / "nsis").glob("*-setup.exe"))
    if not bundles:
        die("Tauri didn't produce an installer")
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"Trailmix-{VERSION}-windows-x64.exe"
    target = OUT / name
    shutil.copy2(bundles[-1], target)
    # The app's updater (to come) checks a download against this before installing it, as the Mac's does.
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (OUT / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    say(f"Built {target} ({target.stat().st_size / 1e6:.0f} MB)")


def main() -> None:
    if sys.platform != "win32":
        die("The Windows installer is built on Windows.")
    stage()
    if "--stage-only" not in sys.argv:
        build_installer()


if __name__ == "__main__":
    main()
