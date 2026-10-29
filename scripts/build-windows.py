#!/usr/bin/env python3
"""Builds the Windows installer: dist/Trailmix-<version>-windows-<arch>.exe (and its .sha256), the Windows
counterpart of scripts/build-app.sh. The installer holds the desktop shell (desktop/, a Tauri app), the
Trailmix server with its own Python, whisper.cpp and ffmpeg, and the web app, so nothing else needs installing.

  python scripts/build-windows.py                  build the installer for the version in VERSION, for x64 PCs
  python scripts/build-windows.py --arch arm64     for ARM PCs (Snapdragon and the like), built on one (or set TRAILMIX_ARCH)
  set TRAILMIX_VERSION=0.19.0-build.7 ...          another version (semver: X.Y.Z, with an optional -suffix)
  python scripts/build-windows.py --stage-only     only stage build/windows (no Rust build, no installer)

Needs: Windows, Python 3.9+, Node.js, Rust (cargo) and internet access (downloads the Python runtime once
into build/cache, plus Python packages and Rust crates). The ARM build also compiles whisper.cpp, which has
no ARM wheel: that needs Visual Studio's ARM64 C++ tools and its Clang tools (clang-cl). The installer is
unsigned for now.
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
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = os.environ.get("TRAILMIX_VERSION") or (ROOT / "VERSION").read_text().strip()
OUT = Path(os.environ.get("TRAILMIX_OUT") or ROOT / "dist")
CACHE = ROOT / "build" / "cache"
STAGE = ROOT / "build" / "windows"  # what gets bundled next to Trailmix.exe (see desktop/src-tauri/tauri.conf.json)


def _arch() -> str:
    args = sys.argv[1:]
    arch = args[args.index("--arch") + 1] if "--arch" in args[:-1] else os.environ.get("TRAILMIX_ARCH") or "x64"
    if arch not in ("x64", "arm64"):
        print(f"x Unknown arch {arch!r}: x64 or arm64", file=sys.stderr)
        sys.exit(2)
    return arch


ARCH = _arch()
RUST_TARGET = {"x64": "x86_64-pc-windows-msvc", "arm64": "aarch64-pc-windows-msvc"}[ARCH]

# python-build-standalone (https://github.com/astral-sh/python-build-standalone): a relocatable CPython.
# The checksums are from that release's SHA256SUMS.
PY_VERSION = "3.14.7"
PY_RELEASE = "20260929"
PY_SHA256 = {
    "x64": "6378385d5e42ec0874656e5efd707fb92334a6972b1aca90fd254eb924a8f404",
    "arm64": "f373eec4bb1d46ea7c7bfca83880c06f79417c419ffd34c8d2643d59c4fe4ba1",
}[ARCH]
PY_FILE = f"cpython-{PY_VERSION}+{PY_RELEASE}-{RUST_TARGET}-install_only_stripped.tar.gz"  # (same triples as Rust's)
PY_URL = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PY_RELEASE}/{PY_FILE.replace('+', '%2B')}"

# ffmpeg for ARM: imageio-ffmpeg (where the x64 one comes from) has no ARM build, so it's BtbN/FFmpeg-Builds'
# static LGPL build (the Windows builds ffmpeg.org links to; it has the Opus encoder), from a month-end build
# (those are kept). The checksum is from that release's checksums.sha256. Not September's: its ARM builds crash on
# start (https://github.com/BtbN/FFmpeg-Builds/issues/675, a broken strip, fixed in October's).
FF_RELEASE = "autobuild-2026-08-31-13-27"
FF_NAME = "ffmpeg-n8.1.2-50-g1a748fe2cd-winarm64-lgpl-8.1"
FF_SHA256 = "0ad4d6e7342d6d77bbae4ac230964ebd51bdd6192414392e5f521d295b39b111"
FF_URL = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{FF_RELEASE}/{FF_NAME}.zip"


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


def native_arm64() -> bool:
    """Whether this PC is an ARM one (also when this Python is an x64 one running emulated on it)."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    process, native = ctypes.c_ushort(), ctypes.c_ushort()
    if not kernel32.IsWow64Process2(kernel32.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
        return False
    return native.value == 0xAA64  # IMAGE_FILE_MACHINE_ARM64


def no_spaces(path: Path, tmp: Path) -> str:
    """path, as one that CMAKE_ARGS can carry (pywhispercpp splits it at spaces): a copy in tmp, by its short (8.3)
    name if even that has a space."""
    copy = tmp / path.name
    shutil.copy2(path, copy)
    if " " not in str(copy):
        return copy.as_posix()
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(copy), buf, len(buf))
    if not buf.value or " " in buf.value:
        die(f"Building whisper.cpp needs a folder without spaces in its path (or with short names): {copy}")
    return Path(buf.value).as_posix()


