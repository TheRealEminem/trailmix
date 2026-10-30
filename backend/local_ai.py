"""Local AI servers already running on this computer: Ollama, LM Studio, Jan, llama.cpp's server, vLLM and the like.
Trailmix asks each usual port on this computer (never another machine), so setup and Settings can offer whatever is
there in one list instead of asking for an address.

Apart from Ollama (its own provider, /api/tags) each answers OpenAI's GET /v1/models and is used through the
OpenAI-compatible provider. Which app it is comes from the port, or the answer's "owned_by" where that says. The
context window, where a server tells it, decides how much transcript goes in one request (custom_context_tokens).
"""
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

log = logging.getLogger("trailmix")

OLLAMA_PORT = 11434
# Port -> the app that uses it by default (None: shared by several, so only "owned_by" can say which).
PORTS = {OLLAMA_PORT: "Ollama", 1234: "LM Studio", 1337: "Jan", 8080: None, 8000: None}
OWNERS = {"vllm": "vLLM", "llamacpp": "llama.cpp"}
UNKNOWN = "Local AI server"
TIMEOUT = httpx.Timeout(2.0, connect=0.5)  # a closed port refuses at once; a busy server gets a moment
CACHE_S = 10.0
# Below this a server's context can't hold the instructions and a useful stretch of meeting (setup says so).
SMALL_CONTEXT = 12000

_lock = threading.Lock()
_cache: tuple[float, list[dict]] = (0.0, [])


def _is_text_model(entry: dict) -> bool:
    """Embedding models are listed too, but can't write notes."""
    return "embed" not in str(entry.get("id") or entry.get("name") or "").lower() and \
        entry.get("type") not in ("embeddings", "embedding")


def _context_of(entry: dict) -> int | None:
    """The context window a /v1/models entry gives, under whichever name its server uses."""
    for key in ("loaded_context_length", "max_model_len", "context_length", "max_context_length"):
        if isinstance(entry.get(key), int) and entry[key] > 0:
            return entry[key]
    ctx = (entry.get("settings") or {}).get("ctx_len")  # Jan
    return ctx if isinstance(ctx, int) and ctx > 0 else None


def parse_models(data: dict) -> list[dict]:
    """[{"id", "context"}] from a /v1/models (or LM Studio /api/v0/models) answer, text models only."""
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [{"id": e["id"], "context": _context_of(e)} for e in entries
            if isinstance(e, dict) and isinstance(e.get("id"), str) and _is_text_model(e)]


def name_for(port: int, data: dict) -> str:
    """Which app answered: its "owned_by" where that names one (vLLM, llama.cpp), else the port's usual app."""
    for e in data.get("data") or []:
        owner = OWNERS.get(str((e or {}).get("owned_by", "")).lower())
        if owner:
            return owner
    return PORTS.get(port) or UNKNOWN


def _ollama(client: httpx.Client) -> dict | None:
    r = client.get(f"http://localhost:{OLLAMA_PORT}/api/tags")
    if r.status_code != 200:
        return None
    models = [{"id": m["name"], "context": None} for m in r.json().get("models", [])
              if isinstance(m, dict) and isinstance(m.get("name"), str) and _is_text_model(m)]
    return {"name": "Ollama", "kind": "ollama", "port": OLLAMA_PORT, "url": f"http://localhost:{OLLAMA_PORT}",
            "models": models}


