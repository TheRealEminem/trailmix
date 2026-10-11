"""Text generation across providers: local Ollama, or any cloud API you have a key for.

Every provider is reduced to one call, generate(provider, cfg, prompt, system) -> text, so summaries,
titles and Q&A work the same whichever one you pick. `cfg` is settings.get_all().
"""
import json
import os
import re
from collections.abc import Callable, Iterator
from urllib.parse import urlparse

import httpx

import meeting_text
import templates

OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "0")  # "0" = unload right after the last call
TIMEOUT = httpx.Timeout(600, connect=8)
MAX_TOKENS = 16000
OLLAMA_NUM_CTX = 16384  # a bigger window costs RAM a 16 GB Mac doesn't have to spare
# How much text (in tokens) each provider can take in one request. Unknown OpenAI-compatible servers are
# often small local models, so they get the conservative figure.
CONTEXT_TOKENS = {"ollama": OLLAMA_NUM_CTX, "custom": 16384, "deepseek": 60000}
DEFAULT_CONTEXT_TOKENS = 120000
CHARS_PER_TOKEN = 3.0      # meeting transcripts run ~3.1 characters per token; err on the safe side
RESERVED_TOKENS = 4500     # the reply, the instructions and the meeting description

PROVIDERS = {
    "ollama": {"label": "Ollama", "kind": "local"},
    "anthropic": {"label": "Claude (Anthropic)", "kind": "cloud"},
    "openai": {"label": "OpenAI", "kind": "cloud", "base": "https://api.openai.com/v1"},
    "gemini": {"label": "Google Gemini", "kind": "cloud"},
    "deepseek": {"label": "DeepSeek", "kind": "cloud", "base": "https://api.deepseek.com"},
    "custom": {"label": "OpenAI-compatible", "kind": "custom"},
}
# Claude models that accept server-side refusal fallback ("default" routing picks the substitute).
_CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

TITLE_PROMPT = """Write a short, specific title (3 to 8 words) for the meeting described by these notes.
Reply with the title only: no quotes, no trailing punctuation, no "Title:" prefix.

NOTES:
{summary}
"""


class LLMError(Exception):
    pass


OnText = Callable[[str], None] | None  # called with everything written so far, as a reply streams in


# ── Provider facts ──────────────────────────────────────────────────────

def label(pid: str, cfg: dict) -> str:
    return (cfg.get("custom_name") or "OpenAI-compatible") if pid == "custom" else PROVIDERS[pid]["label"]


def is_local(pid: str, cfg: dict) -> bool:
    """True when the model would run on this machine (so RAM gates and the recording pause apply)."""
    if pid != "ollama":
        return False
    return (urlparse(cfg["ollama_url"]).hostname or "") in ("localhost", "127.0.0.1", "::1")


def runs_here(pid: str, cfg: dict) -> bool:
    """The model runs on this computer: Ollama, or an OpenAI-compatible server, at a local address."""
    if pid == "ollama":
        return is_local(pid, cfg)
    if pid == "custom":
        return (urlparse(cfg["custom_base_url"]).hostname or "") in ("localhost", "127.0.0.1", "::1")
    return False


def configured(pid: str, cfg: dict) -> tuple[bool, str]:
    """(ready, reason-if-not). Cheap: never makes a network call except for Ollama's tag list."""
    if cfg.get("lockdown") and not runs_here(pid, cfg):
        return False, (f"Lockdown mode is on, so {label(pid, cfg)} can't be used: only AI on this computer can write "
                       "notes (Settings → Privacy)")
    if pid == "ollama":
        return (True, "") if _ollama_tags(cfg) else (False, f"Ollama isn't reachable at {cfg['ollama_url']} (or has no models)")
    if pid == "custom":
        if not cfg["custom_base_url"]:
            return False, "Set a base URL for the OpenAI-compatible provider"
        if not cfg["custom_model"]:
            return False, "Pick a model for the OpenAI-compatible provider"
        return True, ""
    if not cfg[f"{pid}_api_key"]:
        return False, f"Add an API key for {PROVIDERS[pid]['label']} in Settings"
    return True, ""


