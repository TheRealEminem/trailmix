"""Trailmix API: stream meeting audio, draft a live transcript, then transcribe + summarize.

Runs on your Mac by default (everything local). The same app can run on a server: point transcription
and summaries at remote endpoints in Settings and set TRAILMIX_ACCESS_TOKEN (see docs/self-hosting.md).
If frontend/dist exists, the UI is served from here too, so one process is the whole app.
"""
import asyncio
import json
import logging
import os
import signal
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import audio_store as store
import auth
import database as db
import exporter
import live
import llm_engine
import meeting_text
import mlx_engine
import models
import pipeline
import resources
import settings
import templates
import transcribe_remote

log = logging.getLogger("trailmix")
SWEEP_INTERVAL_S = 6 * 3600
UI_DIR = Path(os.getenv("TRAILMIX_UI_DIR", Path(__file__).resolve().parent.parent / "frontend" / "dist"))


async def _sweep_loop():
    while True:
        await run_in_threadpool(store.sweep_expired)
        await asyncio.sleep(SWEEP_INTERVAL_S)


def _exit_with_parent() -> None:
    """Trailmix.app starts this server and names itself in TRAILMIX_PARENT_PID. If the app is force-quit
    or crashes, this process is re-parented to launchd; stop instead of lingering on the port."""
    parent = int(os.getenv("TRAILMIX_PARENT_PID") or 0)
    if not parent:
        return

    def watch():
        while os.getppid() == parent:
            time.sleep(2)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _exit_with_parent()
    db.init_db()
    models.prefetch()
    pipeline.recover_unfinished()
    sweeper = asyncio.create_task(_sweep_loop())
    yield
    sweeper.cancel()


app = FastAPI(title="Trailmix", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in auth.OPEN_PATHS and not auth.is_authorized(request):
        return JSONResponse({"detail": "Sign in required"}, status_code=401)
    return await call_next(request)


# ── Models ─────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    title: str = ""


class MeetingPatch(BaseModel):
    title: str


class SettingsPatch(BaseModel):
    model_config = {"extra": "allow"}  # validated key-by-key in settings.update


class SummarizeRequest(BaseModel):
    provider: str = "auto"      # auto | a provider id
    template: str | None = None


class LoginRequest(BaseModel):
    token: str


class Question(BaseModel):
    question: str


class Bookmark(BaseModel):
    t: float
    note: str = ""


class MarkRequest(BaseModel):
    note: str = ""


class BookmarksPut(BaseModel):
    bookmarks: list[Bookmark]


class TaskPatch(BaseModel):
    done: bool


# ── Helpers ────────────────────────────────────────────────────────────

def _get_or_404(meeting_id: int) -> dict:
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    return meeting


def _present(m: dict) -> dict:
    """Public shape of a meeting: hides file paths, adds parsed fields and audio info."""
    hidden = ("audio_path", "audio_dir", "segments_json", "requested_provider", "draft_json", "exported_paths",
              "bookmarks_json", "speaker_names_json", "qa_json")
    out = {k: v for k, v in m.items() if k not in hidden}
    cfg = settings.get_all()
    out["segments"] = meeting_text.segments(m) or None
    out["draft"] = json.loads(m["draft_json"]) if m.get("draft_json") else None
    out["exported_paths"] = json.loads(m["exported_paths"]) if m.get("exported_paths") else []
    out["bookmarks"] = meeting_text.bookmarks(m)
    out["speaker_names"] = meeting_text.speaker_names(m, cfg)
    out["qa"] = json.loads(m["qa_json"]) if m.get("qa_json") else []
    out["tasks"] = db.tasks_for(m["id"])
    out["title_auto"] = bool(m["title_auto"])
    out["audio_bytes"] = store.audio_bytes(m)
    out["audio_expires_at"] = store.expires_at(m)
    out["audio_deleted"] = bool(m["audio_deleted"])
    out["has_system"] = bool(m["has_system"])
    return out


def _reexport(meeting_id: int) -> None:
    """Keep an already-exported file in step with edits (title, names, ticked tasks)."""
    meeting = db.get_meeting(meeting_id)
    cfg = settings.get_all()
    if not (cfg["auto_export"] and meeting and meeting["exported_paths"] and meeting["transcript"]):
        return
    try:
        paths = exporter.export_meeting(meeting, cfg)
        db.update_meeting(meeting_id, exported_paths=json.dumps(paths), export_error=None)
    except OSError as e:
        db.update_meeting(meeting_id, export_error=f"Export failed: {e}")


def _local_llm_paused(cfg: dict) -> None:
    """Don't load a local model on this machine in the middle of a meeting."""
    first = llm_engine.chain("auto", cfg)[0]
    if live.active_count() and llm_engine.is_local(first, cfg):
        raise HTTPException(409, "Your local model is paused while you're recording. Ask again after you stop, "
                                 "or pick a cloud provider in Settings.")


def _meeting_context(m: dict, cfg: dict, question: str) -> str:
    """The meeting's notes and transcript, cut to what the model can take: for a long meeting and a small
    local model, the parts of the transcript that match the question."""
    when = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).astimezone().strftime("%A %B %-d, %Y")
    parts = [f"MEETING: {m['title']} ({when})"]
    if m.get("summary"):
        parts.append(f"NOTES:\n{m['summary']}")
    room = llm_engine.chain_context_chars(cfg) - sum(len(p) for p in parts) - len(question) - 500
    transcript = meeting_text.excerpt(meeting_text.transcript_text(m, cfg), question, max(room, 2000))
    parts.append(f"TRANSCRIPT:\n{transcript}")
    return "\n\n".join(parts)


