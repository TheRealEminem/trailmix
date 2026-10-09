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
import shutil
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import audio_store as store
import auth
import database as db
import archive
import exporter
import importer
import live
import live_notes
import llm_engine
import meeting_text
import mlx_engine
import models
import ollama_setup
import pipeline
import recorder
import resources
import settings
import templates
import model_ranks
import workspaces
import file_import
import feedback
import questions
import transcribe_remote

log = logging.getLogger("trailmix")
SWEEP_INTERVAL_S = 6 * 3600
UI_DIR = Path(os.getenv("TRAILMIX_UI_DIR", Path(__file__).resolve().parent.parent / "frontend" / "dist"))


async def _sweep_loop():
    while True:
        await run_in_threadpool(store.sweep_expired)
        await run_in_threadpool(model_ranks.refresh)  # newer model rankings from the Trailmix site, once a day
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


WARM_EVERY_S = 15
LIVE_NOTES_EVERY_S = 10


async def _warm_loop():
    """Keeps the live speech model loaded while Trailmix is idle, so a recording's first words show up within
    seconds: from startup, again after each meeting's processing (which frees it for the summary model), and
    as soon as a call app (Zoom, Teams, FaceTime…) opens audio. Only with room to spare in memory, unless a
    call is starting (then only not when it would be dire)."""
    while True:
        try:
            cfg = settings.get_all()
            model = live.live_model(cfg)
            if model and mlx_engine.loaded() != model:
                need = mlx_engine.FINAL_MODEL_RAM_GB if cfg["live_final"] else 0.5
                calling = bool(recorder.status().get("call_app"))
                if resources.has_room(need) or (calling and resources.check(need, "The speech model") is None):
                    live.warm_up(cfg)
        except Exception:
            log.exception("Warming the speech model failed")
        await asyncio.sleep(WARM_EVERY_S)


async def _live_notes_loop():
    """Notes during the meeting (live_notes.py): checks every few seconds whether a recording has ten new
    minutes of transcript to take notes on."""
    while True:
        try:
            if live.active_count():
                await run_in_threadpool(live_notes.tick)
        except Exception:
            log.exception("Notes during the meeting failed")
        await asyncio.sleep(LIVE_NOTES_EVERY_S)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _exit_with_parent()
    db.init_db()
    models.prefetch()
    file_import.clear_leftovers()  # temporary copies from an import the last run didn't finish
    pipeline.recover_unfinished()
    sweeper = asyncio.create_task(_sweep_loop())
    warmer = asyncio.create_task(_warm_loop()) if os.getenv("TRAILMIX_WARM") != "0" else None
    note_taker = asyncio.create_task(_live_notes_loop())
    yield
    note_taker.cancel()
    sweeper.cancel()
    if warmer:
        warmer.cancel()


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
    workspace_id: int | None = None  # the workspace you're in when you press Record


class WorkspaceBody(BaseModel):
    name: str | None = None
    color: str | None = None
    about: str | None = None
    position: int | None = None


class MeetingWorkspace(BaseModel):
    workspace_id: int | None = None


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
    workspace_id: int | None = None  # only meetings in this workspace


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
    hidden = ("audio_path", "audio_dir", "segments_json", "requested_provider", "draft_json", "exported_paths", "live_json", "live_notes_json",
              "tags_json",
              "bookmarks_json", "speaker_names_json", "qa_json")
    out = {k: v for k, v in m.items() if k not in hidden}
    cfg = settings.get_all()
    out["segments"] = meeting_text.segments(m) or None
    out["tags"] = json.loads(m["tags_json"]) if m.get("tags_json") else []
    out["exported_paths"] = json.loads(m["exported_paths"]) if m.get("exported_paths") else []
    out["bookmarks"] = meeting_text.bookmarks(m)
    out["speaker_names"] = meeting_text.speaker_names(m, cfg)
    out["qa"] = json.loads(m["qa_json"]) if m.get("qa_json") else []
    out["tasks"] = db.tasks_for(m["id"])
    out["summary_draft"] = None if m["summary"] else pipeline.draft(m["id"])  # the notes so far, while they're written
    out["title_auto"] = bool(m["title_auto"])
    out["audio_bytes"] = store.audio_bytes(m)
    out["audio_expires_at"] = store.expires_at(m)
    out["audio_deleted"] = bool(m["audio_deleted"])
    out["has_system"] = bool(m["has_system"])
    out["keep_audio"] = bool(m.get("keep_audio"))
    folder = next((str(Path(p).parent) for p in out["exported_paths"] if p.endswith("meeting.json")), None)
    out["export_folder"] = folder
    return out


