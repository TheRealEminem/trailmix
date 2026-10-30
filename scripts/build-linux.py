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
  TRAILMIX_GPU_WHISPER=1 python3 scripts/build-linux.py   whisper.cpp on the GPU (Vulkan), on the CPU without one

Needs: Linux, Python 3.9+, Node.js, Rust (cargo), and the libraries Tauri builds against (on Debian or Ubuntu:
libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libpulse-dev, plus file and xdg-utils), and
internet access (downloads the Python runtime once into build/cache, Python packages, Rust crates, and Tauri's
AppImage tools). Data goes to $XDG_DATA_HOME/Trailmix (~/.local/share/Trailmix) when the app runs. The GPU build
compiles whisper.cpp (see whisper_cpp_vulkan), which also needs a C++ compiler and Vulkan's headers and shader
compiler (on Debian or Ubuntu: g++ libvulkan-dev glslc spirv-headers), and keeps what it compiled in build/cache.
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
        if hasattr(tarfile, "data_filter"):
            tar.extractall(into, filter="data")
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
    if GPU_WHISPER:
        install_whisper_cpp_vulkan(py, reqs)
        return
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
    binaries = [py, *site.glob("_pywhispercpp*.so"), *site.glob("pywhispercpp.libs/*.so*"), *site.glob("lib*.so*")]
    wrong = [p.name for p in binaries if elf_machine(p) != MACHINE]
    if wrong:
        die(f"Not built for {ARCH}: {', '.join(wrong)}")
    if GPU_WHISPER:
        check_whisper_cpp_vulkan(py, site, env)


# MARK: whisper.cpp on the GPU (Vulkan), with TRAILMIX_GPU_WHISPER=1
# (release.yml's GPU_WHISPER switch. Off, whisper.cpp is pywhispercpp's wheel, which runs on the CPU only.)
#
# Compiled from pywhispercpp's source (the checksum is PyPI's; the same as build-windows.py's) with ggml's backends
# as libraries of their own: the CPU one, for x86-64 in a variant for each generation of CPUs (SSE4.2 to AVX-512), of
# which the best for the computer is loaded, and Vulkan for the GPU (NVIDIA, AMD and Intel alike, through Mesa's
# drivers or NVIDIA's own), which loads only where there's a Vulkan loader (libvulkan.so.1), and is used only where
# that finds a GPU. Otherwise whisper.cpp runs on the CPU. whispercpp_engine.py loads them. (ARM gets one CPU
# backend for any ARM64 computer, as pywhispercpp's wheel has: ggml's ARM variants go up to ARMv9.2 with SME, which
# Ubuntu's compilers can't build yet.)

GPU_WHISPER = os.environ.get("TRAILMIX_GPU_WHISPER") == "1" or "--gpu-whisper" in sys.argv
PWCPP_VERSION = "1.5.1"  # (whisper.cpp 1.8.5)
PWCPP_SHA256 = "5df897e5dd9d9f16804fc937bd85b9f5880a0b29a55f8843585e0e03d76195e5"
PWCPP_URL = ("https://files.pythonhosted.org/packages/9f/35/610787a277e177e6b7983277a35a2969c0876b46c0d889bc6a0c4b4ac4a7/"
             f"pywhispercpp-{PWCPP_VERSION}.tar.gz")
# For any computer of this arch rather than tuned to this one, with OpenMP (without it ggml's threads spin while they
# wait for each other), each library finding the others next to it ($ORIGIN), and under one name each (no
# libggml.so.0 -> libggml.so.0.9.5 links, which a wheel turns into copies: loaded twice, ggml would keep two lists
# of backends).
WHISPER_CMAKE_ARGS = ["-DGGML_NATIVE=OFF", "-DGGML_OPENMP=ON", "-DGGML_VULKAN=ON", "-DGGML_BACKEND_DL=ON",
                      f"-DGGML_CPU_ALL_VARIANTS={'ON' if ARCH == 'x86_64' else 'OFF'}",
                      "-DCMAKE_INSTALL_RPATH=$ORIGIN", "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON", "-DCMAKE_PLATFORM_NO_VERSIONED_SONAME=ON"]