# ── Auth & app ─────────────────────────────────────────────────────────

@app.get("/api/models")
def speech_models():
    """Which local speech models are installed or downloading (they're fetched on first use)."""
    return models.status()


@app.post("/api/models/download")
def download_models():
    """Start (or retry) downloading any missing local speech model."""
    models.prefetch()
    return models.status()


@app.get("/api/auth")
def auth_status(request: Request):
    return {"required": auth.required(), "ok": auth.is_authorized(request)}


@app.post("/api/login")
def login(body: LoginRequest, request: Request):
    if not auth.required():
        return {"ok": True}
    if not auth.check_token(body.token.strip()):
        raise HTTPException(401, "That access token isn't right")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(**auth.cookie_kwargs(request))
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/api/shutdown", status_code=202)
def shutdown(request: Request, background: BackgroundTasks):
    """Quit Trailmix from the UI. Only for a local install."""
    if auth.required() or (request.client and request.client.host not in ("127.0.0.1", "::1", "localhost")):
        raise HTTPException(403, "Shutting down is only available for a local install")
    if live.active_count():
        raise HTTPException(409, "Stop the recording first")
    background.add_task(lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"ok": True}


@app.get("/api/health")
def health():
    cfg = settings.get_all()

    def provider_info(pid: str) -> dict:
        ready, why = llm_engine.configured(pid, cfg)
        return {"id": pid, "label": llm_engine.label(pid, cfg), "model": llm_engine.model_for(pid, cfg),
                "ready": ready, "reason": why, "local": llm_engine.is_local(pid, cfg)}

    remote = cfg["transcribe_engine"] == "remote"
    return {
        "summary": provider_info(cfg["summary_provider"]),
        "fallback": provider_info(cfg["summary_fallback"]) if cfg["summary_fallback"] != "none" else None,
        "transcription": {
            "engine": cfg["transcribe_engine"],
            "model": cfg["transcribe_model"] if remote else mlx_engine.FINAL_MODEL,
            "live_model": (cfg["transcribe_live_model"] or cfg["transcribe_model"]) if remote else mlx_engine.LIVE_MODEL,
            "url": cfg["transcribe_url"] if remote else None,
            "local_available": mlx_engine.available(),
        },
        "templates": templates.listing(),
        "providers": [{"id": p, "label": llm_engine.label(p, cfg), "kind": v["kind"]} for p, v in llm_engine.PROVIDERS.items()],
        "audio_retention_days": store.RETENTION_DAYS,
        "auth": auth.required(),
        "recording": live.active_count() > 0,
    }


@app.get("/api/system")
def system():
    return resources.snapshot()


# ── Settings & providers ───────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.put("/api/settings")
def put_settings(body: SettingsPatch):
    try:
        return settings.update(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/providers/{pid}/models")
def provider_models(pid: str):
    if pid not in llm_engine.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    try:
        return {"models": llm_engine.list_models(pid, settings.get_all())}
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/api/providers/{pid}/test")
def provider_test(pid: str):
    if pid not in llm_engine.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    cfg = settings.get_all()
    if live.active_count() and llm_engine.is_local(pid, cfg):
        raise HTTPException(409, "Test the local model after you finish recording")
    try:
        return {"ok": True, "message": llm_engine.test(pid, cfg)}
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/api/transcription/test")
def transcription_test():
    cfg = settings.get_all()
    if cfg["transcribe_engine"] != "remote":
        return {"ok": True, "message": f"Local MLX transcription ({'available' if mlx_engine.available() else 'not installed'})"}
    try:
        return {"ok": True, "message": transcribe_remote.check(cfg["transcribe_url"], cfg["transcribe_api_key"], cfg["transcribe_model"])}
    except transcribe_remote.RemoteError as e:
        raise HTTPException(502, str(e))


# ── Meetings ───────────────────────────────────────────────────────────

@app.get("/api/meetings")
def list_meetings():
    return db.list_meetings()


@app.get("/api/search")
def search(q: str = ""):
    return db.search(q) if q.strip() else []


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: int):
    return _present(_get_or_404(meeting_id))


