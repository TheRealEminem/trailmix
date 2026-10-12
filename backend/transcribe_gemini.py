"""Transcription with speaker labels through Google's gemini-3.5-transcribe (batch, not streaming).

An audio file is uploaded through the Files API, then transcribed with word timestamps and speaker
diarization. The model takes at most 30 minutes per request when diarizing, so longer audio is cut into
overlapping chunks and the speaker labels of each chunk are matched to the previous one by what was said in
the overlap (a chunk's "spk_1" isn't necessarily the last chunk's "spk_1").

Returns the same [{start, end, speaker, text}] segments as the other engines, with speakers named
"Speaker 1", "Speaker 2"... in order of first appearance.
"""
import re
import subprocess
import time
from collections import defaultdict
from urllib.parse import urlparse

import httpx
import numpy as np

SR = 16000
API = "https://generativelanguage.googleapis.com"
MODEL = "gemini-3.5-transcribe"
MAX_CHUNK_S = 25 * 60       # the model's limit with diarization is 30 minutes; leave headroom
OVERLAP_S = 60              # chunks share this much audio, to match their speaker labels
CUT_SEARCH_S = 120          # a chunk ends at the quietest moment in the last two minutes before its limit
SEGMENT_GAP_S = 1.0         # a longer pause starts a new line
SEGMENT_MAX_S = 25.0        # ...and so does a line this long
MIN_MATCH_S = 2.0           # seconds of shared speech needed to call two labels the same person


class GeminiError(Exception):
    pass


def _headers(api_key: str) -> dict:
    return {"x-goog-api-key": api_key}


RATE_LIMIT_RETRIES = 8      # Google's free tier allows few audio tokens a minute; waiting its turn is normal
MAX_WAIT_S = 90
_RETRY_IN = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.I)


def _wait_for(r: httpx.Response, attempt: int) -> float:
    """How long to wait before trying again: what Google asked for ("Please retry in 8s"), else a growing pause."""
    asked = _RETRY_IN.search(r.text)
    return min(MAX_WAIT_S, (float(asked.group(1)) + 2) if asked else 2 ** attempt * 2)


def _request(method: str, url: str, api_key: str, retries: int = 3, **kw) -> httpx.Response:
    """One call, retried on rate limits (patiently) and server errors; failures become GeminiError."""
    kw.setdefault("timeout", 600)
    kw["headers"] = {**_headers(api_key), **kw.get("headers", {})}
    attempt = 0
    while True:
        try:
            r = httpx.request(method, url, **kw)
        except httpx.HTTPError as e:
            if attempt >= retries:
                raise GeminiError(f"Couldn't reach Google ({urlparse(url).netloc}): {e or type(e).__name__}") from e
            time.sleep(2 ** attempt)
            attempt += 1
            continue
        limit = RATE_LIMIT_RETRIES if r.status_code == 429 and retries else retries
        if r.status_code in (429, 500, 502, 503, 504) and attempt < limit:
            time.sleep(_wait_for(r, attempt))
            attempt += 1
            continue
        if r.status_code in (401, 403) or (r.status_code == 400 and "API_KEY_INVALID" in r.text):
            raise GeminiError("Google didn't accept the API key (Settings → Transcription)")
        if r.status_code >= 400:
            raise GeminiError("Google is limiting how much audio this key can transcribe per minute (free keys "
                              "allow little; Google's rate-limit page explains upgrading the tier)" if r.status_code == 429
                              else f"Google returned {r.status_code}: {r.text[:300]}")
        return r


# ── Files API ───────────────────────────────────────────────────────────

def upload(data: bytes, mime: str, api_key: str, name: str = "audio") -> dict:
    """Uploads audio and waits until it's ready. Returns the file ({uri, name, ...})."""
    start = _request(
        "POST", f"{API}/upload/v1beta/files", api_key, json={"file": {"display_name": name}},
        headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(len(data)), "X-Goog-Upload-Header-Content-Type": mime},
    )
    target = start.headers.get("x-goog-upload-url")
    if not target:
        raise GeminiError("Google didn't give an upload address")
    done = _request("POST", target, api_key, content=data,
                    headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                             "Content-Length": str(len(data))})
    file = done.json().get("file") or {}
    for _ in range(60):  # audio is usually ready at once; wait if it's still being processed
        if file.get("state", "ACTIVE") == "ACTIVE":
            return file
        if file.get("state") == "FAILED":
            raise GeminiError("Google couldn't process the uploaded audio")
        time.sleep(2)
        file = _request("GET", f"{API}/v1beta/{file['name']}", api_key).json()
    raise GeminiError("Google took too long to process the uploaded audio")