def model_for(pid: str, cfg: dict) -> str:
    if pid == "ollama":
        if cfg["ollama_model"]:
            return cfg["ollama_model"]
        return preferred_ollama_model(_ollama_tags(cfg))
    return cfg[f"{pid}_model"]


# Families that follow a fixed note format well, best first. Used when no Ollama model is picked in Settings.
_PREFERRED_FAMILIES = ("qwen3", "qwen2.5", "gemma3", "llama3.1", "llama3.2", "mistral", "phi4", "llama3")


def preferred_ollama_model(tags: list[dict]) -> str:
    """The installed model most likely to write good notes: a known instruction-following family, and within
    it the biggest size up to ~14B (bigger rarely fits next to everything else in RAM)."""
    def params(m: dict) -> float:
        text = (m.get("details") or {}).get("parameter_size", "")
        found = re.match(r"([\d.]+)\s*([BM])", text, re.I)
        return float(found.group(1)) / (1000 if found.group(2).upper() == "M" else 1) if found else 0.0

    def rank(m: dict) -> tuple:
        name = m["name"].lower()
        family = next((i for i, f in enumerate(_PREFERRED_FAMILIES) if name.startswith(f)), len(_PREFERRED_FAMILIES))
        size = params(m)
        return (family, size > 15, -size)

    usable = [m for m in tags if "embed" not in m["name"].lower()]
    return min(usable, key=rank)["name"] if usable else ""


def context_chars(pid: str, cfg: dict) -> int:
    """How much transcript (in characters) fits in one request to this provider, leaving room for the rest."""
    tokens = CONTEXT_TOKENS.get(pid, DEFAULT_CONTEXT_TOKENS)
    return int((tokens - RESERVED_TOKENS) * CHARS_PER_TOKEN)


def chain_context_chars(cfg: dict, choice: str = "auto") -> int:
    """The smallest budget among the providers that may answer, so a fallback isn't overfilled either."""
    return min(context_chars(pid, cfg) for pid in chain(choice, cfg))


def chain(choice: str, cfg: dict) -> list[str]:
    """Which providers to try, in order. 'auto' = your primary, then your fallback."""
    if choice in PROVIDERS:
        return [choice]
    out = [cfg["summary_provider"]]
    if cfg["summary_fallback"] not in ("none", cfg["summary_provider"]):
        out.append(cfg["summary_fallback"])
    return out


# ── Ollama ──────────────────────────────────────────────────────────────

def _ollama_tags(cfg: dict) -> list[dict]:
    try:
        r = httpx.get(f"{cfg['ollama_url']}/api/tags", timeout=2)
        r.raise_for_status()
        return r.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return []


def ollama_model_size_gb(cfg: dict) -> float | None:
    tags = _ollama_tags(cfg)
    name = model_for("ollama", cfg)
    chosen = next((m for m in tags if m["name"] == name), None)
    return chosen["size"] / 1024**3 if chosen else None


def ollama_ram_needed_gb(cfg: dict) -> float:
    """Estimated RAM to load the chosen model plus its context window."""
    return (ollama_model_size_gb(cfg) or 0) * 1.1 + 1.5


def _ollama(cfg: dict, prompt: str, system: str | None, keep_alive: str, temperature: float = 0.2,
            on_text: OnText = None) -> str:
    model = model_for("ollama", cfg)
    if not model:
        raise LLMError(f"Ollama isn't reachable at {cfg['ollama_url']} (or has no models installed)")
    body = {
        "model": model,
        "prompt": prompt,
        "stream": bool(on_text),
        "keep_alive": keep_alive,
        "options": {"num_ctx": OLLAMA_NUM_CTX, "temperature": temperature},
    }
    if system:
        body["system"] = system
    url = f"{cfg['ollama_url']}/api/generate"
    if not on_text:
        return _post(url, json=body).json().get("response") or ""
    text = ""
    for line in _stream_lines(url, json=body):
        part = json.loads(line)
        if part.get("error"):
            raise LLMError(f"Ollama: {part['error']}")
        if part.get("response"):
            text += part["response"]
            on_text(text)
    return text