# What the compiled libraries may link against besides each other: glibc and the C++ runtime (on every desktop),
# and, for Vulkan's backend only, the Vulkan loader (from the graphics drivers: without it, that backend doesn't
# load). OpenMP's runtime (libgomp) isn't on every computer, so it's bundled, as pywhispercpp's wheel does.
SYSTEM_LIBS = ("libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "ld-linux", "libstdc++.so.6", "libgcc_s.so.1")
BUNDLED_LIBS = ("libgomp.so.1",)


# What whisper.cpp's Vulkan backend is compiled with: Khronos' headers and Google's shader compiler (glslc), the
# versions in LunarG's Vulkan SDK 1.4.363.0 (the one build-windows.py uses), from GitHub's archives of their tags
# (checksums of those), compiled once into build/cache. Not the system's: Ubuntu 22.04 has no glslc, and older
# headers than whisper.cpp wants. From the system it needs only a C++ compiler and Vulkan's loader (libvulkan1).
VULKAN_TAG = "vulkan-sdk-1.4.363.0"
VULKAN_SOURCES = {  # name: (url, sha256)
    "Vulkan-Headers": (f"https://github.com/KhronosGroup/Vulkan-Headers/archive/refs/tags/{VULKAN_TAG}.tar.gz",
                       "4a078be12bef21cfebc09d878b77a63cff9d68f899254a0b00d0e37ef73e7f7e"),
    "SPIRV-Headers": (f"https://github.com/KhronosGroup/SPIRV-Headers/archive/refs/tags/{VULKAN_TAG}.tar.gz",
                      "5b3a0d099f5c7649e29c0938476c462e71d66ad89a0bde42fa1ba3eb476956c9"),
    "SPIRV-Tools": (f"https://github.com/KhronosGroup/SPIRV-Tools/archive/refs/tags/{VULKAN_TAG}.tar.gz",
                    "e6c83a215538fbfd265ccf398d3877985055df1273dbed73912bd05307664688"),
    "glslang": (f"https://github.com/KhronosGroup/glslang/archive/refs/tags/{VULKAN_TAG}.tar.gz",
                "cda24ec765e6d2845d106b7caa593f34127195a88524cf87821ff0088fb6edec"),
    "shaderc": ("https://github.com/google/shaderc/archive/refs/tags/v2026.4.tar.gz",  # (released with that SDK)
                "f06ce5bcca94e5df7f34e115743597d0ad2e13c5fe9213c67dc8c76031241947"),
}
BUILD_TOOLS = ("cmake>=3.22,<4", "ninja")  # (from PyPI, for compiling those: Ubuntu 22.04's CMake is just old enough)


def vulkan_build_env(extra_cmake_args: list[str]) -> dict:
    """The environment the from-source build gets. pywhispercpp's setup.py hands every environment variable to
    CMake as a -D flag, and its log prints them all: it gets what building needs, never whatever keys and tokens
    are in this shell (or a CI runner's). As build-windows.py and build-app.sh do."""
    tmp = ROOT / "build" / "t"
    rmtree(tmp)
    tmp.mkdir(parents=True)
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8", "TMPDIR": str(tmp),
        **PIP_ENV,
        "SETUPTOOLS_SCM_PRETEND_VERSION": PWCPP_VERSION,
        "CMAKE_ARGS": " ".join([*WHISPER_CMAKE_ARGS, *extra_cmake_args]),
        "CMAKE_BUILD_PARALLEL_LEVEL": str(build_jobs()),
    }


def fetch_source(name: str, into: Path) -> Path:
    """One of VULKAN_SOURCES, downloaded into build/cache once (checked against its checksum every time) and
    unpacked into `into`; its folder there."""
    url, sha256 = VULKAN_SOURCES[name]
    archive = CACHE / f"{name}-{url.rsplit('/', 1)[1]}"
    if not archive.exists():
        say(f"Downloading {name}'s source (once)…")
        part = archive.with_suffix(".part")
        with urllib.request.urlopen(url) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f)
        part.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != sha256:
        archive.unlink()
        die(f"The downloaded {name} source failed its checksum; try again.")
    before = set(into.iterdir()) if into.is_dir() else set()
    into.mkdir(parents=True, exist_ok=True)
    extract(archive, into)
    (folder,) = set(into.iterdir()) - before
    return folder