def _reexport(meeting_id: int) -> None:
    """Keep an already-exported file in step with edits (title, names, ticked tasks)."""
    meeting = db.get_meeting(meeting_id)
    cfg = settings.get_all()
    if not (meeting and meeting["exported_paths"] and meeting["transcript"]):  # exported once: keep it current
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

class RecorderCheckIn(BaseModel):
    mic: str = ""
    source: str = ""
    machine: str = ""
    version: str = ""
    mic_allowed: bool = True
    sound_check: dict | None = None
    shortcut_record: str = ""
    shortcut_mark: str = ""
    open_at_login: bool | None = None
    update: dict | None = None


class OpenAtLogin(BaseModel):
    on: bool


@app.post("/api/recorder/check-in")
def recorder_check_in(body: RecorderCheckIn):
    """The menu bar app, about once a second: what it would record, and any commands for it."""
    return {"commands": recorder.check_in(body.model_dump())}


@app.get("/api/recorder")
def recorder_status():
    """Whether a native recorder is available, and what it would record."""
    return recorder.status()


@app.post("/api/recorder/start", status_code=202)
def recorder_start():
    """Ask the native recorder to start recording (the web app's Record button, when one is available)."""
    if live.active_count():
        raise HTTPException(409, "Trailmix is already recording")
    if not recorder.request("start"):
        raise HTTPException(409, "The menu bar recorder isn't running")
    return {"ok": True}


class GranolaImport(BaseModel):
    note_ids: list[str]
    keep_summary: bool = True


class TextImport(BaseModel):
    text: str
    title: str = ""
    date: str = ""  # ISO date or date-time; empty = now


def _granola_key() -> str:
    key = settings.get_all()["granola_api_key"]
    if not key:
        raise HTTPException(400, "Add your Granola API key first")
    return key


@app.get("/api/import/granola/notes")
def granola_notes():
    """Your Granola meetings (needs a Granola API key in Settings), marking the ones already imported."""
    try:
        return {"notes": importer.granola_notes(_granola_key())}
    except importer.ImportError_ as e:
        raise HTTPException(502, str(e))


@app.post("/api/import/granola", status_code=202)
def granola_import(body: GranolaImport):
    if not body.note_ids:
        raise HTTPException(422, "Pick at least one meeting")
    if not importer.start_granola_import(_granola_key(), body.note_ids, body.keep_summary):
        raise HTTPException(409, "An import is already running")
    return importer.job_status()


@app.get("/api/import/granola")
def granola_import_status():
    return importer.job_status()


@app.post("/api/import/text", status_code=201)
def import_text(body: TextImport):
    """A transcript pasted or dropped in as text (Granola's Copy transcript, or any "Name: …" transcript)."""
    cfg = settings.get_all()
    segments = importer.parse_text(body.text, {cfg.get("your_name", "")})
    if not segments:
        raise HTTPException(422, "There's no transcript text to import")
    when = importer._parse_time(body.date) if body.date else None
    if when and when.tzinfo is None:
        when = when.astimezone()  # a plain date means local time
    meeting_id = importer.save_imported(body.title.strip(), when or datetime.now(timezone.utc), segments, "paste", None, None)
    return {"id": meeting_id}


@app.post("/api/import/file")
async def import_file(request: Request, name: str, modified: float = 0):
    """One file, sent as the request body (streamed to disk, so a long video never sits in memory): a
    recording (audio or video) or a transcript from another app. `modified`: the file's date (ms), used when
    the file itself doesn't say when it was recorded."""
    what = file_import.kind(name)
    if not what:
        raise HTTPException(415, f"{name}: Trailmix can't import this kind of file")
    when = datetime.fromtimestamp(modified / 1000, timezone.utc) if modified else datetime.now(timezone.utc)
    cfg = settings.get_all()
    if what == "transcript":
        data = await request.body()
        try:
            meeting_id = await run_in_threadpool(file_import.import_transcript, name, data, when, {cfg["your_name"]})
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"kind": what, "id": meeting_id, "skipped": meeting_id is None}
    temp = file_import.temp_path(name)
    try:
        with temp.open("wb") as out:
            async for chunk in request.stream():
                out.write(chunk)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    try:
        meeting_id = await run_in_threadpool(file_import.import_recording, name, temp, when)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"kind": what, "id": meeting_id, "skipped": meeting_id is None}