def _extra_context(client: httpx.Client, port: int, name: str, models: list[dict]) -> None:
    """Fills in context windows that /v1/models leaves out, from the server's own API where it has one."""
    base = f"http://localhost:{port}"
    try:
        if name == "LM Studio":  # /api/v0/models has each model's loaded (or maximum) context
            known = {m["id"]: m["context"] for m in parse_models(client.get(f"{base}/api/v0/models").json())}
            for m in models:
                m["context"] = m["context"] or known.get(m["id"])
        elif name == "llama.cpp":  # one model, its context in /props
            props = client.get(f"{base}/props").json()
            ctx = (props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx")
            if isinstance(ctx, int) and ctx > 0:
                for m in models:
                    m["context"] = m["context"] or ctx
    except (httpx.HTTPError, ValueError, AttributeError):
        pass


def _probe(port: int) -> dict | None:
    """The server at this port on this computer, or None if nothing (or nothing AI) answers there."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            if port == OLLAMA_PORT:
                return _ollama(client)
            r = client.get(f"http://localhost:{port}/v1/models")
            if r.status_code != 200:
                return None
            data = r.json()
            models = parse_models(data)
            if not models:
                return None
            name = name_for(port, data)
            _extra_context(client, port, name, models)
            return {"name": name, "kind": "openai", "port": port, "url": f"http://localhost:{port}/v1",
                    "models": models}
    except (httpx.HTTPError, ValueError, KeyError, AttributeError):
        return None


def scan(fresh: bool = False) -> list[dict]:
    """The AI servers answering on this computer, all ports asked at once: [{"name", "kind": "ollama" | "openai",
    "port", "url", "models": [{"id", "context"}]}]. Remembered for a few seconds."""
    global _cache
    with _lock:
        at, found = _cache
        if not fresh and time.monotonic() - at < CACHE_S:
            return found
    with ThreadPoolExecutor(max_workers=len(PORTS)) as pool:
        found = [s for s in pool.map(_probe, PORTS) if s]
    with _lock:
        _cache = (time.monotonic(), found)
    return found


def _app_paths() -> dict[str, list[Path]]:
    home = Path.home()
    if sys.platform == "darwin":
        apps = [Path("/Applications"), home / "Applications"]
        return {"LM Studio": [a / "LM Studio.app" for a in apps], "Jan": [a / "Jan.app" for a in apps]}
    if sys.platform == "win32":
        programs = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
        return {"LM Studio": [programs / "LM Studio" / "LM Studio.exe", programs / "lm-studio" / "LM Studio.exe"],
                "Jan": [programs / "jan" / "Jan.exe", programs / "Jan" / "Jan.exe"]}
    return {"LM Studio": [], "Jan": []}


HINTS = {
    "LM Studio": "LM Studio is installed, but its server is off. In LM Studio, open Developer and switch the server on.",
    "Jan": "Jan is installed, but its server is off. In Jan, open Settings → Local API Server and start it.",
}


def installed_not_running(found: list[dict]) -> list[dict]:
    """[{"name", "hint"}] for local AI apps installed here whose server isn't answering (Ollama has its own setup)."""
    running = {s["name"] for s in found}
    paths = _app_paths()
    paths["LM Studio"] = paths["LM Studio"] + [Path.home() / ".lmstudio"]  # made by LM Studio's first run, on any OS
    return [{"name": name, "hint": HINTS[name]} for name, where in paths.items()
            if name not in running and any(p.exists() for p in where)]


def in_use(server: dict, cfg: dict) -> bool:
    """Whether the notes AI (or its fallback) is set to this server."""
    def same(a: str, b: str) -> bool:
        def norm(u: str) -> str:
            return (u or "").rstrip("/").replace("127.0.0.1", "localhost").replace("[::1]", "localhost")
        return norm(a) == norm(b)

    chosen = {cfg.get("summary_provider"), cfg.get("summary_fallback")}
    if server["kind"] == "ollama":
        return "ollama" in chosen and same(server["url"], cfg.get("ollama_url", ""))
    return "custom" in chosen and same(server["url"], cfg.get("custom_base_url", ""))


def status(cfg: dict, fresh: bool = False) -> dict:
    """For /api/local-ai: {"servers": [... with "in_use"], "installed": [{"name", "hint"}], "small_context"}."""
    found = scan(fresh)
    return {"servers": [{**s, "in_use": in_use(s, cfg)} for s in found], "installed": installed_not_running(found),
            "small_context": SMALL_CONTEXT}