@app.post("/api/meetings", status_code=201)
def start_meeting(body: StartRequest):
    """Creates the meeting row; the client then opens the audio WebSocket for it."""
    if live.active_count():
        raise HTTPException(409, "Trailmix is already recording. Stop that recording first.")
    title = body.title.strip()
    return {"id": db.create_meeting(title or f"Meeting {datetime.now():%b %d, %H:%M}", title_auto=not title)}


@app.websocket("/api/meetings/{meeting_id}/stream")
async def stream(ws: WebSocket, meeting_id: int, draft: int = 1, client: str = "web"):
    """Binary frames: 1 channel byte (0=mic, 1=meeting audio) + int16 PCM @16 kHz.
    Text frames: {"type":"mark"} flags the current moment, {"type":"stop"} ends the recording.
    `client` says which app is capturing ("web" or "helper"); either app can mark or stop it over REST."""
    if not auth.is_authorized(ws):
        await ws.close(code=4401)
        return
    meeting = db.get_meeting(meeting_id)
    if not meeting or meeting["status"] != "recording":
        await ws.close(code=4404)
        return
    await ws.accept()
    session = live.LiveSession(meeting_id, Path(meeting["audio_dir"]), draft=bool(draft), cfg=settings.get_all(),
                               client="helper" if client == "helper" else "web")
    send_lock = asyncio.Lock()
    stop_requested = asyncio.Event()

    async def send(msg: dict):
        async with send_lock:
            await ws.send_json(msg)

    session.request_stop = stop_requested.set  # only called on the event loop (async endpoint below)
    session.notify = send

    async def draft_loop():
        try:
            while True:
                await asyncio.sleep(2)
                for msg in await run_in_threadpool(session.draft_step):
                    await send(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Live draft stopped for meeting %s (recording continues)", meeting_id)

    drafter = asyncio.create_task(draft_loop()) if draft else None
    stop_wait = asyncio.create_task(stop_requested.wait())
    try:
        while True:
            receive = asyncio.create_task(ws.receive())
            done, _ = await asyncio.wait({receive, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if receive not in done:  # stopped from the other app
                receive.cancel()
                break
            msg = receive.result()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                session.write(msg["bytes"])
            elif msg.get("text"):
                data = json.loads(msg["text"])
                if data.get("type") == "stop":
                    break
                if data.get("type") == "mark":
                    await send({"type": "marked", **session.mark(str(data.get("note", "")))})
    except Exception:
        pass  # treat any socket failure like a stop: the audio already on disk is processed
    finally:
        stop_wait.cancel()
        if drafter:
            drafter.cancel()
        session.close()
        db.update_meeting(
            meeting_id, status="queued", duration_sec=session.duration, has_system=int(session.has_system)
        )
        pipeline.enqueue(meeting_id)
        try:
            await send({"type": "stopped", "by": "remote" if stop_requested.is_set() else "you"})
            await ws.close()
        except Exception:
            pass


@app.get("/api/live")
def live_recordings():
    """Recordings in progress, whichever app is capturing them (the web app or the menu bar helper)."""
    out = []
    for session in live.sessions():
        meeting = db.get_meeting(session.meeting_id)
        out.append(session.describe(meeting["title"] if meeting else "Recording"))
    return out


def _live_or_409(meeting_id: int) -> live.LiveSession:
    session = live.get(meeting_id)
    if not session:
        raise HTTPException(409, "That meeting isn't recording")
    return session


@app.post("/api/meetings/{meeting_id}/mark")
async def mark_live(meeting_id: int, body: MarkRequest | None = None):
    """Flags the current moment of a recording, even one another app is capturing."""
    session = _live_or_409(meeting_id)
    mark = await run_in_threadpool(session.mark, body.note if body else "")
    if session.notify:
        try:
            await session.notify({"type": "marked", **mark})  # so the recording app shows it too
        except Exception:
            pass
    return mark


@app.post("/api/meetings/{meeting_id}/stop", status_code=202)
async def stop_live(meeting_id: int):
    """Stops a recording, even one another app is capturing. That app is told, then processing starts."""
    _live_or_409(meeting_id).request_stop()
    return {"ok": True}


@app.patch("/api/meetings/{meeting_id}")
def rename_meeting(meeting_id: int, body: MeetingPatch):
    _get_or_404(meeting_id)
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "Title cannot be empty")
    db.update_meeting(meeting_id, title=title, title_auto=0)  # a hand-picked title is never overwritten
    _reexport(meeting_id)
    return {"ok": True}


@app.put("/api/meetings/{meeting_id}/speakers")
def set_speakers(meeting_id: int, names: dict[str, str]):
    _get_or_404(meeting_id)
    clean = {k: v.strip()[:60] for k, v in names.items() if k in ("You", "Them")}
    db.update_meeting(meeting_id, speaker_names_json=clean)
    _reexport(meeting_id)
    return _present(db.get_meeting(meeting_id))["speaker_names"]


@app.put("/api/meetings/{meeting_id}/bookmarks")
def set_bookmarks(meeting_id: int, body: BookmarksPut):
    _get_or_404(meeting_id)
    marks = sorted(({"t": round(b.t, 2), "note": b.note[:200]} for b in body.bookmarks), key=lambda b: b["t"])
    db.update_meeting(meeting_id, bookmarks_json=marks)
    return marks


@app.get("/api/meetings/{meeting_id}/audio")
def meeting_audio(meeting_id: int):
    meeting = _get_or_404(meeting_id)
    try:
        path = store.playback_path(meeting)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if not path:
        raise HTTPException(404, "No audio available for playback")
    return FileResponse(path, media_type="audio/ogg")


@app.post("/api/meetings/{meeting_id}/confirm", status_code=202)
def confirm(meeting_id: int):
    """Approve the pending step: start transcription (ready_transcribe) or override the low-RAM warning."""
    meeting = _get_or_404(meeting_id)
    if meeting["status"] == "ready_transcribe":
        db.update_meeting(meeting_id, status="queued", wait_reason=None)
        pipeline.enqueue(meeting_id, go=frozenset({"transcribe"}))
    elif meeting["status"] == "waiting_confirm":
        if not pipeline.proceed_now(meeting_id):  # no job is waiting (e.g. from an older version): start one
            stage = frozenset({"summarize" if meeting["transcribed"] else "transcribe"})
            db.update_meeting(meeting_id, status="queued", wait_reason=None)
            pipeline.enqueue(meeting_id, go=stage, force=stage)
    else:
        raise HTTPException(409, "Nothing is waiting for confirmation")
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/retry", status_code=202)
def retry(meeting_id: int):
    """Re-run a job that failed (e.g. transcription error)."""
    meeting = _get_or_404(meeting_id)
    if meeting["status"] != "error":
        raise HTTPException(409, "Only failed meetings can be retried")
    db.update_meeting(meeting_id, status="queued", wait_reason=None, error=None)
    pipeline.enqueue(meeting_id, go=frozenset({"transcribe"}))
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/retranscribe", status_code=202)
def retranscribe(meeting_id: int):
    """Transcribe again from the saved audio (say, after an update that transcribes better), then summarize.
    Speaker names, flags and tasks you ticked off are kept; the summary is rewritten."""
    meeting = _get_or_404(meeting_id)
    if meeting["status"] not in ("done", "error", "ready_summarize"):
        raise HTTPException(409, "This meeting is still being processed")
    if meeting["audio_deleted"] or not store.audio_bytes(meeting):
        raise HTTPException(409, "The audio for this meeting has been deleted, so it can't be transcribed again")
    db.update_meeting(meeting_id, status="queued", wait_reason=None, error=None, transcribed=0, summary=None,
                      summary_error=None)
    pipeline.enqueue(meeting_id, go=frozenset({"transcribe", "summarize"}))
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/summarize", status_code=202)
def resummarize(meeting_id: int, body: SummarizeRequest):
    meeting = _get_or_404(meeting_id)
    if body.provider != "auto" and body.provider not in llm_engine.PROVIDERS:
        raise HTTPException(422, "Unknown provider")
    if body.template and body.template not in templates.TEMPLATES:
        raise HTTPException(422, "Unknown template")
    if not meeting["transcript"] or meeting["status"] not in ("done", "error", "ready_summarize"):
        raise HTTPException(409, "Meeting has no finished transcript to summarize")
    db.update_meeting(
        meeting_id, status="queued", wait_reason=None, summary=None, summary_provider=None,
        summary_error=None, requested_provider=body.provider, requested_template=body.template,
    )
    pipeline.enqueue(meeting_id, go=frozenset({"summarize"}))
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/ask")
def ask_meeting(meeting_id: int, body: Question):
    meeting = _get_or_404(meeting_id)
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "Ask a question")
    if not meeting["transcript"]:
        raise HTTPException(409, "This meeting doesn't have a transcript yet")
    cfg = settings.get_all()
    _local_llm_paused(cfg)
    try:
        answer, used = llm_engine.ask(question, _meeting_context(meeting, cfg, question), cfg)
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))
    entry = {"q": question, "a": answer, "provider": used, "at": datetime.now(timezone.utc).isoformat()}
    history = (json.loads(meeting["qa_json"]) if meeting.get("qa_json") else []) + [entry]
    db.update_meeting(meeting_id, qa_json=history[-50:])
    return entry