@app.post("/api/import/trailmix")
async def import_trailmix(files: list[UploadFile] = File(...), paths: list[str] = Form(default=[])):
    """Meetings exported from Trailmix: a meeting folder, a whole export folder, or meeting.json files. Each
    meeting.json is restored with the recording next to it, if any. Meetings already here are skipped."""
    work = Path(tempfile.mkdtemp(prefix="trailmix-import-"))
    try:
        for i, upload in enumerate(files):
            rel = Path(paths[i] if i < len(paths) and paths[i] else upload.filename or f"file{i}")
            if rel.is_absolute() or ".." in rel.parts:
                continue
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                shutil.copyfileobj(upload.file, out)
        imported, skipped, problems = [], 0, []
        for found in sorted(work.rglob("*.json")):
            try:
                data = json.loads(found.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not archive.looks_like_record(data):
                continue
            try:
                made = await run_in_threadpool(archive.restore, data, found.parent)
            except (ValueError, KeyError) as e:
                problems.append(f"{found.parent.name}: {e}")
                continue
            if made:
                imported.append(made)
            else:
                skipped += 1
        if not imported and not skipped and not problems:
            raise HTTPException(422, "No Trailmix meetings found. Pick a meeting folder (or your export folder) with meeting.json in it.")
        return {"imported": imported, "skipped": skipped, "problems": problems}
    finally:
        shutil.rmtree(work, ignore_errors=True)


class PullRequest(BaseModel):
    model: str = ""


@app.get("/api/ollama")
def ollama_status():
    """Is Ollama there, what's installed, what we'd recommend for this Mac, and any download in progress."""
    return ollama_setup.status(settings.get_all())


@app.post("/api/ollama/install", status_code=202)
def ollama_install():
    """Download, check and install Ollama (the Mac app), or open it if it's installed but not running."""
    if sys.platform != "darwin":
        raise HTTPException(409, "Installing Ollama from here only works on a Mac")
    if not ollama_setup.start_install(settings.get_all()):
        raise HTTPException(409, "Ollama is already being installed")
    return ollama_setup.status(settings.get_all())


@app.post("/api/ollama/pull", status_code=202)
def ollama_pull(body: PullRequest):
    cfg = settings.get_all()
    model = body.model.strip() or ollama_setup.recommended()
    if not ollama_setup.status(cfg)["reachable"]:
        raise HTTPException(409, "Ollama isn't running. Install it from ollama.com, open it, and try again.")
    if not ollama_setup.start_pull(cfg, model):
        raise HTTPException(409, "A model is already downloading")
    return ollama_setup.status(cfg)


@app.post("/api/recorder/open-at-login", status_code=202)
def recorder_open_at_login(body: OpenAtLogin):
    """Start Trailmix (and its menu bar recorder) when you log in to your Mac, or stop doing so."""
    if not recorder.request("open-at-login" if body.on else "no-open-at-login"):
        raise HTTPException(409, "The menu bar recorder isn't running")
    return {"ok": True}


@app.post("/api/recorder/update", status_code=202)
def recorder_update():
    """Install the new version of Trailmix.app the menu bar app found (it downloads, checks, swaps, relaunches)."""
    if live.active_count():
        raise HTTPException(409, "Finish the recording first, then update")
    if not recorder.request("update"):
        raise HTTPException(409, "The menu bar recorder isn't running")
    return {"ok": True}


@app.post("/api/recorder/beta-updates", status_code=202)
def recorder_beta_updates(body: OpenAtLogin):
    """Beta updates: offer pre-releases (new versions being tried out before everyone gets them)."""
    if not recorder.request("beta-updates" if body.on else "no-beta-updates"):
        raise HTTPException(409, "The menu bar recorder isn't running")
    return {"ok": True}


@app.post("/api/recorder/check-update", status_code=202)
def recorder_check_update():
    """Ask Trailmix.app to look for a new version now (the answer arrives with its next check-in)."""
    if not recorder.request("check-update"):
        raise HTTPException(409, "The menu bar recorder isn't running")
    return {"ok": True}


@app.post("/api/recorder/sound-check", status_code=202)
def recorder_sound_check():
    """Ask the menu bar recorder to check the mic and other apps' audio (plays a short chime)."""
    if live.active_count():
        raise HTTPException(409, "Not while recording")
    if not recorder.request("sound-check"):
        raise HTTPException(409, "The menu bar recorder isn't running")
    return {"ok": True}


@app.get("/api/device")
def device():
    """What this Mac can comfortably run (memory, disk, chip), for the setup checklist."""
    return resources.device_advice()


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
            "live_model": (cfg["transcribe_model"] if cfg["live_final"] else cfg["transcribe_live_model"] or cfg["transcribe_model"])
            if remote else (mlx_engine.FINAL_MODEL if cfg["live_final"] else mlx_engine.LIVE_MODEL),
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
    # The workspace you're in (the window says which; the menu bar and hotkey use the last one you picked).
    wanted = body.workspace_id or settings.get_all()["current_workspace"]
    space = wanted if wanted and db.get_workspace(wanted) else None
    return {"id": db.create_meeting(title or f"Meeting {datetime.now():%b %d, %H:%M}", title_auto=not title,
                                    workspace_id=space)}


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
                await asyncio.sleep(0.5)  # a chunk is ready every few seconds; pick it up promptly
                for msg in await run_in_threadpool(session.draft_step):
                    if draft:  # final-while-recording runs this loop even with the live draft turned off
                        await send(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Live draft stopped for meeting %s (recording continues)", meeting_id)

    drafter = asyncio.create_task(draft_loop()) if draft or session.final else None
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
        await run_in_threadpool(session.finish)  # waits for the chunk in hand, saves the transcript so far
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
        out.append({**session.describe(meeting["title"] if meeting else "Recording"), **live_notes.status(session.meeting_id)})
    return out


@app.get("/api/live-notes")
def live_notes_plan():
    """Whether notes are written during meetings with your settings, why, and this Mac's power and memory."""
    return live_notes.decide(settings.get_all())


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
                      summary_error=None, live_json=None)  # the whole recording, from scratch
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


# ── Workspaces ──────────────────────────────────────────────────────────

@app.get("/api/workspaces")
def list_workspaces():
    return db.list_workspaces()


@app.post("/api/workspaces", status_code=201)
def create_workspace(body: WorkspaceBody):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "Give the workspace a name")
    if db.workspace_by_name(name):
        raise HTTPException(409, f"There's already a workspace called {name}")
    return db.get_workspace(db.create_workspace(name[:60], body.color or "", (body.about or "")[:500]))