def vulkan_tools(py: Path) -> list[str]:
    """Vulkan's headers and glslc for compiling whisper.cpp (see VULKAN_SOURCES), made once into build/cache, as
    CMake arguments for it."""
    tools = CACHE / f"vulkan-tools-{VULKAN_TAG}-{ARCH}"
    prefix, glslc = tools / "prefix", tools / "prefix" / "bin" / "glslc"
    loader = system_lib("libvulkan.so.1")  # (from the graphics drivers on the computers it runs on; only linked here)
    args = [f"-DVulkan_INCLUDE_DIR={prefix / 'include'}", f"-DVulkan_LIBRARY={loader}", f"-DVulkan_GLSLC_EXECUTABLE={glslc}",
            f"-DCMAKE_PREFIX_PATH={prefix}"]
    if any(" " in a for a in args):
        die(f"Building whisper.cpp with Vulkan needs a folder without spaces in its path: {ROOT}")
    if (tools / "done").is_file():
        say(f"Vulkan's headers and glslc ({VULKAN_TAG}): made already (build/cache)")
        return args
    say(f"Making Vulkan's headers and glslc ({VULKAN_TAG}; a few minutes, once)…")
    rmtree(tools)
    venv = tools / "venv"
    run([str(py), "-m", "venv", str(venv)])
    run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q", *BUILD_TOOLS], env={**os.environ, **PIP_ENV})
    env = {"PATH": f"{venv / 'bin'}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8"}
    cmake = [str(venv / "bin" / "cmake")]
    src = tools / "src"
    for name in ("Vulkan-Headers", "SPIRV-Headers"):  # (headers only: just installed, with their CMake files)
        folder = fetch_source(name, src)
        run([*cmake, "-S", str(folder), "-B", str(folder / "b"), "-G", "Ninja", f"-DCMAKE_INSTALL_PREFIX={prefix}",
             "--no-warn-unused-cli"],
            env=env, stdout=subprocess.DEVNULL)
        run([*cmake, "--install", str(folder / "b")], env=env, stdout=subprocess.DEVNULL)
    shaderc = fetch_source("shaderc", src)
    for name, into in (("glslang", "glslang"), ("SPIRV-Tools", "spirv-tools"), ("SPIRV-Headers", "spirv-headers")):
        fetch_source(name, src / "deps").rename(shaderc / "third_party" / into)
    build = shaderc / "b"
    run([*cmake, "-S", str(shaderc), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", f"-DPython3_EXECUTABLE={py}",
         "-DSHADERC_SKIP_TESTS=ON", "-DSHADERC_SKIP_EXAMPLES=ON", "-DSHADERC_SKIP_COPYRIGHT_CHECK=ON",
         "-DSHADERC_ENABLE_WERROR_COMPILE=OFF", "-DSPIRV_SKIP_TESTS=ON", "-DSPIRV_SKIP_EXECUTABLES=ON", "-DSPIRV_WERROR=OFF",
         "-DENABLE_GLSLANG_BINARIES=OFF", "-DGLSLANG_TESTS=OFF"], env=env, stdout=subprocess.DEVNULL)
    run([*cmake, "--build", str(build), "--target", "glslc_exe", "-j", str(build_jobs() * 2)], env=env, stdout=subprocess.DEVNULL)
    glslc.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build / "glslc" / "glslc", glslc)
    version = subprocess.run([str(glslc), "--version"], capture_output=True, text=True).stdout.splitlines()
    print(f"  {version[0] if version else 'glslc'}", flush=True)
    rmtree(src)
    rmtree(venv)
    (tools / "done").write_text(f"{VULKAN_TAG}\n")
    return args