def delete_file(file: dict, api_key: str) -> None:
    """The audio shouldn't sit on Google's servers any longer than it takes to transcribe it."""
    try:
        _request("DELETE", f"{API}/v1beta/{file['name']}", api_key, retries=0, timeout=30)
    except GeminiError:
        pass  # Google removes uploads itself after 48 hours


# ── Transcription request and response ──────────────────────────────────

def _config(language: str | None, diarize: bool) -> dict:
    mode = {"type": "verbatim", "timestamp_granularities": ["word"]}
    if diarize:
        mode["diarization_mode"] = "speaker"
    cfg = {"mode": mode, "language_codes": [language] if language else []}
    return {"transcription_config": cfg}


def _offset(value) -> float:
    """"0.450s" -> 0.45"""
    return float(str(value).rstrip("s") or 0)


def parse_words(body) -> list[dict]:
    """[{text, speaker, start, end}] from a response. Walks the whole document for `word_info` annotations,
    so it doesn't depend on how the steps are nested."""
    words = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "word_info" and "text" in node:
                words.append({"text": str(node["text"]).strip(), "speaker": node.get("speaker") or "spk_1",
                              "start": _offset(node.get("start_offset", 0)), "end": _offset(node.get("end_offset", 0))})
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(body)
    words = [w for w in words if w["text"]]
    words.sort(key=lambda w: w["start"])
    return words


def call(file: dict, mime: str, api_key: str, language: str | None, diarize: bool, model: str = MODEL) -> list[dict]:
    body = {"model": model, "input": [{"type": "audio", "uri": file["uri"], "mime_type": mime}],
            "generation_config": _config(language, diarize)}
    r = _request("POST", f"{API}/v1beta/interactions", api_key, json=body)
    return parse_words(r.json())


def group_words(words: list[dict]) -> list[dict]:
    """Words -> lines: a new line at each change of speaker, pause, or when a line gets long."""
    segs: list[dict] = []
    for w in words:
        last = segs[-1] if segs else None
        if (last and last["speaker"] == w["speaker"] and w["start"] - last["end"] <= SEGMENT_GAP_S
                and w["end"] - last["start"] <= SEGMENT_MAX_S):
            last["text"] += " " + w["text"]
            last["end"] = max(last["end"], w["end"])
        else:
            segs.append({"start": w["start"], "end": w["end"], "speaker": w["speaker"], "text": w["text"]})
    return segs


# ── Long audio: overlapping chunks with matched speaker labels ──────────