@app.patch("/api/workspaces/{workspace_id}")
def update_workspace(workspace_id: int, body: WorkspaceBody):
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "No such workspace")
    name = body.name.strip()[:60] if body.name is not None else None
    if name == "":
        raise HTTPException(422, "Give the workspace a name")
    other = db.workspace_by_name(name) if name else None
    if other and other["id"] != workspace_id:
        raise HTTPException(409, f"There's already a workspace called {name}")
    if body.color is not None and body.color not in db.WORKSPACE_COLORS:
        raise HTTPException(422, "Unknown color")
    db.update_workspace(workspace_id, name=name, color=body.color,
                        about=body.about.strip()[:500] if body.about is not None else None, position=body.position)
    return db.get_workspace(workspace_id)


@app.delete("/api/workspaces/{workspace_id}")
def delete_workspace(workspace_id: int):
    """The workspace goes; its meetings stay, unsorted."""
    db.delete_workspace(workspace_id)
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/workspace")
def set_meeting_workspace(meeting_id: int, body: MeetingWorkspace):
    """Puts a meeting in a workspace (or none). Your choice is never changed by automatic sorting."""
    _get_or_404(meeting_id)
    if body.workspace_id and not db.get_workspace(body.workspace_id):
        raise HTTPException(404, "No such workspace")
    db.update_meeting(meeting_id, workspace_id=body.workspace_id, workspace_auto=0)
    _reexport(meeting_id)
    return {"ok": True}


