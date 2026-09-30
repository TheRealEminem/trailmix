"""Trailmix Mac worker: an OpenAI-compatible transcription endpoint backed by mlx-whisper.

Run it on your Apple Silicon Mac (./trailmix worker) and point a server-hosted Trailmix at it:
Settings → Transcription → Remote, URL http://<your-mac>:8770/v1. Anything else that speaks the
OpenAI audio API can use it too.

Models load in a child process that is shut down after TRAILMIX_WORKER_IDLE_S seconds without
requests, so the Mac gets all of its memory back between meetings.

  POST /v1/audio/transcriptions   multipart: file, model, response_format (json|verbose_json|text), language
  GET  /v1/models
"""
import asyncio
import os
import subprocess
import threading
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

import mlx_engine

TOKEN = os.getenv("TRAILMIX_WORKER_TOKEN", "")
IDLE_S = float(os.getenv("TRAILMIX_WORKER_IDLE_S", "120"))
ALIASES = {
    "large": mlx_engine.FINAL_MODEL,
    "whisper-1": mlx_engine.FINAL_MODEL,
    "live": mlx_engine.LIVE_MODEL,
    "small": mlx_engine.LIVE_MODEL,
}

app = FastAPI(title="Trailmix Mac worker")
_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_last_used = time.monotonic()


def _job(audio: np.ndarray, model: str, language: str | None) -> list[dict]:
    """Runs inside the child process; the model stays cached there until the child is retired."""
    if language:
        mlx_engine.LANGUAGE = language
    return mlx_engine.run(audio, model)


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
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        input=data, capture_output=True,
    )
    if proc.returncode != 0:
        raise HTTPException(400, f"Could not decode audio: {proc.stderr.decode()[:200]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


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