def plan_chunks(audio: np.ndarray, max_s: float = MAX_CHUNK_S, overlap_s: float = OVERLAP_S) -> list[tuple[float, float]]:
    """[(start, end)] seconds. One chunk if it fits; otherwise each ends at a quiet moment and the next one
    starts `overlap_s` earlier."""
    total = len(audio) / SR
    if total <= max_s:
        return [(0.0, total)]
    chunks, start = [], 0.0
    while total - start > max_s:
        lo = int((start + max_s - CUT_SEARCH_S) * SR)
        hi = int((start + max_s) * SR)
        step = SR // 2  # half-second windows
        window = range(lo, hi - step, step)
        quietest = min(window, key=lambda a: float(np.sqrt(np.mean(np.square(audio[a:a + step])))))
        cut = (quietest + step // 2) / SR
        chunks.append((start, cut))
        start = cut - overlap_s
    chunks.append((start, total))
    return chunks


def match_speakers(previous: list[dict], current: list[dict], lo: float, hi: float) -> dict[str, str]:
    """Which of `current`'s speaker labels is which of `previous`'s, judged by who is talking at the same
    time in the shared stretch [lo, hi] (both word lists use the same clock). {current label: previous label}
    for labels matched with at least MIN_MATCH_S of shared speech; each previous label is used once."""
    overlap = defaultdict(float)
    prev_words = [w for w in previous if lo <= w["start"] <= hi]
    for w in (w for w in current if lo <= w["start"] <= hi):
        for p in prev_words:
            shared = min(w["end"], p["end"]) - max(w["start"], p["start"])
            if shared > 0:
                overlap[(w["speaker"], p["speaker"])] += shared
    mapping, taken = {}, set()
    for (cur, prev), seconds in sorted(overlap.items(), key=lambda kv: -kv[1]):
        if seconds >= MIN_MATCH_S and cur not in mapping and prev not in taken:
            mapping[cur] = prev
            taken.add(prev)
    return mapping


def stitch(chunk_words: list[tuple[tuple[float, float], list[dict]]]) -> list[dict]:
    """Joins per-chunk words (times relative to each chunk) into one list on the meeting's clock with
    consistent speaker labels. Each chunk's words are used from where the previous chunk ended."""
    out: list[dict] = []
    prev_end = 0.0
    prev_words: list[dict] = []
    fresh = 0
    for i, ((start, end), words) in enumerate(chunk_words):
        shifted = [{**w, "start": w["start"] + start, "end": w["end"] + start} for w in words]
        labels = {w["speaker"] for w in shifted}
        if i == 0:
            rename = {label: label for label in labels}
        else:
            matched = match_speakers(prev_words, shifted, start, prev_end)
            rename = {}
            for label in sorted(labels):
                if label in matched:
                    rename[label] = matched[label]
                else:  # someone who hadn't spoken yet (or couldn't be matched): a new person
                    fresh += 1
                    rename[label] = f"new_{fresh}"
        shifted = [{**w, "speaker": rename[w["speaker"]]} for w in shifted]
        out += [w for w in shifted if w["start"] >= prev_end or i == 0]
        prev_words, prev_end = shifted, end
    return out


def name_speakers(segments: list[dict]) -> list[dict]:
    """Speakers become "Speaker 1", "Speaker 2"... in order of first appearance."""
    names: dict[str, str] = {}
    for s in segments:
        names.setdefault(s["speaker"], f"Speaker {len(names) + 1}")
    return [{**s, "speaker": names[s["speaker"]]} for s in segments]


def encode(audio: np.ndarray) -> bytes:
    """Opus in an Ogg container, small enough to upload quickly (about 20 MB per hour)."""
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "-"],
        input=pcm, capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise GeminiError(f"Couldn't prepare the audio for upload: {proc.stderr.decode()[:200]}")
    return proc.stdout


def transcribe(audio: np.ndarray, api_key: str, language: str | None = None, diarize: bool = True,
               on_progress=None, model: str = MODEL) -> list[dict]:
    """[{start, end, speaker, text}] for the whole of `audio` (16 kHz mono float). `diarize` False still
    gives timestamps but no speaker labels (every word is one speaker). `on_progress(i, n)` after each chunk."""
    import settings

    if settings.locked():
        raise GeminiError("Lockdown mode is on, so recordings can't be sent to Google (Settings → Privacy)")
    if not api_key:
        raise GeminiError("No Google API key is set (Settings → Transcription)")
    chunks = plan_chunks(audio)
    per_chunk = []
    for i, (a, b) in enumerate(chunks):
        file = upload(encode(audio[int(a * SR):int(b * SR)]), "audio/ogg", api_key, name=f"chunk-{i + 1}")
        try:
            per_chunk.append(((a, b), call(file, "audio/ogg", api_key, language, diarize, model)))
        finally:
            delete_file(file, api_key)
        if on_progress:
            on_progress(i + 1, len(chunks))
    words = stitch(per_chunk)
    return name_speakers(group_words(words))


def check(api_key: str) -> str:
    """Proves the key works and the transcription model is available, with a one-second silent clip."""
    if not api_key:
        raise GeminiError("No Google API key is set")
    file = upload(encode(np.zeros(SR, dtype=np.float32)), "audio/ogg", api_key, name="check")
    try:
        call(file, "audio/ogg", api_key, None, True)
    finally:
        delete_file(file, api_key)
    return f"Google accepted a test clip with {MODEL}"


if __name__ == "__main__":  # python transcribe_gemini.py meeting.ogg [--no-diarize]: try it on a file
    import json
    import os
    import sys

    path = sys.argv[1]
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    started = time.monotonic()
    result = transcribe(samples, os.environ["GEMINI_API_KEY"], diarize="--no-diarize" not in sys.argv,
                        on_progress=lambda i, n: print(f"chunk {i}/{n} done", file=sys.stderr))
    print(json.dumps(result, indent=1))
    print(f"{len(samples) / SR / 60:.1f} min of audio in {time.monotonic() - started:.0f} s", file=sys.stderr)
