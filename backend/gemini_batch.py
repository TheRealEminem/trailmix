"""Google's Batch API for text jobs (generateContent): half the price of asking one at a time, in exchange for the
answers coming back later (the target is within 24 hours; small jobs often take a minute or two).

The jobs are sent inline (up to 20 MB of requests at a time) and polled for. Used for work nobody is waiting for,
like rewriting the notes of old meetings with a better model.
"""
import json

import httpx

API = "https://generativelanguage.googleapis.com/v1beta"
INLINE_LIMIT_BYTES = 18 * 1024 * 1024   # Google's limit is 20 MB per job; leave room for the envelope

_STATES = {"PENDING": "pending", "RUNNING": "running", "SUCCEEDED": "succeeded", "FAILED": "failed",
           "CANCELLED": "cancelled", "EXPIRED": "expired"}


class BatchError(Exception):
    pass


def _call(method: str, path: str, api_key: str, **kw) -> dict:
    try:
        r = httpx.request(method, f"{API}/{path}", headers={"x-goog-api-key": api_key}, timeout=kw.pop("timeout", 120), **kw)
    except httpx.HTTPError as e:
        raise BatchError(f"Couldn't reach Google: {e or type(e).__name__}") from e
    if r.status_code in (401, 403) or (r.status_code == 400 and "API_KEY_INVALID" in r.text):
        raise BatchError("Google didn't accept the API key")
    if r.status_code >= 400:
        raise BatchError(f"Google returned {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def request_body(system: str | None, prompt: str) -> dict:
    body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body


def _body(r: dict) -> dict:
    """The generateContent request: `body` as given (audio and the like), or a text one from system and prompt."""
    return r["body"] if r.get("body") else request_body(r.get("system"), r["prompt"])


def size(requests: list[dict]) -> int:
    return sum(len(json.dumps(_body(r)).encode()) + 200 for r in requests)


def split(requests: list[dict]) -> list[list[dict]]:
    """Requests in groups small enough to send as one job each."""
    groups, current, used = [], [], 0
    for r in requests:
        n = size([r])
        if current and used + n > INLINE_LIMIT_BYTES:
            groups.append(current)
            current, used = [], 0
        current.append(r)
        used += n
    return groups + ([current] if current else [])


def create(model: str, requests: list[dict], api_key: str, name: str = "trailmix") -> str:
    """Submits [{key, system, prompt}] (or {key, body}) as one job; returns its name ("batches/...")."""
    body = {"batch": {"display_name": name, "input_config": {"requests": {"requests": [
        {"request": _body(r), "metadata": {"key": r["key"]}} for r in requests]}}}}
    out = _call("POST", f"models/{model}:batchGenerateContent", api_key, json=body)
    if not out.get("name"):
        raise BatchError("Google didn't give the job a name")
    return out["name"]


def status(name: str, api_key: str) -> dict:
    """{state: pending|running|succeeded|failed|cancelled|expired, requests, succeeded, failed, results}. `results`
    ({key: {text, error, usage}}) is there once it has succeeded."""
    j = _call("GET", name, api_key)
    meta = j.get("metadata") or {}
    raw = (meta.get("state") or j.get("state") or "").rsplit("_", 1)[-1]
    state = _STATES.get(raw, "pending")
    stats = meta.get("batchStats") or {}
    out = {"state": state, "requests": int(stats.get("requestCount", 0)), "succeeded": int(stats.get("successfulRequestCount", 0)),
           "failed": int(stats.get("failedRequestCount", 0)), "results": None}
    if state == "succeeded":
        out["results"] = parse_results(j)
    if state == "failed":
        out["error"] = str((j.get("error") or {}).get("message") or "Google says the job failed")
    return out


def parse_results(job: dict) -> dict[str, dict]:
    """{key: {text, error, usage}} from a finished job. The answers sit in a different place in different
    versions of the API's replies, so look in each."""
    holders = [job.get("response") or {}, (job.get("metadata") or {}).get("output") or {}, job.get("dest") or {}]
    items = next((h["inlinedResponses"]["inlinedResponses"] if isinstance(h.get("inlinedResponses"), dict)
                  else h["inlinedResponses"] for h in holders if h.get("inlinedResponses")), [])
    out = {}
    for item in items:
        key = (item.get("metadata") or {}).get("key", "")
        if item.get("error"):
            out[key] = {"text": None, "error": str(item["error"].get("message") or item["error"]), "usage": {}}
            continue
        resp = item.get("response") or {}
        try:
            parts = resp["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            out[key] = {"text": text, "error": None if text.strip() else "Gemini returned no text", "usage": resp.get("usageMetadata") or {}}
        except (KeyError, IndexError):
            out[key] = {"text": None, "error": "Gemini returned no text (it may have blocked the request)", "usage": {}}
    return out


def cancel(name: str, api_key: str) -> None:
    _call("POST", f"{name}:cancel", api_key, json={})
