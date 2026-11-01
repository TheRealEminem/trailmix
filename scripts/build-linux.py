#!/usr/bin/env python3
"""Builds the Linux app: dist/Trailmix-<version>-linux-<arch>.AppImage, .deb and .rpm (each with its .sha256), the
Linux counterpart of scripts/build-windows.py. Each holds the desktop shell (desktop/, the same Tauri app as on
Windows), the Trailmix server with its own Python, whisper.cpp and ffmpeg, and the web app, so nothing else needs
installing but what every desktop has (GTK, WebKitGTK and the sound server's client library; the AppImage
brings GTK and WebKitGTK too).

  python3 scripts/build-linux.py                   build all three for the version in VERSION, for this computer
  python3 scripts/build-linux.py --arch arm64      for ARM computers, built on one (or set TRAILMIX_ARCH)
  TRAILMIX_VERSION=0.19.0-build.7 ...              another version (semver: X.Y.Z, with an optional -suffix)
  python3 scripts/build-linux.py --stage-only      only stage build/linux (no Rust build, no packages)
  python3 scripts/build-linux.py --bundles appimage   only some of appimage, deb, rpm (or set TRAILMIX_BUNDLES)
  python3 scripts/build-linux.py --packages-only   package what's staged in build/linux already (another version, say)

Needs: Linux, Python 3.9+, Node.js, Rust (cargo), and the libraries Tauri builds against (on Debian or Ubuntu:
libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libpulse-dev, plus file and xdg-utils), and
internet access (downloads the Python runtime once into build/cache, Python packages, Rust crates, and Tauri's
AppImage tools). Data goes to $XDG_DATA_HOME/Trailmix (~/.local/share/Trailmix) when the app runs.

What it makes runs on Linux with this computer's glibc or newer (the shell is linked against it), so releases are
built on Ubuntu 22.04 (glibc 2.35: Ubuntu 22.04, Debian 12 and later). With TRAILMIX_MAX_GLIBC=2.35 the build stops
if anything in the app needs a newer one.
"""
import hashlib
import json
import os
import platform
import re
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
STAGE = ROOT / "build" / "linux"  # what gets bundled next to the app (see desktop/src-tauri/tauri.linux.conf.json)


def _arg(flag: str):
    args = sys.argv[1:]
    return args[args.index(flag) + 1] if flag in args[:-1] else None


def _arch() -> str:
    native = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "x86_64")
    arch = _arg("--arch") or os.environ.get("TRAILMIX_ARCH") or native
    if arch not in ("x86_64", "arm64"):
        print(f"x Unknown arch {arch!r}: x86_64 or arm64", file=sys.stderr)
        sys.exit(2)
    return arch


ARCH = _arch()
RUST_TARGET = {"x86_64": "x86_64-unknown-linux-gnu", "arm64": "aarch64-unknown-linux-gnu"}[ARCH]
BUNDLES = (_arg("--bundles") or os.environ.get("TRAILMIX_BUNDLES") or "appimage,deb,rpm").split(",")

# python-build-standalone (https://github.com/astral-sh/python-build-standalone): a relocatable CPython, built for
# glibc 2.17 and later. The checksums are from that release's SHA256SUMS.
PY_VERSION = "3.14.7"
PY_RELEASE = "20260929"
PY_SHA256 = {
    "x86_64": "da5357f47b1d9c8d4c439004d463d893ef688a44c27438a62b47073d26d1377e",
    "arm64": "9b8bd45d06fc3fcdc56f2713a62535fe1c019816461ae87c170dd56184dd3f4d",
}[ARCH]
PY_FILE = f"cpython-{PY_VERSION}+{PY_RELEASE}-{RUST_TARGET}-install_only_stripped.tar.gz"  # (same triples as Rust's)
PY_URL = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PY_RELEASE}/{PY_FILE.replace('+', '%2B')}"