@app.delete("/api/meetings/{meeting_id}/ask", status_code=204)
def clear_questions(meeting_id: int):
    _get_or_404(meeting_id)
    db.update_meeting(meeting_id, qa_json=None)


@app.post("/api/ask")
def ask_everything(body: Question):
    """Question across all meetings: find the relevant ones with full-text search, answer from them."""
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "Ask a question")
    cfg = settings.get_all()
    _local_llm_paused(cfg)
    hits = db.search(question, limit=6, any_term=True)
    meetings = db.get_meetings([h["id"] for h in hits]) or db.recent_meetings(5)
    meetings = [m for m in meetings if m.get("transcript")]
    if not meetings:
        raise HTTPException(409, "There are no transcribed meetings to search yet")
    budget = (llm_engine.chain_context_chars(cfg) - len(question) - 500) // len(meetings)
    blocks = []
    for m in meetings:
        when = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).astimezone().strftime("%b %-d, %Y")
        body_text = m["summary"] or meeting_text.transcript_text(m, cfg)
        blocks.append(f"[M{m['id']}] {m['title']} ({when})\n{meeting_text.excerpt(body_text, question, budget)}")
    context = "MEETINGS (each tagged like [M12]):\n\n" + "\n\n---\n\n".join(blocks)
    try:
        answer, used = llm_engine.ask(question, context, cfg, cite_meetings=True)
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))
    return {
        "answer": answer,
        "provider": used,
        "sources": [{"id": m["id"], "title": m["title"], "created_at": m["created_at"]} for m in meetings],
    }