def build_env(vs: Path) -> dict:
    """The environment the from-source build gets. pywhispercpp's setup.py hands every environment variable to
    CMake as a -D flag, and its log prints them all: it gets what building needs, never whatever keys and tokens
    are in this shell (or a CI runner's). As build-app.sh does for the Intel Mac build."""
    keep = ("SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
            "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
            "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", "NUMBER_OF_PROCESSORS", "OS")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    system_root = env.get("SystemRoot", r"C:\Windows")
    # A short temporary folder: Visual Studio's build of whisper.cpp nests deep, inside pip's own build folders.
    tmp = ROOT / "build" / "t"
    rmtree(tmp)
    tmp.mkdir(parents=True)
    llvm = vs / "VC" / "Tools" / "Llvm" / "ARM64"
    env.update({
        # (Visual Studio's clang has OpenMP's DLL, which the wheel's repair step finds on the PATH and bundles)
        "PATH": f"{system_root}\\System32;{system_root};{system_root}\\System32\\Wbem;{llvm / 'bin'}",
        "TEMP": str(tmp), "TMP": str(tmp),
        "PROCESSOR_ARCHITECTURE": "ARM64",  # (what this is built on, even when run from an emulated x64 Python)
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
        # ggml can't be built for ARM with MSVC's own compiler, only with clang-cl: Visual Studio's ClangCL toolset
        # (CMake uses the newest Visual Studio, as vswhere -latest does).
        "CMAKE_GENERATOR_TOOLSET": "ClangCL",
        # For any ARM64 PC rather than tuned to this one. With OpenMP, as on x64: without it ggml's threads spin while
        # they wait for each other, and slow to a crawl when anything else wants the CPU (a video call, say).
        # CMake doesn't find clang-cl's OpenMP (LLVM's libomp, with Visual Studio's clang) by itself, so it's told.
        "CMAKE_ARGS": " ".join([
            "-DGGML_NATIVE=OFF", "-DGGML_OPENMP=ON",
            "-DOpenMP_C_FLAGS=/openmp", "-DOpenMP_CXX_FLAGS=/openmp",
            "-DOpenMP_C_LIB_NAMES=libomp", "-DOpenMP_CXX_LIB_NAMES=libomp",
            f"-DOpenMP_libomp_LIBRARY={no_spaces(llvm / 'lib' / 'libomp.lib', tmp)}",
        ]),
        "CMAKE_BUILD_PARALLEL_LEVEL": str(os.cpu_count() or 4),
    })
    return env