class AnswerRequest(BaseModel):
    value: str


@app.get("/api/meetings/{meeting_id}/questions")
def meeting_questions(meeting_id: int):
    """What the AI wasn't sure about in this meeting, to ask you (questions.py)."""
    _get_or_404(meeting_id)
    return questions.open_for(meeting_id)


@app.get("/api/questions")
def question_counts():
    """{meeting id: open questions}, for the sidebar."""
    return {str(k): v for k, v in questions.open_counts().items()}


@app.post("/api/questions/{question_id}")
def answer_question(question_id: int, body: AnswerRequest):
    """Your answer: fixes the meeting (a name, a misheard word, who was on the call, its workspace)."""
    try:
        meeting_id = questions.answer(question_id, body.value[:80])
    except KeyError:
        raise HTTPException(404, "No such question")
    _reexport(meeting_id)
    return {"ok": True}


@app.post("/api/questions/{question_id}/dismiss")
def dismiss_question(question_id: int):
    questions.dismiss(question_id)
    return {"ok": True}


class ReportRequest(BaseModel):
    description: str = ""
    screen: str = ""              # the window's text, private parts already replaced by placeholders
    page: str = ""                # which screen it was (e.g. "Settings", "Meeting")
    include_screen: bool = True
    include_diagnostics: bool = True
    blocks: bool = False          # "this stops me using Trailmix": holds the beta back from everyone
    meeting_id: int | None = None


@app.post("/api/report")
def prepare_report(body: ReportRequest):
    """A problem report with personal information removed (feedback.py), for you to check, then open as a
    GitHub issue. Nothing is sent anywhere by this: you send it."""
    cfg = settings.get_all()
    return feedback.prepare(body.description[:4000], body.screen[:20000], body.page[:60], body.include_screen,
                            body.include_diagnostics, body.blocks, body.meeting_id, cfg)


class OrganizeRequest(BaseModel):
    kind: str                # sort | resort | tag | suggest (see workspaces.start)
    only_older: bool = False  # tag: also redo tags made by a weaker model than yours now


@app.post("/api/organize", status_code=202)
def organize(body: OrganizeRequest):
    """Tags, sorts or re-sorts meetings, or suggests workspaces, in the background (follow it with GET)."""
    if body.kind not in ("sort", "resort", "tag", "suggest"):
        raise HTTPException(422, "Unknown job")
    if body.kind in ("sort", "resort") and not db.list_workspaces():
        raise HTTPException(409, "Add a workspace first")
    cfg = settings.get_all()
    _local_llm_paused(cfg)
    if not workspaces.start(body.kind, cfg, body.only_older):
        raise HTTPException(409, "Trailmix is already organizing your meetings")
    return workspaces.job_status()


@app.get("/api/organize")
def organize_status():
    return workspaces.job_status()


# ── Redoing work with a better model ────────────────────────────────────

