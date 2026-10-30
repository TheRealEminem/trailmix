"""Trailmix transcription worker: an OpenAI-compatible transcription endpoint backed by this computer's own speech
engine (local_engine.py: MLX on an Apple silicon Mac, whisper.cpp on an Intel Mac, Windows and Linux).

Run it on the computer with the most speed to spare, often one with a GPU (./trailmix worker on a Mac or Linux,
.\\trailmix.ps1 worker on Windows, or `python -m transcription_worker` in this folder anywhere), and point a
server-hosted Trailmix at it: Settings → Transcription → Your own server, URL http://<that-computer>:8770/v1.
Anything else that speaks the OpenAI audio API can use it too. (mac_worker.py is the same thing under its old name.)

Models load in a child process that is shut down after TRAILMIX_WORKER_IDLE_S seconds without requests, so the
computer gets all of its memory back between meetings.

  POST /v1/audio/transcriptions   multipart: file, model, response_format (json|verbose_json|text), language
  GET  /v1/models
  GET  /health                    {"ok": true, "engine": "MLX" | "whisper.cpp" | null}

TRAILMIX_WORKER_TOKEN (a bearer token callers must send), TRAILMIX_WORKER_HOST (0.0.0.0) and TRAILMIX_WORKER_PORT
(8770) configure it; TRAILMIX_WHISPER_MODEL picks the accurate model, as everywhere else.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

import local_engine

TOKEN = os.getenv("TRAILMIX_WORKER_TOKEN", "")
IDLE_S = float(os.getenv("TRAILMIX_WORKER_IDLE_S", "120"))
ALIASES = {
    "large": local_engine.automatic_model(),
    "whisper-1": local_engine.automatic_model(),
    "live": local_engine.LIVE_MODEL,
    "small": local_engine.LIVE_MODEL,
}

app = FastAPI(title="Trailmix transcription worker")
_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_last_used = time.monotonic()


def ffmpeg() -> str | None:
    """ffmpeg on the PATH, else the one in the imageio-ffmpeg package (how trailmix.ps1 gets one on Windows)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _job(audio: np.ndarray, model: str, language: str | None) -> list[dict]:
    """Runs inside the child process; the model stays cached there until the child is retired."""
    if language:
        local_engine.LANGUAGE = language
    return local_engine.run(audio, model)


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=1)
        return _pool


async def _retire_idle():
    global _pool
    while True:
        await asyncio.sleep(15)
        with _pool_lock:
            if _pool is not None and time.monotonic() - _last_used > IDLE_S:
                _pool.shutdown(wait=False, cancel_futures=True)  # child exits -> model memory is freed
                _pool = None


@app.on_event("startup")
async def _start():
    asyncio.create_task(_retire_idle())


@app.middleware("http")
async def _auth(request: Request, call_next):
    if TOKEN and request.headers.get("authorization", "") != f"Bearer {TOKEN}":
        return JSONResponse({"error": {"message": "Invalid or missing bearer token"}}, status_code=401)
    return await call_next(request)


def _decode(data: bytes) -> np.ndarray:
    exe = ffmpeg()
    if exe is None:
        raise HTTPException(500, "The transcription worker needs ffmpeg, and it isn't installed")
    proc = subprocess.run(
        [exe, "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        input=data, capture_output=True,
    )
    if proc.returncode != 0:
        raise HTTPException(400, f"Could not decode audio: {proc.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


@app.get("/health")
def health():
    return {"ok": local_engine.available(), "engine": local_engine.NAME}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": k, "object": "model", "owned_by": "trailmix"} for k in ALIASES]}


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form("large"),
    response_format: str = Form("json"),
    language: str | None = Form(None),
):
    global _last_used
    if not local_engine.available():
        raise HTTPException(503, "No speech engine is installed on the transcription worker")
    audio = _decode(await file.read())
    _last_used = time.monotonic()
    loop = asyncio.get_running_loop()
    segs = await loop.run_in_executor(_get_pool(), _job, audio, ALIASES.get(model, model), language)
    _last_used = time.monotonic()
    text = " ".join(s["text"] for s in segs).strip()
    if response_format == "text":
        return PlainTextResponse(text)
    if response_format == "verbose_json":
        return {
            "task": "transcribe",
            "duration": len(audio) / 16000,
            "text": text,
            "segments": [{"id": i, **s} for i, s in enumerate(segs)],
        }
    return {"text": text}


def main() -> int:
    """`python -m transcription_worker`: checks what it needs, then serves until Ctrl+C."""
    import uvicorn

    if not local_engine.available():
        print("No speech engine is installed. On an Apple silicon Mac: pip install -r requirements.txt; "
              "anywhere else: pip install -r requirements-whispercpp.txt", file=sys.stderr)
        return 1
    if ffmpeg() is None:
        print("The transcription worker needs ffmpeg: install it, or pip install imageio-ffmpeg", file=sys.stderr)
        return 1
    host = os.getenv("TRAILMIX_WORKER_HOST", "0.0.0.0")
    port = int(os.getenv("TRAILMIX_WORKER_PORT", "8770"))
    print(f"Trailmix transcription worker ({local_engine.NAME}) on port {port}. Ctrl+C stops it.")
    if not TOKEN:
        print("Tip: set TRAILMIX_WORKER_TOKEN so only your server can use it.")
    uvicorn.run("transcription_worker:app", host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