# ── Cloud providers ─────────────────────────────────────────────────────

def _post(url: str, **kw) -> httpx.Response:
    try:
        r = httpx.post(url, timeout=TIMEOUT, **kw)
    except httpx.HTTPError as e:
        raise LLMError(f"Couldn't reach {urlparse(url).netloc}: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"{urlparse(url).netloc} returned {r.status_code}: {_error_text(r)}")
    return r


def _stream_lines(url: str, **kw) -> Iterator[str]:
    """The non-empty lines of a streamed reply, failing the same way _post does."""
    try:
        with httpx.stream("POST", url, timeout=TIMEOUT, **kw) as r:
            if r.status_code >= 400:
                r.read()
                raise LLMError(f"{urlparse(url).netloc} returned {r.status_code}: {_error_text(r)}")
            for line in r.iter_lines():
                if line.strip():
                    yield line
    except httpx.HTTPError as e:
        raise LLMError(f"Couldn't reach {urlparse(url).netloc}: {e}") from e


def _sse(url: str, **kw) -> Iterator[dict]:
    """Server-sent events ("data: {...}" lines) as JSON, up to OpenAI's "data: [DONE]"."""
    for line in _stream_lines(url, **kw):
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except ValueError:
            continue


def _error_text(r: httpx.Response) -> str:
    try:
        return _error_text_of(r.json())
    except ValueError:
        return r.text[:300]


def _error_text_of(data) -> str:
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, list) and err:  # Gemini sometimes wraps it in a list
        err = err[0].get("error", err[0]) if isinstance(err[0], dict) else err[0]
    return str(err.get("message") if isinstance(err, dict) else err)[:300]


def _anthropic(cfg: dict, prompt: str, system: str | None, on_text: OnText = None) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"], timeout=600, max_retries=2)
    model = cfg["anthropic_model"]
    params = {"model": model, "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": prompt}]}
    if system:
        params["system"] = system
    # If a safety classifier declines, the API re-runs the request on a suitable model.
    fallback = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in _CLAUDE_FALLBACK_MODELS else None
    try:
        if on_text:
            manager = client.beta.messages.stream(**params, **fallback) if fallback else client.messages.stream(**params)
            with manager as stream:
                text = ""
                for chunk in stream.text_stream:
                    text += chunk
                    on_text(text)
                msg = stream.get_final_message()  # the reply itself; the streamed text was a preview
        elif fallback:
            msg = client.beta.messages.create(**params, **fallback)
        else:
            msg = client.messages.create(**params)
    except anthropic.AuthenticationError as e:
        raise LLMError("Claude rejected the API key") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"Claude model '{model}' wasn't found") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Claude rate limit hit; try again in a moment") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Couldn't reach the Claude API: {e}") from e
    if msg.stop_reason == "refusal":
        raise LLMError("Claude declined this request")
    return "".join(b.text for b in msg.content if b.type == "text")


def _openai_compatible(base: str, key: str, model: str, prompt: str, system: str | None, pid: str,
                       on_text: OnText = None) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body: dict = {"model": model, "messages": messages}
    if pid == "openai":
        body["max_completion_tokens"] = MAX_TOKENS  # newer OpenAI models reject max_tokens / custom temperature
    elif pid == "deepseek":
        body["max_tokens"] = 8000
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if on_text:
        text = ""
        for event in _sse(f"{base}/chat/completions", json={**body, "stream": True}, headers=headers):
            if event.get("error"):
                raise LLMError(f"{urlparse(base).netloc}: {_error_text_of(event)}")
            for choice in event.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    text += piece
                    on_text(text)
        return text
    r = _post(f"{base}/chat/completions", json=body, headers=headers)
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError(f"Unexpected response from {base}") from e


