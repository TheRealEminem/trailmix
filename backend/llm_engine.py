"""Text generation across providers: local Ollama, or any cloud API you have a key for.

Every provider is reduced to one call, generate(provider, cfg, prompt, system) -> text, so summaries,
titles and Q&A work the same whichever one you pick. `cfg` is settings.get_all().
"""
import os
import re
from urllib.parse import urlparse

import httpx

import templates

OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "0")  # "0" = unload right after the last call
TIMEOUT = httpx.Timeout(600, connect=8)
MAX_TOKENS = 16000

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


# ── Provider facts ──────────────────────────────────────────────────────

def label(pid: str, cfg: dict) -> str:
    return (cfg.get("custom_name") or "OpenAI-compatible") if pid == "custom" else PROVIDERS[pid]["label"]


def is_local(pid: str, cfg: dict) -> bool:
    """True when the model would run on this machine (so RAM gates and the recording pause apply)."""
    if pid != "ollama":
        return False
    return (urlparse(cfg["ollama_url"]).hostname or "") in ("localhost", "127.0.0.1", "::1")


def configured(pid: str, cfg: dict) -> tuple[bool, str]:
    """(ready, reason-if-not). Cheap: never makes a network call except for Ollama's tag list."""
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
        tags = _ollama_tags(cfg)
        return tags[0]["name"] if tags else ""
    return cfg[f"{pid}_model"]


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
    chosen = next((m for m in tags if m["name"] == cfg["ollama_model"]), tags[0] if tags else None)
    return chosen["size"] / 1024**3 if chosen else None


def ollama_ram_needed_gb(cfg: dict) -> float:
    """Estimated RAM to load the chosen model plus its context window."""
    return (ollama_model_size_gb(cfg) or 0) * 1.1 + 1.5


def _ollama(cfg: dict, prompt: str, system: str | None, keep_alive: str) -> str:
    model = model_for("ollama", cfg)
    if not model:
        raise LLMError(f"Ollama isn't reachable at {cfg['ollama_url']} (or has no models installed)")
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_ctx": 16384, "temperature": 0.2},
    }
    if system:
        body["system"] = system
    r = _post(f"{cfg['ollama_url']}/api/generate", json=body)
    return r.json().get("response") or ""


# ── Cloud providers ─────────────────────────────────────────────────────

def _post(url: str, **kw) -> httpx.Response:
    try:
        r = httpx.post(url, timeout=TIMEOUT, **kw)
    except httpx.HTTPError as e:
        raise LLMError(f"Couldn't reach {urlparse(url).netloc}: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"{urlparse(url).netloc} returned {r.status_code}: {_error_text(r)}")
    return r


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
        err = data.get("error", data)
        return str(err.get("message") if isinstance(err, dict) else err)[:300]
    except ValueError:
        return r.text[:300]


def _anthropic(cfg: dict, prompt: str, system: str | None) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"], timeout=600, max_retries=2)
    model = cfg["anthropic_model"]
    params = {"model": model, "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": prompt}]}
    if system:
        params["system"] = system
    try:
        if model in _CLAUDE_FALLBACK_MODELS:
            # If a safety classifier declines, the API re-runs the request on a suitable model.
            msg = client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
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


def _openai_compatible(base: str, key: str, model: str, prompt: str, system: str | None, pid: str) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body: dict = {"model": model, "messages": messages}
    if pid == "openai":
        body["max_completion_tokens"] = MAX_TOKENS  # newer OpenAI models reject max_tokens / custom temperature
    elif pid == "deepseek":
        body["max_tokens"] = 8000
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    r = _post(f"{base}/chat/completions", json=body, headers=headers)
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError(f"Unexpected response from {base}") from e


def _gemini(cfg: dict, prompt: str, system: str | None) -> str:
    body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    r = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['gemini_model']}:generateContent",
        json=body,
        headers={"x-goog-api-key": cfg["gemini_api_key"]},
    )
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError("Gemini returned no text (it may have blocked the request)") from e
    return "".join(p.get("text", "") for p in parts)


def generate(pid: str, cfg: dict, prompt: str, system: str | None = None, keep_alive: str | None = None) -> str:
    ok, why = configured(pid, cfg)
    if not ok:
        raise LLMError(why)
    if pid == "ollama":
        text = _ollama(cfg, prompt, system, keep_alive or OLLAMA_KEEP_ALIVE)
    elif pid == "anthropic":
        text = _anthropic(cfg, prompt, system)
    elif pid == "gemini":
        text = _gemini(cfg, prompt, system)
    elif pid == "custom":
        text = _openai_compatible(cfg["custom_base_url"], cfg["custom_api_key"], cfg["custom_model"], prompt, system, pid)
    else:
        text = _openai_compatible(PROVIDERS[pid]["base"], cfg[f"{pid}_api_key"], cfg[f"{pid}_model"], prompt, system, pid)
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
        return [m["name"] for m in tags]
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


def summarize(transcript: str, cfg: dict, choice: str, template: str, moments: list[str],
              want_title: bool) -> tuple[str, str, str | None]:
    """Returns (summary, provider_used, title). The title is best-effort and never fails the summary."""
    if not transcript.strip():
        raise LLMError("Transcript is empty (no speech detected)")
    prompt = templates.build_prompt(template, cfg["custom_template"], transcript, moments)

    def run(pid: str):
        # Keep a local Ollama model loaded between the summary and title calls, then let it unload.
        summary = generate(pid, cfg, prompt, keep_alive="5m" if want_title else OLLAMA_KEEP_ALIVE)
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
