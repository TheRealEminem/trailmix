"""Transcription through any OpenAI-compatible endpoint (POST {base}/audio/transcriptions).

That covers OpenAI and Groq in the cloud, self-hosted servers (speaches, whisper.cpp), and
Trailmix's own Mac worker (mac_worker.py) - which is how a server install borrows your Mac's GPU.
"""
import io
import wave
from urllib.parse import urlparse

import httpx
import numpy as np

SR = 16000


class RemoteError(Exception):
    pass


def _unreachable(base_url: str, error: httpx.HTTPError) -> RemoteError:
    """Says in plain words why an endpoint can't be reached, instead of the raw socket error."""
    host = urlparse(base_url).netloc or base_url
    detail = str(error)
    if isinstance(error, httpx.TimeoutException):
        why = f"{host} didn't answer in time"
    elif "nodename nor servname" in detail or "Name or service not known" in detail or "getaddrinfo" in detail:
        why = f"there's no computer called \u201c{host.split(':')[0]}\u201d on this network"
    elif "refused" in detail.lower():
        why = f"nothing is answering at {host}"
    else:
        why = detail or type(error).__name__
    return RemoteError(f"Couldn't reach the transcription endpoint: {why}. Check Settings \u2192 Transcription.")


def to_wav(audio: np.ndarray) -> bytes:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(audio: np.ndarray, base_url: str, api_key: str, model: str, language: str | None,
               timeout: float = 300) -> list[dict]:
    """Returns [{start, end, text}] relative to the start of `audio`."""
    if not base_url:
        raise RemoteError("No transcription endpoint is set (Settings → Transcription)")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    wav = to_wav(audio)
    duration = len(audio) / SR

    def call(fmt: str) -> httpx.Response:
        data = {"model": model, "response_format": fmt}
        if language:
            data["language"] = language
        try:
            return httpx.post(f"{base_url}/audio/transcriptions", headers=headers, data=data,
                              files={"file": ("audio.wav", wav, "audio/wav")}, timeout=timeout)
        except httpx.HTTPError as e:
            raise _unreachable(base_url, e) from e

    r = call("verbose_json")
    if r.status_code == 400 and "response_format" in r.text:  # e.g. gpt-4o-transcribe only does plain json
        r = call("json")
    if r.status_code >= 400:
        raise RemoteError(f"Transcription endpoint returned {r.status_code}: {r.text[:300]}")
    body = r.json()
    segs = [
        {"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
        for s in body.get("segments") or []
        if s.get("text", "").strip()
    ]
    if not segs and body.get("text", "").strip():
        segs = [{"start": 0.0, "end": duration, "text": body["text"].strip()}]
    return segs


def check(base_url: str, api_key: str, model: str) -> str:
    """Sends a short silent clip to prove the endpoint and key work."""
    transcribe(np.zeros(SR, dtype=np.float32), base_url, api_key, model, None, timeout=60)
    return f"{base_url} accepted a test clip with model {model}"