def _gemini(cfg: dict, prompt: str, system: str | None, on_text: OnText = None) -> str:
    body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    headers = {"x-goog-api-key": cfg["gemini_api_key"]}
    if on_text:
        text = ""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['gemini_model']}:streamGenerateContent?alt=sse"
        for event in _sse(url, json=body, headers=headers):
            if event.get("error"):
                raise LLMError(f"Gemini: {_error_text_of(event)}")
            for cand in event.get("candidates") or []:
                for part in (cand.get("content") or {}).get("parts") or []:
                    if part.get("text") and not part.get("thought"):
                        text += part["text"]
                        on_text(text)
        return text
    r = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['gemini_model']}:generateContent",
        json=body,
        headers=headers,
    )
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError("Gemini returned no text (it may have blocked the request)") from e
    return "".join(p.get("text", "") for p in parts)


def generate(pid: str, cfg: dict, prompt: str, system: str | None = None, keep_alive: str | None = None,
             exact: bool = False, on_text: OnText = None) -> str:
    """`exact`: the same answer every time (no sampling) where the provider allows it, for picking from a list.
    `on_text`: stream the reply, calling it with the text so far as it's written."""
    ok, why = configured(pid, cfg)
    if not ok:
        raise LLMError(why)
    if pid == "ollama":
        text = _ollama(cfg, prompt, system, keep_alive or OLLAMA_KEEP_ALIVE, 0.0 if exact else 0.2, on_text)
    elif pid == "anthropic":
        text = _anthropic(cfg, prompt, system, on_text)
    elif pid == "gemini":
        text = _gemini(cfg, prompt, system, on_text)
    elif pid == "custom":
        text = _openai_compatible(cfg["custom_base_url"], cfg["custom_api_key"], cfg["custom_model"], prompt, system, pid,
                                  on_text)
    else:
        text = _openai_compatible(PROVIDERS[pid]["base"], cfg[f"{pid}_api_key"], cfg[f"{pid}_model"], prompt, system, pid,
                                  on_text)
    text = text.strip()
    if not text:
        raise LLMError(f"{label(pid, cfg)} returned an empty response")
    return text


def run_chain(choice: str, cfg: dict, fn) -> tuple[object, str]:
    """Calls fn(provider) for each provider in the chain until one succeeds. Returns (result, provider)."""
    errors = []
    for pid in chain(choice, cfg):
        try:
            return fn(pid), pid
        except LLMError as e:
            errors.append(f"{label(pid, cfg)}: {e}")
    raise LLMError("; ".join(errors))


# ── Model discovery & connection tests ─────────────────────────────────