# ── Tasks ──────────────────────────────────────────────────────────────

@app.get("/api/tasks")
def list_tasks():
    return db.all_tasks()


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskPatch):
    meeting_id = db.set_task_done(task_id, body.done)
    if meeting_id is None:
        raise HTTPException(404, "Task not found")
    _reexport(meeting_id)
    return {"ok": True}


# ── Files ──────────────────────────────────────────────────────────────

@app.post("/api/meetings/{meeting_id}/export")
def export_now(meeting_id: int):
    """Write the meeting to the export folder now, using the current export settings."""
    meeting = _get_or_404(meeting_id)
    if not meeting["transcript"]:
        raise HTTPException(409, "Nothing to export yet")
    try:
        paths = exporter.export_meeting(meeting, settings.get_all())
    except OSError as e:
        db.update_meeting(meeting_id, export_error=f"Export failed: {e}")
        raise HTTPException(500, f"Export failed: {e}")
    db.update_meeting(meeting_id, exported_paths=json.dumps(paths), export_error=None)
    return {"paths": paths}


@app.delete("/api/meetings/{meeting_id}/audio", status_code=204)
def delete_audio(meeting_id: int):
    meeting = _get_or_404(meeting_id)
    if meeting["status"] in ("recording", "queued", "transcribing", "waiting_confirm", "ready_transcribe") and not meeting["transcribed"]:
        raise HTTPException(409, "Audio is still needed to produce the transcript")
    store.delete_audio(meeting)


@app.delete("/api/meetings/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int):
    meeting = _get_or_404(meeting_id)
    if meeting["status"] in ("recording", "transcribing", "summarizing"):
        raise HTTPException(409, "Meeting is still being processed")
    store.delete_audio(meeting)
    db.delete_meeting(meeting_id)


# The built web app, when present (./trailmix builds it). Registered last so /api routes win.
if UI_DIR.is_dir():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