# The ELF header's machine type for this arch.
MACHINE = {"x86_64": 62, "arm64": 183}[ARCH]
PIP_ENV = {"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}


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


def download(url: str, sha256: str, what: str) -> Path:
    """Downloads url into build/cache once, and checks it against its pinned checksum every time."""
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / url.rsplit("/", 1)[1].replace("%2B", "+")
    if not archive.exists():
        say(f"Downloading {what} (once)…")
        part = archive.with_suffix(".part")
        with urllib.request.urlopen(url) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f)
        part.replace(archive)
    h = hashlib.sha256()
    with open(archive, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest() != sha256:
        archive.unlink()
        die(f"The downloaded {what} failed its checksum; try again.")
    return archive


def elf_machine(path: Path) -> int:
    with open(path, "rb") as f:
        head = f.read(20)
    if head[:4] != b"\x7fELF":
        return -1
    return int.from_bytes(head[18:20], "little")


def extract(archive: Path, into: Path) -> None:
    with tarfile.open(archive) as tar:
        # (Before 3.12 the "data" filter takes the runtime's hard links, terminfo's, as pointing outside it, as on
        # Ubuntu 22.04's Python 3.10. The archive is checked against its pinned checksum first, so it's trusted.)
        if hasattr(tarfile, "data_filter") and sys.version_info >= (3, 12):
            tar.extractall(into, filter="data")
        elif hasattr(tarfile, "data_filter"):
            tar.extractall(into, filter="fully_trusted")
        else:
            tar.extractall(into)


def rmtree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink()


def no_symlinks(folder: Path) -> None:
    """Replaces every symlink with a copy of what it points at: the packages copy files, not links (python3 is a
    link to python3.14 in the runtime)."""
    for p in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not p.is_symlink():
            continue
        target = p.resolve()
        p.unlink()
        if target.is_dir():
            shutil.copytree(target, p, symlinks=False)
        elif target.is_file():
            shutil.copy2(target, p)


def trim(python: Path) -> None:
    """Drops what the app never uses from the Python runtime (as build-app.sh does)."""
    lib = next((python / "lib").glob("python3.*"))
    site = lib / "site-packages"
    for pattern in ("pip", "pip-*", "setuptools", "setuptools-*", "pkg_resources", "_distutils_hack", "distutils-precedence.pth"):
        for p in site.glob(pattern):
            rmtree(p)
    for name in ("test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3"):
        rmtree(lib / name)
    for p in lib.glob("config-3.*"):
        rmtree(p)
    for p in list((lib / "lib-dynload").glob("_tkinter*")) + list((python / "lib").glob("libtcl*")) + list((python / "lib").glob("libtk*")):
        rmtree(p)
    for name in ("include", "share"):
        rmtree(python / name)
    for p in (python / "lib").glob("tcl*"):
        rmtree(p)
    for p in (python / "lib").glob("tk*"):
        rmtree(p)
    # Only python3 itself (a link to python3.14, made a copy of it), not pip's, idle's or python3-config, which are
    # scripts pointing at this build machine.
    bin_dir = python / "bin"
    real = (bin_dir / "python3").resolve()
    keep = bin_dir / ".python3"
    shutil.copy2(real, keep)
    for p in list(bin_dir.iterdir()):
        if p != keep:
            rmtree(p)
    keep.rename(bin_dir / "python3")
    for p in list(site.rglob("tests")) + list(site.rglob("test")):
        if p.is_dir():
            rmtree(p)
    for p in list(python.rglob("__pycache__")):
        rmtree(p)


def install_packages(py: Path) -> None:
    reqs = str(ROOT / "backend" / "requirements-whispercpp.txt")
    # Wheels only (pywhispercpp has manylinux ones for x86_64 and aarch64): anything without one stops the build
    # rather than being compiled here unnoticed.
    run([str(py), "-m", "pip", "install", "-q", "--no-compile", "--no-warn-script-location", "--only-binary", ":all:", "-r", reqs],
        env={**os.environ, **PIP_ENV})


def check_packages(py: Path) -> None:
    """whisper.cpp loads in a bare environment, as the app gives the engine, and the binaries that matter are
    built for this arch."""
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
    code = "import _pywhispercpp, pywhispercpp.model; print(_pywhispercpp.__file__); print(_pywhispercpp.whisper_print_system_info())"
    done = subprocess.run([str(py), "-I", "-c", code], env=env, capture_output=True, text=True)
    if done.returncode != 0:
        die(f"whisper.cpp doesn't load:\n{done.stderr[-3000:]}")
    print(done.stdout.strip(), flush=True)
    site = next((py.parent.parent / "lib").glob("python3.*")) / "site-packages"
    libs = sorted(p.name for p in site.glob("pywhispercpp.libs/*")) + sorted(p.name for p in site.glob("*.so*"))
    print(f"whisper.cpp's libraries: {', '.join(libs) or '(none bundled)'}", flush=True)
    if not any("gomp" in name or "omp" in name for name in libs):
        print("! whisper.cpp may be built without OpenMP here (its threads then spin while they wait)", flush=True)
    binaries = [py, *site.glob("_pywhispercpp*.so"), *site.glob("pywhispercpp.libs/*.so*")]
    wrong = [p.name for p in binaries if elf_machine(p) != MACHINE]
    if wrong:
        die(f"Not built for {ARCH}: {', '.join(wrong)}")


def install_ffmpeg(py: Path) -> None:
    """A static ffmpeg from the imageio-ffmpeg wheel for this arch (it has the Opus encoder)."""
    (STAGE / "bin").mkdir()
    ffmpeg = STAGE / "bin" / "ffmpeg"
    platform_tag = {"x86_64": "manylinux2014_x86_64", "arm64": "manylinux2014_aarch64"}[ARCH]
    with tempfile.TemporaryDirectory() as tmp:
        run([str(py), "-m", "pip", "install", "-q", "--no-compile", "--no-deps", "--only-binary", ":all:", "--platform", platform_tag,
             "--target", tmp, "imageio-ffmpeg"], env={**os.environ, **PIP_ENV})
        found = sorted(Path(tmp).glob("imageio_ffmpeg/binaries/ffmpeg-linux*"))
        if not found:
            die("The imageio-ffmpeg wheel had no Linux ffmpeg in it")
        shutil.copy2(found[-1], ffmpeg)
    ffmpeg.chmod(0o755)
    if elf_machine(ffmpeg) != MACHINE:
        die(f"ffmpeg isn't built for {ARCH}")
    done = subprocess.run([str(ffmpeg), "-hide_banner", "-encoders"], capture_output=True, timeout=120)
    if b" libopus " not in done.stdout:
        print(f"ffmpeg -encoders: exit {done.returncode}\n{done.stdout[-1500:].decode(errors='replace')}\n"
              f"{done.stderr[-1500:].decode(errors='replace')}", file=sys.stderr)
        die("ffmpeg has no Opus encoder (libopus), which recordings are saved with")


def stage() -> None:
    npm = tool("npm")
    native = {"x86_64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower())
    if native != ARCH:
        die(f"The {ARCH} app is built on a {ARCH} computer (its Python and whisper.cpp run as part of the build).")
    say(f"Building for Linux on {ARCH}")
    say("Building the web app…")
    if not (ROOT / "frontend" / "node_modules" / ".package-lock.json").is_file():
        run([npm, "ci", "--silent"], cwd=ROOT / "frontend")
    run([npm, "run", "-s", "build"], cwd=ROOT / "frontend", stdout=subprocess.DEVNULL)

    archive = download(PY_URL, PY_SHA256, f"the Python runtime ({PY_VERSION})")
    rmtree(STAGE)
    STAGE.mkdir(parents=True)
    say("Unpacking the Python runtime…")
    extract(archive, STAGE)  # -> build/linux/python
    python = STAGE / "python"
    py = python / "bin" / "python3"
    if not py.exists():
        die(f"No bin/python3 in {python}")

    say("Installing Python packages…")
    install_packages(py)
    check_packages(py)
    install_ffmpeg(py)

    say("Trimming…")
    trim(python)
    no_symlinks(python)

    backend = STAGE / "backend"
    backend.mkdir()
    for p in (ROOT / "backend").glob("*.py"):
        shutil.copy2(p, backend / p.name)
    if (ROOT / "backend" / "assets").is_dir():
        shutil.copytree(ROOT / "backend" / "assets", backend / "assets")
    shutil.copytree(ROOT / "frontend" / "dist", STAGE / "ui")
    shutil.copy2(ROOT / "VERSION", STAGE / "VERSION")

    # Compile ahead of time, never re-checked against the source: the app folder is read-only (inside the
    # AppImage, or /usr/lib), and it starts faster.
    run([str(py), "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash", str(python / "lib"), str(backend)],
        stdout=subprocess.DEVNULL)
    size = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file()) / 1e6
    say(f"Staged {STAGE} ({size:.0f} MB)")


def sha256_file(path: Path) -> None:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    (path.parent / f"{path.name}.sha256").write_text(f"{h.hexdigest()}  {path.name}\n")


def build_packages() -> None:
    npm = tool("npm")
    npx = tool("npx")
    tool("cargo")
    desktop = ROOT / "desktop"
    say(f"Building the desktop shell and the packages ({', '.join(BUNDLES)})…")
    if not (desktop / "node_modules" / ".package-lock.json").is_file():
        run([npm, "ci", "--silent"], cwd=desktop)
    override = ROOT / "build" / "linux-version.json"
    override.write_text(json.dumps({"version": VERSION}))
    if shutil.which("rustup"):
        run(["rustup", "target", "add", RUST_TARGET], stdout=subprocess.DEVNULL)
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR") or desktop / "src-tauri" / "target")
    bundle_dir = target_dir / RUST_TARGET / "release" / "bundle"
    for kind in ("appimage", "deb", "rpm"):
        rmtree(bundle_dir / kind)
    env = {
        **os.environ,
        # linuxdeploy's own strip is too old for what current toolchains make, and the app is stripped already.
        "NO_STRIP": "1",
        # Tauri's AppImage tools are AppImages themselves: run them without FUSE (build machines often have none).
        "APPIMAGE_EXTRACT_AND_RUN": "1",
    }
    # An RPM version can't have a "-": a pre-release (0.19.5-build.7) goes in as version 0.19.5, release 0.build.7
    # (which sorts before 0.19.5's own release, 1). That package is bundled from the same build afterwards.
    base, _, pre = VERSION.partition("-")
    later = ["rpm"] if pre and "rpm" in BUNDLES else []
    first = [b for b in BUNDLES if b not in later]
    if first:
        run([npx, "tauri", "build", "--target", RUST_TARGET, "--config", str(override), "--bundles", ",".join(first)], cwd=desktop, env=env)
    if later:
        rpm_override = ROOT / "build" / "linux-rpm-version.json"
        rpm_override.write_text(json.dumps({"version": base, "bundle": {"linux": {"rpm": {"release": f"0.{pre.replace('-', '.')}"}}}}))
        if not first:
            run([npx, "tauri", "build", "--target", RUST_TARGET, "--config", str(override), "--no-bundle"], cwd=desktop, env=env)
        run([npx, "tauri", "bundle", "--target", RUST_TARGET, "--config", str(rpm_override), "--bundles", "rpm"], cwd=desktop, env=env)
    OUT.mkdir(parents=True, exist_ok=True)
    made = {"appimage": sorted(bundle_dir.glob("appimage/*.AppImage")), "deb": sorted(bundle_dir.glob("deb/*.deb")),
            "rpm": sorted(bundle_dir.glob("rpm/*.rpm"))}
    for kind in BUNDLES:
        if not made.get(kind):
            die(f"Tauri didn't make the {kind} package")
        ext = {"appimage": "AppImage", "deb": "deb", "rpm": "rpm"}[kind]
        target = OUT / f"Trailmix-{VERSION}-linux-{ARCH}.{ext}"
        shutil.copy2(made[kind][-1], target)
        if kind == "appimage":
            target.chmod(0o755)
        # The AppImage's updater checks a download against this before it puts it in place, as the Mac's does.
        sha256_file(target)
        say(f"Built {target} ({target.stat().st_size / 1e6:.0f} MB)")


# MARK: Which Linux versions it runs on

# The newest glibc anything in the app may need, when set (release.yml sets it): the shell is linked against the
# glibc of the computer it's built on, so it's built on an older Linux (Ubuntu 22.04: glibc 2.35) to run on
# Ubuntu 22.04, Debian 12 and later. Building on something newer by mistake then stops the build.
MAX_GLIBC = os.environ.get("TRAILMIX_MAX_GLIBC", "")


def _glibc_version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def glibc_needed(path: Path) -> tuple:
    """The newest GLIBC_x.y symbol version an ELF file asks for ((0,) for none), from readelf's version needs."""
    done = subprocess.run(["readelf", "--version-info", "--wide", str(path)], capture_output=True, text=True)
    found = re.findall(r"Name: GLIBC_([0-9][0-9.]*)", done.stdout)
    return max((_glibc_version(v) for v in found), default=(0,))


def check_glibc() -> None:
    """Says the oldest glibc the app runs with (the newest version anything in it needs), and stops the build if
    that's newer than TRAILMIX_MAX_GLIBC. Looks at the shell and every program and library staged with it."""
    if not shutil.which("readelf"):
        print("! No readelf here (binutils), so which glibc the app needs isn't checked", flush=True)
        return
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR") or ROOT / "desktop" / "src-tauri" / "target")
    shell = target_dir / RUST_TARGET / "release" / "Trailmix"
    files = [shell] if shell.is_file() else []
    files += [p for p in STAGE.rglob("*") if p.is_file() and not p.is_symlink() and elf_machine(p) == MACHINE]
    if not files:
        return
    needs = {p: glibc_needed(p) for p in files}
    newest = max(needs, key=needs.get)
    version = ".".join(map(str, needs[newest]))
    say(f"The app needs glibc {version} or later (for {newest.relative_to(ROOT) if newest.is_relative_to(ROOT) else newest})")
    if MAX_GLIBC and needs[newest] > _glibc_version(MAX_GLIBC):
        over = sorted(f"{p.name} ({'.'.join(map(str, v))})" for p, v in needs.items() if v > _glibc_version(MAX_GLIBC))
        die(f"Built to need glibc {version}, newer than {MAX_GLIBC} (TRAILMIX_MAX_GLIBC): {', '.join(over[:10])}. "
            "Build on an older Linux (release.yml uses Ubuntu 22.04).")


def main() -> None:
    if not sys.platform.startswith("linux"):
        die("The Linux app is built on Linux.")
    if "--packages-only" in sys.argv and (STAGE / "python").is_dir():
        say(f"Using what's staged in {STAGE} already")
    else:
        stage()
    if "--stage-only" not in sys.argv:
        build_packages()
        check_glibc()


if __name__ == "__main__":
    main()