def list_models(pid: str, cfg: dict) -> list[str]:
    if pid == "ollama":
        tags = _ollama_tags(cfg)
        if not tags:
            raise LLMError(f"Ollama isn't reachable at {cfg['ollama_url']}")
        return [m["name"] for m in tags if "embed" not in m["name"].lower()]  # embedding models can't write notes
    if pid == "anthropic":
        import anthropic

        if not cfg["anthropic_api_key"]:
            raise LLMError("Add an API key first")
        try:
            return [m.id for m in anthropic.Anthropic(api_key=cfg["anthropic_api_key"], timeout=20).models.list()]
        except anthropic.APIError as e:
            raise LLMError(f"Claude: {e}") from e
    if pid == "gemini":
        if not cfg["gemini_api_key"]:
            raise LLMError("Add an API key first")
        r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                      headers={"x-goog-api-key": cfg["gemini_api_key"]}, timeout=20)
        if r.status_code >= 400:
            raise LLMError(f"Gemini returned {r.status_code}: {_error_text(r)}")
        return [m["name"].removeprefix("models/") for m in r.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])]
    base = cfg["custom_base_url"] if pid == "custom" else PROVIDERS[pid]["base"]
    key = cfg[f"{pid}_api_key"]
    if not base:
        raise LLMError("Set a base URL first")
    if pid != "custom" and not key:
        raise LLMError("Add an API key first")
    try:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=20)
    except httpx.HTTPError as e:
        raise LLMError(f"Couldn't reach {base}: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"{urlparse(base).netloc} returned {r.status_code}: {_error_text(r)}")
    return sorted(m["id"] for m in r.json().get("data", []))


def test(pid: str, cfg: dict) -> str:
    reply = generate(pid, cfg, "Reply with exactly: OK", keep_alive=OLLAMA_KEEP_ALIVE)
    return f"{label(pid, cfg)} · {model_for(pid, cfg)} replied “{reply[:40]}”"


# ── Meeting tasks ───────────────────────────────────────────────────────

def clean_title(raw: str) -> str | None:
    first = next((ln for ln in raw.strip().splitlines() if ln.strip()), "")
    title = re.sub(r"^\s*(meeting\s+)?title\s*[:\-]\s*", "", first, flags=re.I).strip(" \t\"'*#`.")
    return title[:80] or None


def split_lines(text: str, limit: int) -> list[str]:
    """Splits text into pieces of at most `limit` characters, only between lines."""
    pieces, current, size = [], [], 0
    for line in text.splitlines():
        line = line[:limit]
        if current and size + len(line) + 1 > limit:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return pieces


_PREAMBLE_MAX = 800
_CLOSER = re.compile(r"^\s*(let me know|i hope|feel free|if you (need|want|have|would)|please note|note:|overall,? (this|the) (meeting|conversation))", re.I)


def tidy(text: str) -> str:
    """Strips what models wrap notes in: code fences, "Here are the notes…" preambles, "Let me know…" closers."""
    lines = [ln for ln in text.strip().splitlines() if not re.match(r"^\s*```(?:markdown|md)?\s*$", ln, re.I)]
    first = next((i for i, ln in enumerate(lines) if re.match(r"^#{1,4}\s", ln)), None)
    if first and len("\n".join(lines[:first])) <= _PREAMBLE_MAX:
        lines = lines[first:]
    while lines and (not lines[-1].strip() or _CLOSER.match(lines[-1])):
        lines.pop()
    return "\n".join(_drop_stray_none(lines)).strip()


_NONE_BULLET = re.compile(r"^\s*[-*+]\s*(?:\[ \]\s*)?none\.?\s*$", re.I)


def _drop_stray_none(lines: list[str]) -> list[str]:
    """A "- None" placeholder in a section that also has real bullets (small models add both)."""
    out, section = [], []

    def flush():
        real = [ln for ln in section if re.match(r"^\s*[-*+]\s", ln) and not _NONE_BULLET.match(ln)]
        out.extend(ln for ln in section if not (real and _NONE_BULLET.match(ln)))
        section.clear()

    for ln in lines:
        if re.match(r"^#{1,4}\s", ln):
            flush()
        section.append(ln)
    flush()
    return out


_ACTION_HEADING = re.compile(r"^#{1,4}\s*action items\b", re.I | re.M)
_MOMENTS_HEADING = re.compile(r"^#{1,4}\s*flagged moments\b", re.I)


def place_moments(summary: str, moments: list[str]) -> str:
    """The notes' "Flagged Moments" section, made from the real flags (what was being said at each one),
    just before Action Items. Any such section the model wrote anyway is dropped: they invent flags."""
    out, skipping = [], False
    for line in summary.splitlines():
        if re.match(r"^#{1,4}\s", line):
            skipping = bool(_MOMENTS_HEADING.match(line))
        if not skipping:
            out.append(line)
    summary = "\n".join(out).strip()
    if not moments:
        return summary
    section = "## Flagged Moments\n" + "\n".join(f"- {m}" for m in moments)
    lines = summary.splitlines()
    at = next((i for i, ln in enumerate(lines) if _ACTION_HEADING.match(ln)), None)
    if at is None:
        return f"{summary}\n\n{section}"
    before, after = "\n".join(lines[:at]).rstrip(), "\n".join(lines[at:])
    return f"{before}\n\n{section}\n\n{after}" if before else f"{section}\n\n{after}"
_WORD = re.compile(r"[A-Za-z]+|\d[\d,.]*\d|\d")
_COMMON = set("""the and for with that this from have about what when were they them their there would could
should which while into your just like want also some more most than then been being will idea think liked""".split())


def _reflected(note: str, summary: str) -> bool:
    """Whether the notes cover a typed note: at least half its telling words (numbers, names, longer words)."""
    text = meeting_text._URL.sub(" ", re.sub(r"^\[[\d:]+\]\s*", "", note))  # links are checked on their own
    words = {w.lower() for w in _WORD.findall(text)}
    telling = {w for w in words if (len(w) > 4 or any(c.isdigit() for c in w)) and w not in _COMMON}
    if not telling:
        return True
    lower = summary.lower()
    return sum(w in lower for w in telling) * 2 >= len(telling)


def keep_mine(summary: str, mine: list[str]) -> str:
    """Nothing you typed gets lost: notes the model didn't work in go under "Your Notes" word for word, and any
    link still missing goes under "Links"."""
    left_out = [re.sub(r"^\[[\d:]+\]\s*", "", n) for n in mine if not _reflected(n, summary)]
    if left_out:
        summary = f"{summary.rstrip()}\n\n## Your Notes\n" + "\n".join(f"- {n}" for n in left_out)
    missing = [u for u in meeting_text.links(list(mine)) if u not in summary]
    if missing:
        summary = f"{summary.rstrip()}\n\n## Links\n" + "\n".join(f"- {u}" for u in missing)
    return summary


_TIMESTAMP = re.compile(r"^\[(\d+(?::\d\d)+)\]", re.M)


def _span(part: str) -> str:
    """'NOTES ON [00:00] TO [22:41]' from a piece of transcript (or plain 'NOTES' for notes on notes)."""
    stamps = _TIMESTAMP.findall(part)
    return f"NOTES ON [{stamps[0]}] TO [{stamps[-1]}]" if stamps else "NOTES"


def _stamps(part: str) -> tuple[str, str] | None:
    """The first and last timestamps in a piece of transcript."""
    stamps = _TIMESTAMP.findall(part)
    return (stamps[0], stamps[-1]) if stamps else None


def _draft_part(span: tuple[str, str] | None, notes: str) -> str:
    """One part's notes as you see them while the summary is written: under a heading for the stretch of the
    meeting they cover, with the model's own headings a level down."""
    heading = f"## Notes on {span[0]} to {span[1]}" if span else "## Notes"
    return heading + "\n" + re.sub(r"^#{1,3}\s", "### ", tidy(notes), flags=re.M)


_LINE_TIME = re.compile(r"^\[(?:(\d+):)?(\d+):(\d\d)\]")


def after(transcript: str, seconds: float, overlap: float = 90) -> str:
    """The transcript from `seconds` on. A turn can run up to 90 s past its timestamp, so the turns that start
    up to `overlap` seconds before are kept too: a little repeated beats a little lost."""
    out, keep = [], False
    for line in transcript.splitlines():
        t = _LINE_TIME.match(line)
        if t:
            keep = int(t.group(1) or 0) * 3600 + int(t.group(2)) * 60 + int(t.group(3)) >= seconds - overlap
        if keep:
            out.append(line)
    return "\n".join(out)


def unload(pid: str, cfg: dict) -> None:
    """Lets a local Ollama model go right away instead of when its keep-alive runs out."""
    if pid != "ollama" or OLLAMA_KEEP_ALIVE != "0" or not cfg.get("ollama_url"):
        return
    try:
        httpx.post(f"{cfg['ollama_url']}/api/generate", json={"model": model_for("ollama", cfg), "keep_alive": 0}, timeout=10)
    except httpx.HTTPError:
        pass


def summarize(transcript: str, cfg: dict, choice: str, template: str, moments: list[str],
              want_title: bool, about: str = "", minutes: float = 30, on_progress=None,
              on_draft: OnText = None, early_notes: tuple[list[dict], float] | None = None,
              mine: list[str] = ()) -> tuple[str, str, str | None]:
    """Returns (summary, provider_used, title). The title is best-effort and never fails the summary.

    A transcript too long for the provider's context is summarized in parts first (notes on each part),
    and the notes are then written up; nothing is ever silently cut off.
    `on_draft` gets the notes so far as they're written: each part's notes as they come, then the write-up
    (which replaces them once it starts). The returned summary is the finished, tidied version.
    `early_notes`: (notes written during the meeting, the seconds they cover), from live_notes.usable. Used
    only when the transcript is too long for one go: then only the rest of the meeting needs notes first."""
    if not transcript.strip():
        raise LLMError("Transcript is empty (no speech detected)")

    def run(pid: str):
        budget = context_chars(pid, cfg)
        hold = "5m"  # keep a local model loaded between the calls of one summary, then let it unload
        notes, shown = [], []  # what the write-up reads, and what you watch being written

        def take_notes(parts: list[str], progress: str) -> None:
            for i, part in enumerate(parts, 1):
                if on_progress:
                    on_progress(progress.format(i=i, n=len(parts)))
                system, prompt = templates.notes_prompt(part, i, len(parts), about)
                show = None
                if on_draft:
                    show = lambda t, part=part: on_draft("\n\n".join(shown + [_draft_part(_stamps(part), t)]))
                text = tidy(generate(pid, cfg, prompt, system=system, keep_alive=hold, on_text=show))
                notes.append(f"{_span(part)}:\n{text}")
                shown.append(_draft_part(_stamps(part), text))

        material, from_notes = transcript, False
        if early_notes and len(transcript) > budget:
            # Notes written during the meeting (live_notes.py) cover its start: only the rest needs notes now.
            done, until = early_notes
            for n in done:
                span = (meeting_text.fmt(n["start"]), meeting_text.fmt(n["end"]))
                notes.append(f"NOTES ON [{span[0]}] TO [{span[1]}]:\n{n['text']}")
                shown.append(_draft_part(span, n["text"]))
            if on_draft and shown:
                on_draft("\n\n".join(shown))
            rest = after(transcript, until)
            if rest.strip():
                take_notes(split_lines(rest, budget), "Notes on the rest of the meeting ({i} of {n})")
            material, from_notes = "\n\n".join(notes), True
        for _ in range(3):  # notes on notes for very long meetings; each round is much shorter than the last
            if len(material) <= budget:
                break
            parts = split_lines(material, budget)
            notes, shown = [], []
            take_notes(parts, "Summarizing part {i} of {n}")
            material, from_notes = "\n\n".join(notes), True
        material = material[:budget]  # only reached if the notes somehow didn't shrink
        if on_progress and from_notes:
            on_progress("Writing up the notes")
        system, prompt = templates.final_prompt(template, cfg["custom_template"], material, from_notes, moments, about, minutes,
                                                mine)
        show = (lambda t: on_draft(tidy(t))) if on_draft else None
        summary = tidy(generate(pid, cfg, prompt, system=system, keep_alive=hold, on_text=show))
        if not _ACTION_HEADING.search(summary):  # small models sometimes forget the one section we rely on
            system, prompt = templates.actions_prompt(material, from_notes, about)
            show = (lambda t, before=summary: on_draft(f"{before}\n\n{tidy(t)}")) if on_draft else None
            actions = tidy(generate(pid, cfg, prompt, system=system, keep_alive=hold, on_text=show))
            summary += "\n\n" + (actions if _ACTION_HEADING.search(actions) else "## Action Items\n- None")
        summary = place_moments(summary, moments)
        summary = keep_mine(summary, mine)
        if not want_title:
            unload(pid, cfg)
        title = None
        if want_title:
            try:
                title = clean_title(generate(pid, cfg, TITLE_PROMPT.format(summary=summary), keep_alive=OLLAMA_KEEP_ALIVE))
            except LLMError:
                pass
        return summary, title

    (summary, title), used = run_chain(choice, cfg, run)
    return summary, used, title


ASK_SYSTEM = """You answer questions about the user's own meetings, using only the material provided.
Be direct and concise. If the material doesn't contain the answer, say so plainly instead of guessing.
When you rely on a specific moment, cite its timestamp like [12:34]. {extra}"""


def ask(question: str, context: str, cfg: dict, cite_meetings: bool = False) -> tuple[str, str]:
    extra = "When you use a meeting, cite it by its tag, like [M12]." if cite_meetings else ""
    prompt = f"{context}\n\nQUESTION: {question}"
    return run_chain("auto", cfg, lambda pid: generate(pid, cfg, prompt, system=ASK_SYSTEM.format(extra=extra)))