def build_jobs() -> int:
    """How many compilers at once: one per core, as memory allows (Vulkan's shaders compile into C++ files that
    take a couple of GB each)."""
    try:
        with open("/proc/meminfo") as f:
            gb = next(int(line.split()[1]) for line in f if line.startswith("MemTotal:")) / 1e6
    except (OSError, StopIteration, ValueError):
        gb = 8
    return max(1, min(os.cpu_count() or 4, int(gb // 3)))


def elf_dynamic(path: Path) -> dict:
    """A (64-bit, little-endian) ELF file's dynamic section: {"needed": [...], "soname": ..., "runpath": ...}."""
    data = path.read_bytes()

    def u(at: int, size: int) -> int:
        return int.from_bytes(data[at:at + size], "little")

    shoff, shentsize, shnum = u(0x28, 8), u(0x3A, 2), u(0x3C, 2)
    sections = [shoff + i * shentsize for i in range(shnum)]
    found: dict = {"needed": [], "soname": None, "runpath": None}
    for s in sections:
        if u(s + 4, 4) != 6:  # SHT_DYNAMIC
            continue
        strtab = u(sections[u(s + 40, 4)] + 24, 8)  # its sh_link: .dynstr, at that offset

        def text(off: int) -> str:
            start = strtab + off
            return data[start:data.index(b"\0", start)].decode("utf-8", "replace")

        at = u(s + 24, 8)
        end = at + u(s + 32, 8)
        while at < end:
            tag, val = u(at, 8), u(at + 8, 8)
            if tag == 0:
                break
            if tag == 1:
                found["needed"].append(text(val))
            elif tag == 14:
                found["soname"] = text(val)
            elif tag in (15, 29):  # DT_RPATH, DT_RUNPATH
                found["runpath"] = text(val)
            at += 16
    return found


def system_lib(name: str) -> Path:
    """This computer's own copy of a library, as the dynamic linker finds it, to bundle it."""
    out = subprocess.run(["/sbin/ldconfig", "-p"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.strip().startswith(name + " ") and "=>" in line:
            p = Path(line.split("=>", 1)[1].strip())
            if p.is_file() and elf_machine(p) == MACHINE:
                return p
    die(f"No {name} here to bundle with whisper.cpp")


def whisper_cpp_vulkan(py: Path) -> Path:
    """Compiles whisper.cpp with Vulkan (see PWCPP_URL), once: a folder in build/cache with pywhispercpp's wheel and
    whisper.cpp's libraries (it takes a while: Vulkan's shaders, and the CPU backend once per variant)."""
    for name in ("cc", "c++"):
        tool(name)
    key = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]  # (this recipe)
    built = CACHE / f"whisper-cpp-{PWCPP_VERSION}-vulkan-{ARCH}-{key}"
    if (built / "done").is_file():
        say("whisper.cpp with Vulkan: compiled already (build/cache)")
        return built
    vulkan = vulkan_tools(py)
    archive = download(PWCPP_URL, PWCPP_SHA256, f"pywhispercpp {PWCPP_VERSION}'s source")
    work = ROOT / "build" / "w"
    rmtree(work)
    work.mkdir(parents=True)
    extract(archive, work)
    src = work / f"pywhispercpp-{PWCPP_VERSION}"
    say("Compiling whisper.cpp with Vulkan (pywhispercpp's wheel has no GPU support; 10 minutes or so)…")
    rmtree(built)
    (built / "libs").mkdir(parents=True)
    log = ROOT / "build" / "whisper-cpp-build.log"
    done = subprocess.run([str(py), "-m", "pip", "wheel", "-q", "--no-deps", "--log", str(log), "-w", str(built), str(src)], env=vulkan_build_env(vulkan))
    if done.returncode != 0:  # (the log has only what vulkan_build_env passes on)
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []
        errors = [line for line in lines if "error" in line.lower() or "FAILED" in line or "CMake Error" in line]
        print("\n".join(line[-300:] for line in [*errors[:60], "...", *lines[-40:]]), file=sys.stderr)
        die(f"Compiling whisper.cpp with Vulkan failed (all of it in {log})")
    with open(log, encoding="utf-8", errors="replace") as f:  # what CMake made of it
        found = [line.strip()[-200:] for line in f if "-- " in line and "-D" not in line
                 and any(word in line for word in ("Vulkan", "GL_", "CPU backend variant", "OpenMP", "SPIRV"))]
    print("\n".join(f"  {line}" for line in found[:60]), flush=True)
    libs: dict[str, Path] = {}
    for p in sorted(src.rglob("lib*.so*")):
        if p.is_file() and not p.is_symlink() and elf_machine(p) == MACHINE:
            libs.setdefault(elf_dynamic(p)["soname"] or p.name, p)
    for name in ("libwhisper.so", "libggml.so", "libggml-base.so", "libggml-vulkan.so"):
        if name not in libs:
            die(f"Compiling whisper.cpp made no {name} (it made {', '.join(sorted(libs)) or 'no libraries'})")
    if not any(n.startswith("libggml-cpu") for n in libs):
        die(f"Compiling whisper.cpp made no CPU backends (libggml-cpu*.so; it made {', '.join(sorted(libs))})")
    for name, p in libs.items():
        shutil.copy2(p, built / "libs" / name)
    (built / "done").write_text(f"pywhispercpp {PWCPP_VERSION}, {' '.join(WHISPER_CMAKE_ARGS)}\n")
    rmtree(work)
    rmtree(ROOT / "build" / "t")
    return built


def install_whisper_cpp_vulkan(py: Path, reqs: str) -> None:
    """The compiled whisper.cpp, with its libraries next to it under their own names (the backends, loaded on
    their own, look for libggml-base.so by that name) and OpenMP's runtime; everything else from wheels, as
    without it."""
    whisper = whisper_cpp_vulkan(py)
    wheel = next(whisper.glob("pywhispercpp-*.whl"))
    run([str(py), "-m", "pip", "install", "-q", "--no-compile", "--no-warn-script-location", "--only-binary", ":all:", str(wheel), "-r", reqs],
        env={**os.environ, **PIP_ENV})
    site = next((py.parent.parent / "lib").glob("python3.*")) / "site-packages"
    for p in site.glob("lib*.so*"):  # (the wheel's own copies of them, if any)
        p.unlink()
    for p in (whisper / "libs").glob("*.so*"):
        shutil.copy2(p, site / p.name)
    for name in BUNDLED_LIBS:
        source = system_lib(name)
        print(f"  bundling {name} from {source}", flush=True)
        shutil.copy2(source, site / name)


def check_whisper_cpp_vulkan(py: Path, site: Path, env: dict) -> None:
    """The GPU build holds together: its backends are there, everything they link against is bundled, on every
    Linux desktop or (for Vulkan's) the graphics drivers' Vulkan loader, and whisper.cpp finds its CPU backend."""
    libs = sorted(site.glob("lib*.so*"))
    names = {p.name for p in libs}
    print(f"whisper.cpp's libraries ({sum(p.stat().st_size for p in libs) / 1e6:.0f} MB): {', '.join(sorted(names))}", flush=True)
    if "libggml-vulkan.so" not in names or not any(n.startswith("libggml-cpu") for n in names):
        die("whisper.cpp's backends (libggml-vulkan.so, libggml-cpu*.so) aren't bundled")
    if any(".so." in n for n in names - set(BUNDLED_LIBS)):
        die(f"whisper.cpp's libraries should be there under one name each: {', '.join(sorted(names))}")
    newest: dict[str, tuple] = {}
    for p in [*libs, *site.glob("_pywhispercpp*.so")]:
        dyn = elf_dynamic(p)
        for need in dyn["needed"]:
            if need in names or need.startswith(SYSTEM_LIBS) or (need == "libvulkan.so.1" and p.name == "libggml-vulkan.so"):
                continue
            die(f"{p.name} needs {need}, which is neither bundled nor on every Linux desktop")
        if p.name not in BUNDLED_LIBS and "$ORIGIN" not in (dyn["runpath"] or ""):
            die(f"{p.name} doesn't look for its libraries next to it ($ORIGIN): {dyn['runpath']}")
        for lib, version in re.findall(rb"(GLIBCXX|GLIBC)_(\d+(?:\.\d+)+)", p.read_bytes()):
            v = tuple(int(n) for n in version.split(b"."))
            newest[lib.decode()] = max(newest.get(lib.decode(), v), v)
    # (as Tauri's own binary does, it needs this build machine's C library and C++ runtime, or newer ones)
    print("whisper.cpp needs " + ", ".join(f"{k} {'.'.join(map(str, v))}" for k, v in sorted(newest.items())), flush=True)
    code = (f"import sys; sys.path.insert(0, {str(ROOT / 'backend')!r}); import whispercpp_engine; "
            "print('whisper.cpp:', whispercpp_engine.system_info())")
    done = subprocess.run([str(py), "-I", "-c", code], env=env, capture_output=True, text=True)
    if done.returncode != 0 or "CPU :" not in done.stdout:
        die(f"whisper.cpp's backends don't load:\n{done.stdout[-2000:]}\n{done.stderr[-3000:]}")
    print(done.stdout.strip(), flush=True)
    probe = subprocess.run([str(py), "-I", "-c", f"import ctypes; ctypes.CDLL({str(site / 'libggml-vulkan.so')!r})"], env=env, capture_output=True)
    print("Vulkan's backend loads here (there's a libvulkan.so.1)" if probe.returncode == 0 else
          "Vulkan's backend doesn't load here (no libvulkan.so.1: no Vulkan driver), so whisper.cpp runs on the CPU", flush=True)


def try_vulkan() -> None:
    """--try-vulkan, after a GPU build: the staged whisper.cpp transcribes real speech through its Vulkan backend on
    Mesa's software GPU (lavapipe, from mesa-vulkan-drivers), which works on a computer without a graphics card, as
    CI's. whisper.cpp leaves software GPUs out by itself, so it's picked by hand (GGML_VK_VISIBLE_DEVICES): it shows
    the shaders and the backend compute the right words, not how fast a real GPU is."""
    py = STAGE / "python" / "bin" / "python3"
    if not (STAGE / "backend").is_dir() or not list(STAGE.rglob("libggml-vulkan.so")):
        die("--try-vulkan needs a GPU build staged in build/linux (TRAILMIX_GPU_WHISPER=1)")
    code = """if True:
        import sys, wave
        import numpy as np
        import models, whispercpp_engine as engine
        with wave.open(sys.argv[1]) as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        model = "ggerganov/whisper.cpp:ggml-tiny.bin"
        models.path(model)
        print("whisper.cpp", engine.system_info())
        print("DEVICE:", engine.device())
        print("TEXT:", " ".join(s["text"] for s in engine.run(audio, model, None, "en")))
    """
    with tempfile.TemporaryDirectory() as data:
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8", "TRAILMIX_DATA_DIR": data,
               "HF_HOME": os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface"), "GGML_VK_VISIBLE_DEVICES": "0"}
        done = subprocess.run([str(py), "-c", code, str(ROOT / "scripts" / "fixtures" / "them.wav")], cwd=STAGE / "backend", env=env,
                              capture_output=True, text=True, timeout=1200)
    print(done.stdout.strip(), flush=True)
    device = next((line[8:] for line in done.stdout.splitlines() if line.startswith("DEVICE: ")), "")
    text = next((line[6:] for line in done.stdout.splitlines() if line.startswith("TEXT: ")), "").lower()
    if done.returncode != 0 or not device.startswith("the GPU"):
        die(f"whisper.cpp didn't run on the software GPU (exit {done.returncode}; on {device or 'nothing'}):\n{done.stderr[-3000:]}")
    if not all(word in text for word in ("login", "wednesday", "pricing")):
        die(f"whisper.cpp got the words wrong on Vulkan: {text!r}")
    say(f"whisper.cpp transcribed through Vulkan, on {device}")


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


def main() -> None:
    if not sys.platform.startswith("linux"):
        die("The Linux app is built on Linux.")
    if "--try-vulkan" in sys.argv:
        try_vulkan()
        return
    if "--packages-only" in sys.argv and (STAGE / "python").is_dir():
        say(f"Using what's staged in {STAGE} already")
    else:
        stage()
    if "--stage-only" not in sys.argv:
        build_packages()


if __name__ == "__main__":
    main()