def clang_cl_toolset() -> Path | None:
    """The Visual Studio here with the ClangCL toolset for ARM64 (its "C++ Clang tools" components), if any."""
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return None
    found = subprocess.run([str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
                           capture_output=True, text=True).stdout.strip().splitlines()
    for vs in map(Path, found):
        # (under MSBuild\Microsoft\VC\v170 for Visual Studio 2022, v180 for 2026)
        toolsets = list((vs / "MSBuild" / "Microsoft" / "VC").glob("v*/Platforms/ARM64/PlatformToolsets"))
        if any((t / "ClangCL").is_dir() for t in toolsets):
            print(f"  OpenMP in {vs}: {[str(p.relative_to(vs)) for p in (vs / 'VC' / 'Tools' / 'Llvm').rglob('libomp*')]}", flush=True)
            return vs
        for t in toolsets:
            print(f"  {t}: {', '.join(sorted(p.name for p in t.iterdir()))}", file=sys.stderr)
    return None


def pe_machine(path: Path) -> int:
    with open(path, "rb") as f:
        head = f.read(4096)
    pe = int.from_bytes(head[0x3C:0x40], "little")
    return int.from_bytes(head[pe + 4:pe + 6], "little")


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


MACHINE = {"x64": 0x8664, "arm64": 0xAA64}[ARCH]  # the PE header's machine type for this arch


def install_packages(py: Path) -> None:
    reqs = str(ROOT / "backend" / "requirements-whispercpp.txt")
    if ARCH == "x64":
        env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
        run([str(py), "-m", "pip", "install", "-q", "--no-compile", "-r", reqs], env=env)
        return
    # Everything has an ARM wheel except whisper.cpp's (pywhispercpp), which is compiled here, and only that:
    # anything else without a wheel stops the build rather than being compiled unnoticed.
    vs = clang_cl_toolset()
    if not vs:
        die("Building whisper.cpp for ARM needs Visual Studio (2022 or later) with its ARM64 C++ build tools and its C++ Clang tools "
            "(the components Microsoft.VisualStudio.Component.VC.Tools.ARM64, ...VC.Llvm.Clang and ...VC.Llvm.ClangToolset).")
    say("Compiling whisper.cpp (pywhispercpp has no ARM wheel; a few minutes)…")
    run([str(py), "-m", "pip", "install", "-q", "--no-compile", "--no-warn-script-location", "--only-binary", ":all:", "--no-binary", "pywhispercpp",
         "--log", str(ROOT / "build" / "whisper-cpp-build.log"), "-r", reqs], env=build_env(vs))
    rmtree(ROOT / "build" / "t")
    with open(ROOT / "build" / "whisper-cpp-build.log", encoding="utf-8", errors="replace") as log:
        for line in log:
            if "OpenMP" in line and "-D" not in line:
                print("  " + line.strip()[-200:])


def check_packages(py: Path) -> None:
    """whisper.cpp loads in a bare environment, as the app gives the engine, finding its DLLs (installed next to
    it), and the binaries that matter are built for this arch."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    env = {"SystemRoot": system_root, "PATH": f"{system_root}\\System32;{system_root}"}
    code = "import _pywhispercpp, pywhispercpp.model; print(_pywhispercpp.__file__); print(_pywhispercpp.whisper_print_system_info())"
    done = subprocess.run([str(py), "-I", "-c", code], env=env, capture_output=True, text=True)
    if done.returncode != 0:
        die(f"whisper.cpp doesn't load:\n{done.stderr[-3000:]}")
    print(done.stdout.strip(), flush=True)
    site = py.parent / "Lib" / "site-packages"
    dlls = sorted(site.glob("*.dll"))  # whisper.cpp's, as its wheel's repair step bundled them
    print(f"whisper.cpp's DLLs: {', '.join(p.name for p in dlls)}", flush=True)
    if not any(p.name.startswith(("libomp", "vcomp")) for p in dlls):
        die("whisper.cpp was built without OpenMP (or its DLL wasn't bundled)")
    binaries = [py, *py.parent.glob("python3*.dll"), *site.glob("_pywhispercpp*.pyd"), *dlls]
    wrong = [p.name for p in binaries if pe_machine(p) != MACHINE]
    if wrong:
        die(f"Not built for {ARCH}: {', '.join(wrong)}")


def install_ffmpeg(py: Path) -> None:
    (STAGE / "bin").mkdir()
    ffmpeg = STAGE / "bin" / "ffmpeg.exe"
    if ARCH == "x64":
        # A static build from the imageio-ffmpeg wheel (includes the Opus encoder).
        env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
        with tempfile.TemporaryDirectory() as tmp:
            run([str(py), "-m", "pip", "install", "-q", "--no-compile", "--no-deps", "--target", tmp, "imageio-ffmpeg"], env=env)
            found = sorted(Path(tmp).glob("imageio_ffmpeg/binaries/ffmpeg-win-*.exe"))
            if not found:
                die("The imageio-ffmpeg wheel had no Windows ffmpeg in it")
            shutil.copy2(found[-1], ffmpeg)
    else:
        archive = download(FF_URL, FF_SHA256, "ffmpeg for ARM")
        with zipfile.ZipFile(archive) as z, z.open(f"{FF_NAME}/bin/ffmpeg.exe") as src, open(ffmpeg, "wb") as dst:
            shutil.copyfileobj(src, dst)
    if pe_machine(ffmpeg) != MACHINE:
        die(f"ffmpeg isn't built for {ARCH}")
    done = subprocess.run([str(ffmpeg), "-hide_banner", "-encoders"], capture_output=True, timeout=120)
    if b" libopus " not in done.stdout:
        print(f"ffmpeg -encoders: exit {done.returncode:#x}\n{done.stdout[-1500:].decode(errors='replace')}\n"
              f"{done.stderr[-1500:].decode(errors='replace')}", file=sys.stderr)
        die("ffmpeg has no Opus encoder (libopus), which recordings are saved with")


def stage() -> None:
    npm = tool("npm")
    if ARCH == "arm64" and not native_arm64():
        die("The ARM installer is built on an ARM PC (its Python and whisper.cpp run as part of the build).")
    say(f"Building for Windows on {ARCH}")
    say("Building the web app…")
    if not (ROOT / "frontend" / "node_modules").is_dir():
        run([npm, "ci", "--silent"], cwd=ROOT / "frontend")
    run([npm, "run", "-s", "build"], cwd=ROOT / "frontend", stdout=subprocess.DEVNULL)

    archive = download(PY_URL, PY_SHA256, f"the Python runtime ({PY_VERSION})")
    rmtree(STAGE)
    STAGE.mkdir(parents=True)
    say("Unpacking the Python runtime…")
    extract(archive, STAGE)  # -> build/windows/python
    python = STAGE / "python"
    py = python / "python.exe"
    if not py.is_file():
        die(f"No python.exe in {python}")

    say("Installing Python packages…")
    install_packages(py)
    check_packages(py)
    install_ffmpeg(py)

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
    if shutil.which("rustup"):
        run(["rustup", "target", "add", RUST_TARGET], stdout=subprocess.DEVNULL)
    run([npx, "tauri", "build", "--target", RUST_TARGET, "--config", str(override)], cwd=desktop)
    bundles = sorted((desktop / "src-tauri" / "target" / RUST_TARGET / "release" / "bundle" / "nsis").glob("*-setup.exe"))
    if not bundles:
        die("Tauri didn't produce an installer")
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"Trailmix-{VERSION}-windows-{ARCH}.exe"
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