def _upgradable(cfg: dict) -> dict:
    """What the AI you have now would do better than the model that did it, per job."""
    current = llm_engine.model_for(llm_engine.chain("auto", cfg)[0], cfg)
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, summary, summary_provider, summary_model, tags_json, tags_model, workspace_id,
                      workspace_auto, workspace_model, status FROM meetings WHERE status = 'done'""").fetchall()
    ours = set(llm_engine.PROVIDERS)
    notes = [r for r in rows if r["summary"] and r["summary_provider"] in ours
             and model_ranks.better(current, r["summary_model"])]
    tags = [r for r in rows if r["tags_json"] and model_ranks.better(current, r["tags_model"])]
    sorting = [r for r in rows if r["workspace_auto"] and r["workspace_model"]
               and model_ranks.better(current, r["workspace_model"])]

    def by_model(items, key):
        counts: dict[str, int] = {}
        for r in items:
            counts[r[key] or "an earlier model"] = counts.get(r[key] or "an earlier model", 0) + 1
        return counts

    return {"model": current, "score": model_ranks.score(current),
            "notes": {"count": len(notes), "ids": [r["id"] for r in notes], "from": by_model(notes, "summary_model")},
            "tags": {"count": len(tags), "from": by_model(tags, "tags_model")},
            "sorting": {"count": len(sorting), "from": by_model(sorting, "workspace_model")}}


@app.get("/api/upgrades")
def upgrades():
    """Meetings whose notes, tags or workspace came from a weaker model than the one you have now."""
    data = _upgradable(settings.get_all())
    data["notes"].pop("ids")
    return data


@app.post("/api/upgrades/notes", status_code=202)
def upgrade_notes():
    """Rewrites the notes made by a weaker model, one meeting after another (your ticked tasks stay ticked)."""
    cfg = settings.get_all()
    _local_llm_paused(cfg)
    ids = _upgradable(cfg)["notes"]["ids"]
    for meeting_id in ids:
        db.update_meeting(meeting_id, status="queued", wait_reason=None, summary=None, summary_error=None,
                          requested_provider="auto")
        pipeline.enqueue(meeting_id, go=frozenset({"summarize"}))
    return {"queued": len(ids)}


@app.post("/api/meetings/{meeting_id}/retitle")
def retitle(meeting_id: int):
    """A new title from the meeting's notes (it's yours to rename again; automatic titling won't change it)."""
    m = _get_or_404(meeting_id)
    if not m["summary"]:
        raise HTTPException(409, "This meeting has no notes to name it from yet")
    cfg = settings.get_all()
    _local_llm_paused(cfg)
    notes = llm_engine.place_moments(m["summary"], [])[:6000]
    try:
        title, _ = llm_engine.run_chain("auto", cfg, lambda pid: llm_engine.clean_title(
            llm_engine.generate(pid, cfg, llm_engine.TITLE_PROMPT.format(summary=notes))))
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))
    if not title:
        raise HTTPException(502, "The AI didn't come up with a usable title; try again")
    db.update_meeting(meeting_id, title=title, title_auto=1)
    _reexport(meeting_id)
    return {"title": title}


@app.post("/api/ask")
def ask_everything(body: Question):
    """Question across all meetings: find the relevant ones with full-text search, answer from them."""
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "Ask a question")
    cfg = settings.get_all()
    _local_llm_paused(cfg)
    hits = db.search(question, limit=6, any_term=True, workspace_id=body.workspace_id)
    meetings = db.get_meetings([h["id"] for h in hits]) or db.recent_meetings(5, body.workspace_id)
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


class KeepRequest(BaseModel):
    keep: bool


@app.post("/api/meetings/{meeting_id}/keep")
def keep_forever(meeting_id: int, body: KeepRequest):
    """'Keep forever': the recording is never cleaned up, and the meeting is exported with it, so it lives on
    in your export folder (hard-linked: no second copy on the same disk)."""
    meeting = _get_or_404(meeting_id)
    db.update_meeting(meeting_id, keep_audio=int(body.keep))
    meeting = db.get_meeting(meeting_id)
    if body.keep and meeting["transcript"]:
        try:
            paths = exporter.export_meeting(meeting, settings.get_all(), include_audio=True)
            db.update_meeting(meeting_id, exported_paths=json.dumps(paths), export_error=None)
        except OSError as e:
            db.update_meeting(meeting_id, export_error=f"Export failed: {e}")
            raise HTTPException(500, f"Couldn't save it to the export folder: {e}")
    return _present(db.get_meeting(meeting_id))


class ExportAll(BaseModel):
    include_audio: bool = False


@app.post("/api/export/all", status_code=202)
def export_all(body: ExportAll):
    """Export every transcribed meeting now, in the background."""
    if not exporter.export_all(settings.get_all(), body.include_audio):
        raise HTTPException(409, "An export is already running")
    return exporter.job_status()


@app.get("/api/export/all")
def export_all_status():
    return exporter.job_status()


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
class _UI(StaticFiles):
    """The web app. Its page must never be cached (an update changes which scripts it loads, and a stale copy
    keeps running the old version); the scripts it loads have their content hash in the name, so those can be."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        immutable = path.startswith("assets/") and response.status_code == 200
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


if UI_DIR.is_dir():
    app.mount("/", _UI(directory=UI_DIR, html=True), name="ui")
