"""Trailmix API: stream meeting audio, draft a live transcript, then transcribe + summarize.

Runs on your Mac by default (everything local). The same app can run on a server: point transcription
and summaries at remote endpoints in Settings and set TRAILMIX_ACCESS_TOKEN (see docs/self-hosting.md).
If frontend/dist exists, the UI is served from here too, so one process is the whole app.
"""
import asyncio
import json
import logging
import re
import os
import signal
import shutil
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import attachments
import audio_store as store
import auth
import database as db
import duplicates
import events
import gemini_client
import archive
import batch_jobs
import catch_up
import cloud
import compare
import exporter
import importer
import live
import live_notes
import llm_engine
import logfile
import meeting_date
import meeting_text
import local_engine
import models
import ollama_setup
import pipeline
import preflight
import recorder
import resources
import settings
import telemetry
import templates
import trackers
import model_ranks
import recording_laws
import pricing
import workspaces
import file_import
import feedback
import questions
import gemini_batch
import sources
import watched
import transcribe_gemini
import transcribe_remote

if "pytest" not in sys.modules:  # the tests import this module, but keep their own data folder and output
    logfile.configure()
log = logging.getLogger("trailmix")
SWEEP_INTERVAL_S = 6 * 3600
UI_DIR = Path(os.getenv("TRAILMIX_UI_DIR", Path(__file__).resolve().parent.parent / "frontend" / "dist"))


async def _sweep_loop():
    while True:
        await run_in_threadpool(store.sweep_expired)
        await run_in_threadpool(store.prune_playback_copies)  # the playback mixes nobody has played lately
        await run_in_threadpool(model_ranks.refresh)  # newer model rankings from the Trailmix site, once a day
        await run_in_threadpool(recording_laws.refresh)  # and corrections to the recording-laws table
        await asyncio.sleep(SWEEP_INTERVAL_S)


async def _stats_loop():
    """The once-a-day anonymous usage summary (telemetry.py); checked hourly so it goes out on any day
    Trailmix is open."""
    while True:
        try:
            await run_in_threadpool(telemetry.daily)
        except Exception:
            log.exception("Usage stats failed")
        await asyncio.sleep(3600)


def _exit_with_parent() -> None:
    """The app (Trailmix.app, or Trailmix.exe on Windows) starts this server and names itself in
    TRAILMIX_PARENT_PID. If the app is force-quit or crashes, this process is re-parented (to launchd on a
    Mac); stop instead of lingering on the port."""
    parent = int(os.getenv("TRAILMIX_PARENT_PID") or 0)
    if not parent:
        return

    if sys.platform == "win32":
        # Windows keeps reporting the old parent's id after it's gone: wait on the process itself instead.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        handle = kernel32.OpenProcess(0x00100000, False, parent)  # SYNCHRONIZE
        if not handle:
            return

        def watch_handle():
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=watch_handle, daemon=True, name="parent-watch").start()
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
            if model and local_engine.loaded() != model:
                need = local_engine.FINAL_MODEL_RAM_GB if cfg["live_final"] else 0.5
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


BATCH_CHECK_EVERY_S = 60


async def _batch_loop():
    """Batch jobs sent to Google (batch_jobs.py): asks how they're going once a minute while any is open, and puts the
    answers of finished ones in their meetings."""
    while True:
        try:
            await run_in_threadpool(batch_jobs.check)
        except Exception:
            log.exception("Checking batch jobs failed")
        await asyncio.sleep(BATCH_CHECK_EVERY_S)


SYNC_EVERY_S = 15 * 60


async def _sources_loop():
    """Other note takers' new meetings (sources.py): every 15 minutes, from each source that's turned on. Off with
    TRAILMIX_SYNC=0 (a test run on a copy of your database)."""
    await asyncio.sleep(60)  # after start-up's own work
    while True:
        try:
            await run_in_threadpool(sources.sync_all)
        except Exception:
            log.exception("Syncing other note takers failed")
        await asyncio.sleep(SYNC_EVERY_S)


FOLDERS_EVERY_S = 60


async def _folders_loop():
    """Watched folders (watched.py): looks for new recordings and transcripts in them every minute. Off with
    TRAILMIX_SYNC=0, like the sources loop."""
    while True:
        try:
            await run_in_threadpool(watched.scan_all)
        except Exception:
            log.exception("Looking through watched folders failed")
        await asyncio.sleep(FOLDERS_EVERY_S)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _exit_with_parent()
    before = db.last_run_version()
    try:
        if copy := db.back_up_for(archive.APP_VERSION):  # before this version changes the database
            log.info("Backed up the database to %s", copy)
    except Exception:
        log.exception("Couldn't back up the database before starting this version")
    db.init_db()
    telemetry.version_changed(before, archive.APP_VERSION)
    models.prefetch()
    file_import.clear_leftovers()  # temporary copies from an import the last run didn't finish
    pipeline.recover_unfinished()
    events.topics = {"recorder": recorder.status, "live": live_recordings, "system": resources.snapshot,
                     "models": models.status}
    ticker = asyncio.create_task(events.run())
    sweeper = asyncio.create_task(_sweep_loop())
    warmer = asyncio.create_task(_warm_loop()) if os.getenv("TRAILMIX_WARM") != "0" else None
    note_taker = asyncio.create_task(_live_notes_loop())
    batches = asyncio.create_task(_batch_loop())
    stats = asyncio.create_task(_stats_loop())
    syncing = os.getenv("TRAILMIX_SYNC") != "0"
    syncer = asyncio.create_task(_sources_loop()) if syncing else None
    watcher = asyncio.create_task(_folders_loop()) if syncing else None
    yield
    if syncer:
        syncer.cancel()
    if watcher:
        watcher.cancel()
    batches.cancel()
    stats.cancel()
    note_taker.cancel()
    sweeper.cancel()
    ticker.cancel()
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
    engine: str | None = None        # speech engine for this recording (local, remote, gemini); None: Settings


class WorkspaceBody(BaseModel):
    name: str | None = None
    color: str | None = None
    about: str | None = None
    position: int | None = None
    template: str | None = None  # the note style for its meetings ("" = the default in Settings)


class MeetingWorkspace(BaseModel):
    workspace_id: int | None = None


class MeetingPatch(BaseModel):
    title: str | None = None
    created_at: str | None = None  # when it happened: ISO, with the time zone (a plain date is local midnight)


class SettingsPatch(BaseModel):
    model_config = {"extra": "allow"}  # validated key-by-key in settings.update


class RetranscribeRequest(BaseModel):
    engine: str | None = None  # local | remote | gemini, just for this time; None = the one in Settings


class SummarizeRequest(BaseModel):
    provider: str = "auto"      # auto | a provider id
    template: str | None = None


class LoginRequest(BaseModel):
    token: str


class Question(BaseModel):
    question: str
    workspace_id: int | None = None  # only meetings in this workspace


class CatchUpRequest(BaseModel):
    kind: str = "question"  # "missed", "explain", "decided", or "question" (yours, in `question`)
    question: str = ""


class Bookmark(BaseModel):
    t: float
    note: str = ""


class MarkRequest(BaseModel):
    note: str = ""


class MyNote(BaseModel):
    text: str
    t: float | None = None  # seconds into the recording; None for notes added afterwards


class MyNotes(BaseModel):
    notes: list[MyNote]


class BookmarksPut(BaseModel):
    bookmarks: list[Bookmark]


class TaskPatch(BaseModel):
    done: bool | None = None
    deleted: bool | None = None  # false: back from being deleted (Undo)


# ── Helpers ────────────────────────────────────────────────────────────

def _get_or_404(meeting_id: int) -> dict:
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    return meeting


def _present(m: dict) -> dict:
    """Public shape of a meeting: hides file paths, adds parsed fields and audio info."""
    hidden = ("audio_path", "audio_dir", "segments_json", "requested_provider", "merged_json", "not_duplicate_json", "draft_json", "exported_paths", "live_json", "live_notes_json", "my_notes_json",
              "tags_json",
              "bookmarks_json", "speaker_names_json", "qa_json")
    out = {k: v for k, v in m.items() if k not in hidden}
    cfg = settings.get_all()
    out["segments"] = meeting_text.segments(m) or None
    out["tags"] = json.loads(m["tags_json"]) if m.get("tags_json") else []
    out["exported_paths"] = json.loads(m["exported_paths"]) if m.get("exported_paths") else []
    out["bookmarks"] = meeting_text.bookmarks(m)
    out["my_notes"] = meeting_text.my_notes(m)
    out["speaker_names"] = meeting_text.speaker_names(m, cfg)
    out["qa"] = json.loads(m["qa_json"]) if m.get("qa_json") else []
    tasks = db.tasks_for(m["id"], deleted=True)
    out["tasks"] = [t for t in tasks if not t["deleted"]]
    out["tasks_deleted"] = len(tasks) - len(out["tasks"])  # all of them deleted: no Action Items section at all
    out["attachments"] = [attachments.present(a) for a in db.attachments_for(m["id"])]
    out["summary_draft"] = None if m["summary"] else pipeline.draft(m["id"])  # the notes so far, while they're written
    out["title_auto"] = bool(m["title_auto"])
    out["audio_bytes"] = store.audio_bytes(m)
    out["audio_expires_at"] = store.expires_at(m)
    out["audio_deleted"] = bool(m["audio_deleted"])
    out["has_system"] = bool(m["has_system"])
    out["keep_audio"] = bool(m.get("keep_audio"))
    out["ask_first"] = bool(m.get("ask_first"))
    # Copies of this meeting merged into it (newest last), and a copy it may be the same as (you're asked).
    out["merged"] = [{"from": duplicates._label(r["meeting"]), "title": r["meeting"]["title"], "at": r["at"],
                      "offset": r.get("offset") or 0}
                     for r in (json.loads(m["merged_json"]) if m.get("merged_json") else [])]
    other = db.get_meeting(m["duplicate_of"]) if m.get("duplicate_of") else None
    out["duplicate_of"] = ({"id": other["id"], "title": other["title"], "from": duplicates._label(other),
                            "created_at": other["created_at"]} if other else None)
    pid = m.get("requested_provider")  # the AI picked for its notes, if not the default
    out["notes_with"] = (f"{llm_engine.label(pid, cfg)} · {m['requested_model'] or llm_engine.model_for(pid, cfg)}"
                         if pid in llm_engine.PROVIDERS else None)
    folder = next((str(Path(p).parent) for p in out["exported_paths"] if p.endswith("meeting.json")), None)
    out["export_folder"] = folder
    return out


def _reexport(meeting_id: int) -> None:
    """Keep an already-exported file in step with edits (title, names, ticked tasks)."""
    pipeline.reexport(meeting_id)


def _local_llm_paused(cfg: dict) -> None:
    """Don't load a local model on this machine in the middle of a meeting."""
    first = llm_engine.chain("auto", cfg)[0]
    if live.active_count() and llm_engine.is_local(first, cfg):
        raise HTTPException(409, "Your local model is paused while you're recording. Ask again after you stop, "
                                 "or pick a cloud provider in Settings.")


def _meeting_context(m: dict, cfg: dict, question: str) -> str:
    """The meeting's notes and transcript, cut to what the model can take: for a long meeting and a small
    local model, the parts of the transcript that match the question."""
    local = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).astimezone()
    when = f"{local:%A %B} {local.day}, {local:%Y}"
    parts = [f"MEETING: {m['title']} ({when})"]
    if m.get("summary"):
        parts.append(f"NOTES:\n{m['summary']}")
    if material := attachments.context_block(m["id"], llm_engine.chain_context_chars(cfg) // 5):
        parts.append(material)
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
    call_app: str | None = None  # a call app using audio right now (the speech model gets ready)
    call_reminders: bool | None = None
    lockdown: bool | None = None  # the recorder's copy of Lockdown mode (it skips update checks)
    platform: str | None = None  # "macos-arm64", "windows-x64"…
    capabilities: list[str] | None = None  # recorder.CAPABILITIES it supports; None: an older Mac app (all)


class OpenAtLogin(BaseModel):
    on: bool


@app.post("/api/recorder/check-in")
def recorder_check_in(body: RecorderCheckIn):
    """The menu bar app, about once a second: what it would record, and any commands for it."""
    commands = recorder.check_in(body.model_dump())
    want = settings.locked()
    if body.lockdown is not None and body.lockdown != want:  # keep the recorder's copy in step
        commands.append("lockdown" if want else "no-lockdown")
    return {"commands": commands}


@app.websocket("/api/recorder/ws")
async def recorder_socket(ws: WebSocket):
    """The menu bar app's line to the server. It sends what it would record (JSON, as for check-in) whenever that
    changes; it gets {"commands": [...]} the moment the window asks for something, and {"live": [...]} whenever
    the recordings in progress change, so it needn't ask."""
    if not auth.is_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    send_lock = asyncio.Lock()

    async def send(message: dict) -> None:
        async with send_lock:
            await ws.send_json(message)

    def push(commands: list[str]) -> None:  # from any thread (recorder.request runs in the threadpool)
        asyncio.run_coroutine_threadsafe(send({"commands": commands}), loop)

    async def forward_live() -> None:
        queue, current = await events.subscribe(frozenset({"live"}))
        try:
            if "live" in current:
                await send({"live": json.loads(current["live"])})
            while True:
                changed = await queue.get()
                await send({"live": json.loads(changed["live"])})
        finally:
            events.unsubscribe(queue)

    forwarding = asyncio.create_task(forward_live())
    recorder.attach(push)
    try:
        while True:
            info = await ws.receive_json()
            commands = await run_in_threadpool(recorder.check_in, info)
            want = await run_in_threadpool(settings.locked)
            if info.get("lockdown") is not None and info["lockdown"] != want:  # keep the recorder's copy in step
                commands.append("lockdown" if want else "no-lockdown")
            if commands:
                await send({"commands": commands})
    except WebSocketDisconnect:
        pass
    finally:
        recorder.detach(push)
        forwarding.cancel()


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


class SourceAuto(BaseModel):
    auto: bool
    start: str = "now"  # where to start when turning it on: now | week | all


def _source(name: str) -> sources.Source:
    if name not in sources.SOURCES:
        raise HTTPException(404, "No such source")
    return sources.SOURCES[name]


@app.get("/api/import/sources")
def import_sources():
    """Other note takers whose new meetings can come in by themselves, and how each one's sync is going."""
    return {"sources": sources.status()}


@app.put("/api/import/sources/{name}")
def set_import_source(name: str, body: SourceAuto):
    src = _source(name)
    cfg = settings.get_all()
    if body.auto:
        if not cfg[src.key_setting]:
            raise HTTPException(400, f"Add your {src.label} API key first")
        if settings.locked(cfg):
            raise HTTPException(409, settings.LOCKDOWN)
        if body.start not in sources.STARTS:
            raise HTTPException(422, f"start must be one of {', '.join(sources.STARTS)}")
        try:
            sources.check(name, cfg[src.key_setting])
        except importer.ImportError_ as e:
            raise HTTPException(422, str(e))
    sources.set_auto(name, body.auto, body.start)
    if body.auto:
        sources.sync_soon(name)
    return {"sources": sources.status()}


@app.delete("/api/import/sources/{name}")
def disconnect_import_source(name: str):
    """Turns it off and forgets its key. The meetings it brought in stay."""
    _source(name)
    sources.disconnect(name)
    return sources.connections()


@app.get("/api/import/connections")
def import_connections():
    """Everything that brings meetings in by itself (services and watched folders) as one list, and what could be
    connected."""
    return sources.connections()


@app.post("/api/import/sources/{name}/sync", status_code=202)
def sync_import_source(name: str):
    """Look for new meetings now instead of at the next round."""
    src = _source(name)
    cfg = settings.get_all()
    if not cfg[src.key_setting]:
        raise HTTPException(400, f"Add your {src.label} API key first")
    if settings.locked(cfg):
        raise HTTPException(409, settings.LOCKDOWN)
    sources.sync_soon(name)
    return {"sources": sources.status()}


class WatchFolder(BaseModel):
    path: str
    existing: bool = False  # also bring in what's in it now


@app.get("/api/import/folders")
def watched_folders():
    """Watched folders, how each is going, and folders other meeting apps save into that could be watched."""
    return watched.status()


@app.post("/api/import/folders")
def watch_folder(body: WatchFolder):
    try:
        added = watched.add(body.path, body.existing)
    except ValueError as e:
        raise HTTPException(422, str(e))
    threading.Thread(target=watched.scan_all, daemon=True, name="watch-first-look").start()
    return {**watched.status(), "added": added}


@app.post("/api/import/folders/scan", status_code=202)
def scan_folders():
    """Look through the watched folders now instead of at the next minute."""
    threading.Thread(target=watched.scan_all, daemon=True, name="watch-look-now").start()
    return watched.status()


@app.delete("/api/import/folders")
def unwatch_folder(path: str):
    watched.remove(path)
    return watched.status()


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
    """Download, check and install Ollama (the Mac or Windows app), or open it if it's installed but not running."""
    if sys.platform not in ("darwin", "win32"):
        raise HTTPException(409, "Installing Ollama from here only works on a Mac or Windows")
    if settings.locked():
        raise HTTPException(409, f"{settings.LOCKDOWN}, so Ollama can't be downloaded")
    if not ollama_setup.start_install(settings.get_all()):
        raise HTTPException(409, "Ollama is already being installed")
    return ollama_setup.status(settings.get_all())


@app.post("/api/ollama/pull", status_code=202)
def ollama_pull(body: PullRequest):
    cfg = settings.get_all()
    if settings.locked(cfg):
        raise HTTPException(409, f"{settings.LOCKDOWN}, so AI models can't be downloaded")
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


@app.post("/api/recorder/call-reminders", status_code=202)
def recorder_call_reminders(body: OpenAtLogin):
    """"Zoom call started. Record it?": on (which also forgets the apps you said not to ask about) or off."""
    if not recorder.request("call-reminders" if body.on else "no-call-reminders"):
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


@app.post("/api/recorder/go-back", status_code=202)
def recorder_go_back():
    """Install the version before this one (Settings → Updates → Go back), and skip this one until a newer
    version comes out. Meetings stay: the database works with the older version."""
    if live.active_count():
        raise HTTPException(409, "Finish the recording first, then go back")
    if not recorder.request("go-back"):
        raise HTTPException(409, "The menu bar recorder isn't running")
    return {"ok": True}


@app.post("/api/recorder/unskip-update", status_code=202)
def recorder_unskip_update():
    """Offer the version you went back from again."""
    if not recorder.request("unskip-update"):
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
    if settings.locked():
        raise HTTPException(409, f"{settings.LOCKDOWN}, so speech models can't be downloaded")
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
        return {"id": pid, "label": llm_engine.label(pid, cfg), "model": llm_engine.model_name(pid, cfg),
                "ready": ready, "reason": why, "local": llm_engine.is_local(pid, cfg)}

    remote = cfg["transcribe_engine"] == "remote"
    gemini = cfg["transcribe_engine"] == "gemini"
    cloudy = cfg["transcribe_engine"] == "trailmix"
    off = cloud.features_off(cfg)
    return {
        "summary": provider_info(cfg["summary_provider"]),
        "fallback": provider_info(cfg["summary_fallback"]) if cfg["summary_fallback"] != "none" else None,
        "transcription": {
            "engine": cfg["transcribe_engine"],
            "model": ("Trailmix Cloud" if cloudy else transcribe_gemini.MODEL if gemini else cfg["transcribe_model"] if remote
                      else local_engine.FINAL_MODEL),
            "live_model": None if gemini or cloudy else (cfg["transcribe_model"] if cfg["live_final"] else cfg["transcribe_live_model"] or cfg["transcribe_model"])
            if remote else (local_engine.FINAL_MODEL if cfg["live_final"] else local_engine.LIVE_MODEL),
            "url": cfg["transcribe_url"] if remote else None,
            "local_available": local_engine.available(),
            # the transcript made while recording is the final one (with Trailmix Cloud, at the Pro level)
            "live_final": (cfg["live_final"] and not gemini and not cloudy) or (cloudy and "live_captions" not in off),
            "gemini_ready": gemini_client.ready(cfg) and not settings.locked(cfg),  # Google can be used for speaker labels
            "remote_ready": preflight.engine_problem("remote", cfg) is None,
            "cloud_ready": preflight.engine_problem("trailmix", cfg) is None,
        },
        "cloud": {"signed_in": bool(cfg["cloud_token"]), "available": cloud.offered(cfg),
                  "account": cloud.account(cfg=cfg), "level": cloud.level(cfg), "off": off},
        "version": archive.APP_VERSION,
        "build": "app" if "/Contents/Resources/" in sys.executable else "source",  # the installed app, or from source
        "templates": templates.listing(),
        "providers": [{**provider_info(p), "kind": v["kind"]} for p, v in llm_engine.PROVIDERS.items()
                      if p != "trailmix" or cloud.offered(cfg)],
        "ask_before_ai": cfg["ask_before_ai"],
        "audio_retention_days": store.retention_days(),
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


@app.get("/api/recording-laws")
def recording_laws_table():
    return recording_laws.table()


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


# ── Trailmix Cloud ─────────────────────────────────────────────────────

class CloudPoll(BaseModel):
    code: str


def _cloud(fn, *args):
    try:
        return fn(*args)
    except cloud.CloudError as e:
        raise HTTPException(502, str(e))


@app.post("/api/cloud/link")
def cloud_link():
    """Starts signing this Mac in: {code, link_url, expires_in, interval}. The UI opens link_url and polls."""
    return _cloud(cloud.link)


@app.post("/api/cloud/poll")
def cloud_poll(body: CloudPoll):
    """{status: pending|expired|done, account?}: done once you've signed in in the browser."""
    return _cloud(cloud.poll, body.code)


@app.get("/api/cloud/account")
def cloud_account(refresh: bool = True):
    """The plan, level and hours ({} when signed out), fresh from the server unless `refresh` is false."""
    cfg = settings.get_all()
    acct = cloud.account(refresh=refresh, cfg=cfg)
    cfg = settings.get_all()
    return {"signed_in": bool(cfg["cloud_token"]), "account": acct, "level": cloud.level(cfg),
            "off": cloud.features_off(cfg)}


@app.post("/api/cloud/signout")
def cloud_signout():
    cloud.sign_out()
    return {"signed_in": False}


@app.post("/api/transcription/test")
def transcription_test():
    cfg = settings.get_all()
    if cfg["transcribe_engine"] == "trailmix":
        problem = preflight.engine_problem("trailmix", cfg)
        if problem:
            raise HTTPException(409, problem)
        acct = cloud.account(refresh=True, cfg=cfg)
        return {"ok": True, "message": f"Signed in to Trailmix Cloud ({acct.get('plan', 'unknown')} plan)"}
    if cfg["transcribe_engine"] == "gemini":
        try:
            return {"ok": True, "message": transcribe_gemini.check(gemini_client.credential(cfg))}
        except transcribe_gemini.GeminiError as e:
            raise HTTPException(502, str(e))
    if cfg["transcribe_engine"] != "remote":
        return {"ok": True, "message": f"Local transcription ({local_engine.NAME + ' available' if local_engine.available() else 'not installed'})"}
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
    cfg = settings.get_all()
    if body.engine:
        _check_engine(body.engine, cfg)  # say now, not after the meeting, that it can't be done
    wanted = body.workspace_id or cfg["current_workspace"]
    space = wanted if wanted and db.get_workspace(wanted) else None
    return {"id": db.create_meeting(title or f"Meeting {datetime.now():%b %d, %H:%M}", title_auto=not title,
                                    workspace_id=space, requested_engine=body.engine or None)}


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
    cfg = settings.get_all()
    if meeting.get("requested_engine"):
        cfg["transcribe_engine"] = meeting["requested_engine"]
    session = live.LiveSession(meeting_id, Path(meeting["audio_dir"]), draft=bool(draft), cfg=cfg,
                               client="helper" if client == "helper" else "web")
    send_lock = asyncio.Lock()
    stop_requested = asyncio.Event()

    async def send(msg: dict):
        async with send_lock:
            await ws.send_json(msg)

    session.request_stop = stop_requested.set  # only called on the event loop (async endpoint below)
    session.notify = send
    if session.captions_off_reason:
        await send({"type": "status", "message": session.captions_off_reason})

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

    drafter = asyncio.create_task(draft_loop()) if session.polls_drafts else None
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
        await session.stop_gemini()  # the last words of the live captions
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


@app.get("/api/events")
async def event_stream():
    """What's going on, as it changes (server-sent events; events.py): the menu bar recorder, recordings in
    progress, memory and disk, the speech models' downloads. One open stream per window, instead of asking
    every couple of seconds."""
    return StreamingResponse(events.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/live")
def live_recordings():
    """Recordings in progress, whichever app is capturing them (the web app or the menu bar helper)."""
    out = []
    for session in live.sessions():
        meeting = db.get_meeting(session.meeting_id)
        out.append({**session.describe(meeting["title"] if meeting else "Recording"), **live_notes.status(session.meeting_id),
                    "my_notes": meeting_text.my_notes(meeting) if meeting else []})
    return out


@app.get("/api/vocabulary")
def vocabulary_info():
    """Names and terms the models spell your way: the ones you added or confirmed (editable in Settings) and the
    ones Trailmix takes from your name, workspaces and the people you've named."""
    learned = questions.vocabulary()
    have = {w.lower() for w in learned}
    return {"learned": learned, "seeded": [w for w in questions.seeded() if w.lower() not in have]}


@app.get("/api/lockdown")
def lockdown_info():
    return {"on": settings.locked()}


@app.get("/api/stats")
def stats_info():
    """Anonymous usage stats: whether they're on, the latest events exactly as sent, and what today's summary
    contains (Settings → Privacy → See what's sent)."""
    cfg = settings.get_all()
    return {"available": telemetry.available(), "on": telemetry.enabled(cfg), "recent": telemetry.recent(),
            "daily": {**telemetry._base(), **telemetry.daily_properties(cfg)}}


@app.get("/api/live-notes")
def live_notes_plan():
    """Whether notes are written during meetings with your settings, why, and this Mac's power and memory."""
    return live_notes.decide(settings.get_all())


def _live_or_409(meeting_id: int) -> live.LiveSession:
    session = live.get(meeting_id)
    if not session:
        raise HTTPException(409, "That meeting isn't recording")
    return session


@app.post("/api/meetings/{meeting_id}/my-notes")
def add_my_note(meeting_id: int, body: MyNote):
    """A note you typed. While recording it's stamped with the moment; the notes AI works it into the notes."""
    m = _get_or_404(meeting_id)
    text = body.text.strip()[:4000]
    if not text:
        raise HTTPException(422, "Type something first")
    session = live.get(meeting_id)
    notes = meeting_text.my_notes(m) + [{"t": round(session.duration, 1) if session else None, "text": text}]
    db.update_meeting(meeting_id, why="you edited your notes", my_notes_json=notes)
    return notes


@app.put("/api/meetings/{meeting_id}/my-notes")
def replace_my_notes(meeting_id: int, body: MyNotes):
    """Your notes after editing them, saved as you type (one history entry per stretch of typing). Each is a line of
    markdown; leading spaces nest a list item, so only the end is trimmed. Regenerate rewrites the notes with them."""
    _get_or_404(meeting_id)
    notes = [{"t": n.t, "text": n.text.rstrip()[:4000]} for n in body.notes if n.text.strip()]
    db.update_meeting(meeting_id, why="you edited your notes", merge_s=120, my_notes_json=notes or None)
    return notes


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


@app.get("/api/meetings/{meeting_id}/catch-up")
def catch_up_plan(meeting_id: int):
    """Before asking about a meeting that's still recording: which AI would answer, about what a question costs, any
    warning about memory on this Mac, and the questions asked so far (catch_up.py)."""
    session = _live_or_409(meeting_id)
    return catch_up.plan(session, _get_or_404(meeting_id), settings.get_all())


@app.post("/api/meetings/{meeting_id}/catch-up")
def catch_up_ask(meeting_id: int, body: CatchUpRequest):
    """"What did I miss?", "Explain that", "What's been decided?" or your question, answered from the meeting so far."""
    session = _live_or_409(meeting_id)
    try:
        return catch_up.ask(session, _get_or_404(meeting_id), settings.get_all(), body.kind, body.question[:2000])
    except catch_up.NotReady as e:
        raise HTTPException(409, str(e))
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/api/meetings/{meeting_id}/stop", status_code=202)
async def stop_live(meeting_id: int):
    """Stops a recording, even one another app is capturing. That app is told, then processing starts."""
    _live_or_409(meeting_id).request_stop()
    return {"ok": True}


@app.patch("/api/meetings/{meeting_id}")
def edit_meeting(meeting_id: int, body: MeetingPatch):
    """Renames a meeting, or moves it to when it really happened (an imported one often has no date)."""
    m = _get_or_404(meeting_id)
    fields = {}
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "Title cannot be empty")
        fields.update(title=title, title_auto=0)  # a hand-picked title is never overwritten
    if body.created_at is not None:
        when = importer._parse_time(body.created_at.strip())
        if not when:
            raise HTTPException(422, "That isn't a date Trailmix can read")
        if when.tzinfo is None:
            when = when.astimezone()  # a plain date or time means local time
        if not datetime(1990, 1, 1, tzinfo=timezone.utc) <= when <= datetime.now(timezone.utc) + timedelta(days=2):
            raise HTTPException(422, "Pick a date that isn't in the future")
        fields["created_at"] = importer._iso(when)
        if not m.get("imported_at"):  # its audio's 30 days still count from when it came in, not the new date
            fields["imported_at"] = m["created_at"]
    if fields:
        db.update_meeting(meeting_id, why="you changed the " + " and ".join(
            {"title": "title", "created_at": "date"}[k] for k in ("title", "created_at") if k in fields), **fields)
        _reexport(meeting_id)
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/guess-date")
def guess_date(meeting_id: int):
    """The AI reads the transcript for clues to when the meeting happened. Only a suggestion: nothing changes."""
    m = _get_or_404(meeting_id)
    if not m["transcript"]:
        raise HTTPException(409, "This meeting has no transcript to read")
    cfg = settings.get_all()
    _local_llm_paused(cfg)
    try:
        return meeting_date.guess(m, cfg)
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))


@app.put("/api/meetings/{meeting_id}/speakers")
def set_speakers(meeting_id: int, names: dict[str, str]):
    _get_or_404(meeting_id)
    allowed = {"You", "Them", *meeting_text.speaker_labels(_get_or_404(meeting_id))}
    clean = {k: v.strip()[:60] for k, v in names.items() if k in allowed}
    db.update_meeting(meeting_id, why="you renamed speakers", speaker_names_json=clean)
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
def retranscribe(meeting_id: int, body: RetranscribeRequest | None = None):
    """Transcribe again from the saved audio (say, after an update that transcribes better, or with a different
    engine: "gemini" labels who said what), then summarize. Speaker names, flags and tasks you ticked off are
    kept; the summary is rewritten."""
    meeting = _get_or_404(meeting_id)
    engine = body.engine if body else None
    cfg = settings.get_all()
    if engine and engine not in settings.CHOICES["transcribe_engine"]:
        raise HTTPException(422, "Unknown transcription engine")
    if (engine or cfg["transcribe_engine"]) in ("gemini", "trailmix"):  # fail now, not after the old notes are cleared
        problem = preflight.engine_problem(engine or cfg["transcribe_engine"], cfg)
        if problem:
            raise HTTPException(409, problem)
    if meeting["status"] not in ("done", "error", "ready_summarize"):
        raise HTTPException(409, "This meeting is still being processed")
    if meeting["audio_deleted"] or not store.audio_bytes(meeting):
        raise HTTPException(409, "The audio for this meeting has been deleted, so it can't be transcribed again")
    db.update_meeting(meeting_id, status="queued", wait_reason=None, error=None, transcribed=0, summary=None,
                      summary_error=None, live_json=None,  # the whole recording, from scratch
                      requested_engine=engine, requested_transcribe_model=None)  # (None: with whatever Settings says now)
    pipeline.enqueue(meeting_id, go=frozenset({"transcribe", "summarize"}))
    return {"ok": True}


# ── Transcript versions: try another speech model, compare, switch ─────

class TrialRequest(BaseModel):
    engine: str                     # local | remote | gemini
    model: str | None = None        # e.g. a newer Google model, or whisper-1 / gpt-4o-transcribe for a remote endpoint
    apply: bool = False             # use the result straight away (otherwise it's kept to compare)
    summarize: bool = False         # with apply: write the notes again from it
    mix_tracks: bool | None = None  # Gemini: one mixed recording (half the cost) or each track; None: as in Settings
    batch: bool = False             # an economy model only: as a half-price Batch job, answers back later


class ApplyRequest(BaseModel):
    summarize: bool = False


def _check_engine(engine: str, cfg: dict) -> None:
    """Fails now, in plain words, if this engine can't be used (rather than after the job was queued)."""
    problem = preflight.engine_problem(engine, cfg)
    if problem:
        raise HTTPException(422 if problem.startswith("Unknown") else 409, problem)


def _version_summary(v: dict) -> dict:
    segments = json.loads(v["segments_json"]) if v.get("segments_json") else None
    return {"id": v["id"], "created_at": v["created_at"], "engine": v["engine"], "model": v["model"],
            "made_with": v["made_with"], "source": v["source"], "applied": bool(v["applied"]), "seconds": v["seconds"],
            "duration_sec": v["duration_sec"], "note": v["note"], **compare.stats(segments, v["transcript"])}


@app.get("/api/meetings/{meeting_id}/versions")
def transcript_versions(meeting_id: int):
    """Every transcript made of this meeting, oldest first, and the trial in progress (if any)."""
    _get_or_404(meeting_id)
    return {"versions": [_version_summary(v) for v in db.transcript_versions(meeting_id)],
            "trial": pipeline.trial_status(meeting_id)}


@app.get("/api/meetings/{meeting_id}/versions/{version_id}")
def transcript_version(meeting_id: int, version_id: int):
    _get_or_404(meeting_id)
    v = db.get_transcript_version(version_id)
    if not v or v["meeting_id"] != meeting_id:
        raise HTTPException(404, "No such transcript")
    return {**_version_summary(v), "transcript": v["transcript"],
            "segments": json.loads(v["segments_json"]) if v["segments_json"] else None}


@app.post("/api/meetings/{meeting_id}/versions/{version_id}/apply", status_code=202)
def apply_transcript_version(meeting_id: int, version_id: int, body: ApplyRequest | None = None):
    """Switches the meeting to this transcript. The one it replaces stays in the versions."""
    meeting = _get_or_404(meeting_id)
    if meeting["status"] not in ("done", "error", "ready_summarize"):
        raise HTTPException(409, "This meeting is still being processed")
    try:
        pipeline.apply_version(meeting_id, version_id, summarize=bool(body and body.summarize))
    except ValueError as e:
        raise HTTPException(404, str(e))
    _reexport(meeting_id)
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/trial", status_code=202)
def start_trial(meeting_id: int, body: TrialRequest):
    """Transcribes the saved audio again with another engine or model and keeps the result as a new version, to
    compare with the one in use. The meeting itself isn't touched unless `apply`."""
    meeting = _get_or_404(meeting_id)
    if meeting["audio_deleted"] or not store.audio_bytes(meeting):
        raise HTTPException(409, "The audio for this meeting has been deleted, so it can't be transcribed again")
    if not meeting["transcript"]:
        raise HTTPException(409, "This meeting has no transcript yet")
    if meeting["status"] not in ("done", "error", "ready_summarize"):
        raise HTTPException(409, "This meeting is still being processed")
    current = pipeline.trial_status(meeting_id)
    if current and current["state"] in ("queued", "running"):
        raise HTTPException(409, "Another transcript is already being made for this meeting")
    cfg = settings.get_all()
    _check_engine(body.engine, cfg)
    if body.batch:
        return _submit_transcript_batch([meeting_id], body, cfg)
    pipeline.enqueue_trial(meeting_id, body.engine, body.model, apply=body.apply, summarize=body.summarize,
                           mix_tracks=body.mix_tracks)
    return pipeline.trial_status(meeting_id)


def _submit_transcript_batch(ids: list[int], body, cfg: dict) -> dict:
    """Sends these meetings' audio to Google to be transcribed by an economy model at half price (a Batch job)."""
    if body.engine != "gemini":
        raise HTTPException(422, "Only Google Gemini can run as a batch job")
    model = body.model or cfg["gemini_transcribe_model"]
    try:
        return {"batch": True, **batch_jobs.submit_transcripts(ids, cfg, model, apply=body.apply)}
    except batch_jobs.NotReady as e:
        raise HTTPException(409, str(e))
    except (gemini_batch.BatchError, transcribe_gemini.GeminiError) as e:
        raise HTTPException(502, str(e))


@app.get("/api/meetings/{meeting_id}/trial")
def trial_status(meeting_id: int):
    _get_or_404(meeting_id)
    return pipeline.trial_status(meeting_id)


@app.get("/api/meetings/{meeting_id}/compare")
def compare_versions(meeting_id: int, a: int, b: int):
    """How two transcripts of this meeting differ: agreement, and the words that differ."""
    _get_or_404(meeting_id)
    va, vb = db.get_transcript_version(a), db.get_transcript_version(b)
    if not va or not vb or va["meeting_id"] != meeting_id or vb["meeting_id"] != meeting_id:
        raise HTTPException(404, "No such transcript")
    spoken = [compare.plain(json.loads(v["segments_json"]) if v["segments_json"] else None, v["transcript"]) for v in (va, vb)]
    return {"a": _version_summary(va), "b": _version_summary(vb), **compare.diff(*spoken),
            "inline": compare.inline(*spoken)}


@app.get("/api/meetings/{meeting_id}/history")
def meeting_history(meeting_id: int, field: str | None = None):
    """The receipt: every change to this meeting (when, what, from what to what, and why), newest first."""
    _get_or_404(meeting_id)
    return db.history_for(meeting_id, field)


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
        summary_error=None, requested_provider=body.provider, requested_template=body.template, requested_model=None,
    )
    pipeline.enqueue(meeting_id, go=frozenset({"summarize"}))
    return {"ok": True}


class PreflightRequest(BaseModel):
    meeting_ids: list[int]
    engine: str | None = None             # speech engine (None: Settings')
    transcribe_model: str | None = None
    provider: str | None = None           # notes provider (None: Settings')
    model: str | None = None              # its model (None: Settings')


class ProcessRequest(PreflightRequest):
    template: str | None = None
    remember: bool = False                # also make these choices the defaults in Settings


_PROCESSABLE = ("done", "error", "ready_transcribe", "ready_summarize")


def _preflight_meetings(ids: list[int]) -> list[dict]:
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise HTTPException(422, "Pick at least one meeting")
    found = db.get_meetings(ids)
    if len(found) != len(ids):
        raise HTTPException(404, "Meeting not found")
    return found


@app.post("/api/preflight")
def preflight_estimate(body: PreflightRequest):
    """Which AI could process these meetings and what the chosen one would take, before anything runs."""
    if body.provider and body.provider not in llm_engine.PROVIDERS:
        raise HTTPException(422, "Unknown provider")
    if body.engine and body.engine not in settings.CHOICES["transcribe_engine"]:
        raise HTTPException(422, "Unknown transcription engine")
    meetings = _preflight_meetings(body.meeting_ids)
    return {**preflight.preflight(meetings, settings.get_all(), body.engine, body.transcribe_model, body.provider,
                                  body.model), "recording": live.active_count() > 0}


@app.post("/api/process", status_code=202)
def process(body: ProcessRequest):
    """Transcribes (if needed) and writes notes for these meetings with the AI you picked. Notes already written are
    written again; ticked-off tasks stay ticked."""
    cfg = settings.get_all()
    meetings = _preflight_meetings(body.meeting_ids)
    provider = body.provider or cfg["summary_provider"]
    if provider not in llm_engine.PROVIDERS:
        raise HTTPException(422, "Unknown provider")
    if body.template and body.template not in templates.TEMPLATES:
        raise HTTPException(422, "Unknown template")
    check = dict(cfg)
    if body.model:
        check[llm_engine.model_key(provider)] = body.model
    ready, why = llm_engine.configured(provider, check)
    if not ready:
        raise HTTPException(409, why)
    untranscribed = [m for m in meetings if not m["transcribed"]]
    engine = body.engine or cfg["transcribe_engine"]
    if untranscribed:
        _check_engine(engine, cfg)
        if any(m["audio_deleted"] or not store.audio_bytes(m) for m in untranscribed):
            raise HTTPException(409, "The audio for a meeting that still needs a transcript has been deleted")
    # (an import waiting for this choice may not have reached ready_* yet; its queued run then uses this choice too)
    busy = [m["title"] for m in meetings if m["status"] not in _PROCESSABLE and not m["ask_first"]]
    if busy:
        raise HTTPException(409, f"Still being processed: {', '.join(busy)}")
    for m in meetings:
        fields = dict(status="queued", wait_reason=None, error=None, summary=None, summary_provider=None,
                      summary_error=None, ask_first=0, requested_provider=provider, requested_model=body.model or None,
                      requested_template=body.template or None)
        if not m["transcribed"]:
            fields.update(requested_engine=engine, requested_transcribe_model=body.transcribe_model or None)
        db.update_meeting(m["id"], **fields)
        pipeline.enqueue(m["id"], go=frozenset({"transcribe", "summarize"}))
    if body.remember:
        defaults = {"summary_provider": provider}
        if body.model:
            defaults[llm_engine.model_key(provider)] = body.model
        if untranscribed:
            defaults["transcribe_engine"] = engine
            if body.transcribe_model and engine == "gemini":
                defaults["gemini_transcribe_model"] = body.transcribe_model
        settings.update(defaults)
    return {"queued": len(meetings)}


class MergeRequest(BaseModel):
    pairs: list[list[int]] = []   # [[a, b], ...]; empty with `all`: every pair "Find duplicates" sees
    all: bool = False


class ApartRequest(BaseModel):
    a: int
    b: int


@app.get("/api/duplicates")
def find_duplicates():
    """Meetings that look like the same meeting twice (Trailmix and Granola both had it), with which one would stay."""
    return {"pairs": duplicates.find_all()}


@app.post("/api/duplicates/merge")
def merge_duplicates(body: MergeRequest):
    pairs = [(p["keep"]["id"], p["other"]["id"]) for p in duplicates.find_all()] if body.all else \
        [tuple(p) for p in body.pairs if len(p) == 2]
    kept, problems = [], []
    for a, b in pairs:
        try:
            kept.append(duplicates.merge(a, b))
        except ValueError as e:
            problems.append(str(e))
    return {"merged": len(kept), "kept": kept, "problems": problems}


@app.post("/api/duplicates/apart", status_code=204)
def keep_duplicates_apart(body: ApartRequest):
    """Not the same meeting after all: never suggested or merged again."""
    duplicates.keep_apart(body.a, body.b)


@app.post("/api/meetings/{meeting_id}/unmerge")
def unmerge(meeting_id: int):
    """Undoes the last merge into this meeting: the other copy is its own meeting again."""
    _get_or_404(meeting_id)
    try:
        return {"id": duplicates.unmerge(meeting_id)}
    except ValueError as e:
        raise HTTPException(409, str(e))


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
        with llm_engine.purpose("questions", meeting_id):
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
    if body.template and body.template not in templates.TEMPLATES:
        raise HTTPException(422, "Unknown note style")
    db.update_workspace(workspace_id, name=name, color=body.color,
                        about=body.about.strip()[:500] if body.about is not None else None, position=body.position,
                        template=body.template)
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
    db.update_meeting(meeting_id, why="you moved it to another workspace", workspace_id=body.workspace_id, workspace_auto=0)
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

def _upgradable(cfg: dict, current: str | None = None) -> dict:
    """What the AI you have now (or `current`, e.g. the Gemini model a batch job would use) would do better than the
    model that did it, per job."""
    current = current or llm_engine.model_for(llm_engine.chain("auto", cfg)[0], cfg)
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, summary, summary_provider, summary_model, tags_json, tags_model, workspace_id,
                      workspace_auto, workspace_model, status FROM meetings WHERE status = 'done'""").fetchall()
    ours = set(llm_engine.PROVIDERS)
    notes = [r for r in rows if r["summary"] and r["summary_provider"] in ours  # not notes imported from another app
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


def _speech_engines(cfg: dict) -> list[dict]:
    """The speech engines you could use right now, each with how it's rated: [{engine, model, score}]."""
    out = []
    if gemini_client.ready(cfg) and not settings.locked(cfg):
        out.append({"engine": "gemini", "model": cfg["gemini_transcribe_model"]})
    if cfg["transcribe_url"] and (not settings.locked(cfg) or settings.on_this_computer(cfg["transcribe_url"])):
        out.append({"engine": "remote", "model": cfg["transcribe_model"]})
    if local_engine.available():
        out.append({"engine": "local", "model": local_engine.model_label(local_engine.FINAL_MODEL)})
    for e in out:
        e["score"] = model_ranks.score(e["model"])
    return out


def _transcripts_upgradable(cfg: dict) -> dict:
    """Transcripts a speech engine you have now would do better than the one that made them (only where the
    audio is still kept, since it has to be transcribed again)."""
    engines = [e for e in _speech_engines(cfg) if e["score"] is not None]
    best = max(engines, key=lambda e: e["score"], default=None)
    rows = []
    if best:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id, transcribed_with FROM meetings WHERE status IN ('done', 'ready_summarize') AND transcript IS NOT NULL "
                "AND audio_deleted = 0 AND audio_dir IS NOT NULL").fetchall()
        rows = [r for r in rows if model_ranks.better(best["model"], model_ranks.speech_model(r["transcribed_with"]))]
    counts: dict[str, int] = {}
    for r in rows:
        name = model_ranks.speech_model(r["transcribed_with"]) or "an earlier engine"
        counts[name] = counts.get(name, 0) + 1
    return {"best": best, "engines": engines, "count": len(rows), "ids": [r["id"] for r in rows], "from": counts}


@app.get("/api/upgrades")
def upgrades():
    """Meetings whose notes, tags or workspace came from a weaker model than the one you have now."""
    cfg = settings.get_all()
    data = _upgradable(cfg)
    data["notes"].pop("ids")
    # The same notes, rewritten by Gemini as a half-price batch job (when there's a key and Lockdown is off).
    gemini = _upgradable(cfg, cfg["gemini_model"])["notes"]
    data["notes_batch"] = {"ready": gemini_client.ready(cfg) and not settings.locked(cfg), "model": cfg["gemini_model"],
                           "score": model_ranks.score(cfg["gemini_model"]), "count": gemini["count"], "from": gemini["from"]}
    transcripts = _transcripts_upgradable(cfg)
    transcripts.pop("ids")
    data["transcripts"] = transcripts
    return data


def _estimate(meetings: list[dict], engine: str, model: str | None, cfg: dict) -> dict:
    """What redoing these meetings' transcripts would send and cost: {meetings: [{id, title, minutes, cost}],
    minutes, cost (None if the price isn't known), per_minute, as_of}. An upper bound: a silent track isn't sent."""
    model = model or (cfg["gemini_transcribe_model"] if engine == "gemini" else cfg["transcribe_model"] if engine == "remote" else None)
    mixed = engine in ("gemini", "trailmix") and bool(cfg["gemini_mix_tracks"])  # your mic and the call as one recording
    rows = []
    for m in meetings:
        minutes = pricing.meeting_minutes(m["duration_sec"], bool(m["has_system"]), mixed=mixed)
        rows.append({"id": m["id"], "title": m["title"], "minutes": round(minutes, 1),
                     "cost": pricing.cost(minutes, engine, model)})
    known = all(r["cost"] is not None for r in rows)
    return {"meetings": rows, "minutes": round(sum(r["minutes"] for r in rows), 1),
            "cost": round(sum(r["cost"] for r in rows), 4) if known else None,
            "per_minute": pricing.per_minute(engine, model), "model": model, "as_of": pricing.AS_OF, "mixed": mixed}


@app.get("/api/meetings/{meeting_id}/trial/estimate")
def trial_estimate(meeting_id: int, engine: str, model: str | None = None, mix_tracks: bool | None = None):
    """What transcribing this meeting's audio again with this engine would cost, roughly."""
    m = _get_or_404(meeting_id)
    cfg = settings.get_all()
    if mix_tracks is not None:
        cfg["gemini_mix_tracks"] = mix_tracks
    return _estimate([m], engine, model, cfg)


@app.get("/api/usage")
def usage(days: int = 30):
    """What the paid AI was asked to do in the last `days` days (audio minutes, tokens), and about what it cost."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    since = start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 1000:03d}Z"  # as the database writes them
    rows = db.usage_since(since)
    return {"days": days, "rows": rows, "cost": round(sum(r["cost"] or 0 for r in rows), 4),
            "unpriced": sum(r["unpriced"] for r in rows), "as_of": pricing.AS_OF}


class NotesBatchRequest(BaseModel):
    limit: int = 5                    # at most this many meetings (newest first)
    meeting_ids: list[int] | None = None   # None: the meetings Gemini's notes would beat
    dry_run: bool = False


@app.post("/api/upgrades/notes/batch", status_code=202)
def upgrade_notes_batch(body: NotesBatchRequest):
    """Has Gemini rewrite old notes as a half-price Batch job: the answers come back later (often within minutes,
    at most about a day) and are put in the meetings by themselves; the old notes stay in the history."""
    cfg = settings.get_all()
    if settings.locked(cfg):
        raise HTTPException(409, f"{settings.LOCKDOWN}, so nothing can be sent to Google")
    if not gemini_client.ready(cfg):
        raise HTTPException(409, "Add a Google API key first (Settings → AI providers)")
    ids = body.meeting_ids if body.meeting_ids is not None else _upgradable(cfg, cfg["gemini_model"])["notes"]["ids"]
    newest = sorted((m for m in db.get_meetings(ids) if m), key=lambda m: m["created_at"], reverse=True)[:max(1, body.limit)]
    ids = [m["id"] for m in newest]
    if body.dry_run:
        requests, _, skipped = batch_jobs.prepare(ids, cfg)
        return {**batch_jobs.estimate(requests, cfg["gemini_model"]), "skipped": skipped, "limit": body.limit}
    try:
        return batch_jobs.submit_notes(ids, cfg)
    except batch_jobs.NotReady as e:
        raise HTTPException(409, str(e))
    except gemini_batch.BatchError as e:
        raise HTTPException(502, str(e))


@app.get("/api/batches")
def list_batches():
    """Batch jobs sent to Google: where each stands, and what became of its answers."""
    out = []
    for j in db.batch_jobs():
        out.append({"id": j["id"], "kind": j["kind"], "model": j["model"], "state": j["state"], "created_at": j["created_at"],
                    "finished_at": j["finished_at"], "meetings": len(json.loads(j["items_json"])), "applied": bool(j["applied"]),
                    "est_cost": j["est_cost"], "error": j["error"],
                    "result": json.loads(j["result_json"]) if j["result_json"] else None})
    return out


@app.post("/api/batches/check")
def check_batches():
    changed = batch_jobs.check()
    return {"changed": changed, "jobs": list_batches()}


@app.post("/api/batches/{job_id}/cancel")
def cancel_batch(job_id: int):
    try:
        batch_jobs.cancel(job_id, settings.get_all())
    except batch_jobs.NotReady as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


class TrialsRequest(BaseModel):
    engine: str
    model: str | None = None
    meeting_ids: list[int] | None = None   # None: see scope
    scope: str = "better"                  # "better": the meetings this engine would transcribe better than the one that did;
                                           # "all": every meeting whose audio is still kept (to compare engines)
    batch: bool = False                    # an economy model only: as a half-price Batch job, answers back later
    limit: int = 3                         # at most this many meetings (newest first): spend a little at a time
    dry_run: bool = False                  # only say which meetings, how much audio, and about what it would cost
    apply: bool = False                    # use each result when it's made (otherwise they're kept to compare)
    summarize: bool = False                # with apply: write each meeting's notes again from the new transcript


@app.post("/api/transcript-trials", status_code=202)
def start_trials(body: TrialsRequest):
    """Redoes transcripts with another engine or model, one meeting after another, keeping each result as a
    version to compare (or using it, with `apply`). `meeting_ids` None means the meetings this engine would
    transcribe better than the engine that did (see /api/upgrades)."""
    cfg = settings.get_all()
    _check_engine(body.engine, cfg)
    model = body.model or (cfg["gemini_transcribe_model"] if body.engine == "gemini" else None)
    if body.meeting_ids is not None:
        ids = body.meeting_ids
    elif body.scope == "all":
        with db.connect() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM meetings WHERE status IN ('done', 'ready_summarize') AND transcript IS NOT NULL AND audio_deleted = 0 "
                "AND audio_dir IS NOT NULL ORDER BY id DESC").fetchall()]
    else:
        ids = _transcripts_upgradable(cfg)["ids"]
    eligible = []
    for meeting_id in ids:
        m = db.get_meeting(meeting_id)
        busy = pipeline.trial_status(meeting_id)
        if (not m or not m["transcript"] or m["audio_deleted"] or not store.audio_bytes(m)
                or m["status"] not in ("done", "error", "ready_summarize") or (busy and busy["state"] in ("queued", "running"))):
            continue
        eligible.append(m)
    eligible = sorted(eligible, key=lambda m: m["created_at"], reverse=True)[:max(1, body.limit)]
    estimate = _estimate(eligible, body.engine, model, cfg)
    if body.batch and estimate["cost"] is not None:
        estimate["cost"] = round(estimate["cost"] * pricing.BATCH_DISCOUNT, 4)
    if body.dry_run:
        return {**estimate, "available": len(ids), "limit": body.limit}
    if body.batch:
        out = _submit_transcript_batch([m["id"] for m in eligible], body, cfg)
        return {"queued": out["submitted"], "skipped": len(ids) - out["submitted"], **estimate}
    for m in eligible:
        pipeline.enqueue_trial(m["id"], body.engine, body.model, apply=body.apply, summarize=body.summarize)
    return {"queued": len(eligible), "skipped": len(ids) - len(eligible), **estimate}


@app.get("/api/transcript-trials")
def list_trials():
    """Every transcript redone (or being redone) since Trailmix started: where each stands, and how much its
    words agree with the transcript the meeting has."""
    out = []
    for meeting_id, trial in pipeline.all_trials().items():
        m = db.get_meeting(meeting_id)
        if m:
            out.append({"meeting_id": meeting_id, "title": m["title"], "current": m["transcribed_with"], **trial})
    counts: dict[str, int] = {}
    for t in out:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    return {"trials": out, "counts": counts}


@app.delete("/api/transcript-trials")
def cancel_trials():
    return {"cancelled": pipeline.cancel_trials()}


@app.post("/api/upgrades/notes", status_code=202)
def upgrade_notes():
    """Rewrites the notes made by a weaker model, one meeting after another (your ticked tasks stay ticked)."""
    cfg = settings.get_all()
    _local_llm_paused(cfg)
    ids = _upgradable(cfg)["notes"]["ids"]
    for meeting_id in ids:
        db.update_meeting(meeting_id, status="queued", wait_reason=None, summary=None, summary_error=None,
                          requested_provider="auto", requested_model=None)
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
        with llm_engine.purpose("notes", meeting_id):
            title, _ = llm_engine.run_chain("auto", cfg, lambda pid: llm_engine.clean_title(
                llm_engine.generate(pid, cfg, llm_engine.TITLE_PROMPT.format(summary=notes))))
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))
    if not title:
        raise HTTPException(502, "The AI didn't come up with a usable title; try again")
    db.update_meeting(meeting_id, why="a new title from its notes", title=title, title_auto=1)
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
        local = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).astimezone()
        when = f"{local:%b} {local.day}, {local:%Y}"
        body_text = m["summary"] or meeting_text.transcript_text(m, cfg)
        if material := attachments.context_block(m["id"], budget // 3):
            body_text += "\n\n" + material
        blocks.append(f"[M{m['id']}] {m['title']} ({when})\n{meeting_text.excerpt(body_text, question, budget)}")
    context = "MEETINGS (each tagged like [M12]):\n\n" + "\n\n---\n\n".join(blocks)
    try:
        with llm_engine.purpose("questions"):
            answer, used = llm_engine.ask(question, context, cfg, cite_meetings=True)
    except llm_engine.LLMError as e:
        raise HTTPException(502, str(e))
    return {
        "answer": answer,
        "provider": used,
        "sources": [{"id": m["id"], "title": m["title"], "created_at": m["created_at"]} for m in meetings],
    }


# ── Attachments ────────────────────────────────────────────────────────

_PENDING = re.compile(r"[0-9a-f]{32}")


def _attachment_or_404(attachment_id: int) -> dict:
    att = db.get_attachment(attachment_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    return att


def _pending_path(pending_id: str) -> Path:
    if not _PENDING.fullmatch(pending_id):
        raise HTTPException(400, "Bad upload id")
    found = next((p for p in (db.DATA_DIR / "incoming").glob(f"{pending_id}.*") if ".small." not in p.name), None)
    if not found:
        raise HTTPException(404, "That upload is gone; add the file again")
    return found


@app.post("/api/meetings/{meeting_id}/attachments")
async def add_attachment(meeting_id: int, request: Request, name: str, searchable: bool = False):
    """A file, sent as the request body (streamed to disk). A big one isn't kept yet: the reply is {pending} with
    its size and about how small it could get, and /finish says whether to shrink it."""
    _get_or_404(meeting_id)
    temp = file_import.temp_path(name)
    try:
        with temp.open("wb") as out:
            async for chunk in request.stream():
                out.write(chunk)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    cfg = settings.get_all()
    if attachments.needs_choice(temp, name, cfg):
        try:
            small, _ = await run_in_threadpool(attachments.estimate, temp, name)
        except attachments.AttachmentError:
            small = None  # can't be shrunk: keep it as it is
        if small is not None and small > temp.stat().st_size * 0.9:
            small = None  # shrinking would hardly help (a PDF that's mostly text): don't ask
        if small is not None and cfg["attachments_always_shrink"]:
            return {"attachment": attachments.present(db.get_attachment(await run_in_threadpool(
                attachments.store, meeting_id, temp, name, searchable, True, attachments.made_earlier(temp))))}
        if small is not None:
            return {"pending": {"id": temp.stem, "name": name, "kind": attachments.kind_of(name),
                                "size": temp.stat().st_size, "estimate": small, "searchable": searchable}}
    att_id = await run_in_threadpool(attachments.store, meeting_id, temp, name, searchable, False,
                                     attachments.made_earlier(temp))
    return {"attachment": attachments.present(db.get_attachment(att_id))}


class AttachmentFinish(BaseModel):
    name: str
    shrink: bool
    searchable: bool = False


@app.post("/api/meetings/{meeting_id}/attachments/pending/{pending_id}")
def finish_attachment(meeting_id: int, pending_id: str, body: AttachmentFinish):
    """Keeps a big upload, shrunk or as it is."""
    _get_or_404(meeting_id)
    temp = _pending_path(pending_id)
    try:
        att_id = attachments.store(meeting_id, temp, body.name, body.searchable, body.shrink,
                                   attachments.made_earlier(temp))
    except attachments.AttachmentError as e:
        raise HTTPException(422, str(e))
    return attachments.present(db.get_attachment(att_id))


@app.delete("/api/meetings/{meeting_id}/attachments/pending/{pending_id}", status_code=204)
def cancel_attachment(meeting_id: int, pending_id: str):
    temp = _pending_path(pending_id)
    for p in (temp, attachments.made_earlier(temp)):
        if p:
            p.unlink(missing_ok=True)


class AttachmentLink(BaseModel):
    url: str
    name: str = ""
    searchable: bool = False


@app.post("/api/meetings/{meeting_id}/attachments/link")
def add_attachment_link(meeting_id: int, body: AttachmentLink):
    _get_or_404(meeting_id)
    try:
        att_id = attachments.add_link(meeting_id, body.url, body.name, body.searchable)
    except attachments.AttachmentError as e:
        raise HTTPException(422, str(e))
    return attachments.present(db.get_attachment(att_id))


class AttachmentPatch(BaseModel):
    searchable: bool | None = None
    name: str | None = None
    caption: str | None = None


@app.patch("/api/attachments/{attachment_id}")
def update_attachment(attachment_id: int, body: AttachmentPatch):
    _attachment_or_404(attachment_id)
    fields = {k: v.strip() for k, v in (("name", body.name), ("caption", body.caption)) if v is not None}
    if fields.get("name") == "":
        raise HTTPException(422, "A name can't be empty")
    db.update_attachment(attachment_id, **fields)
    if body.searchable is not None:
        attachments.set_searchable(attachment_id, body.searchable)
    return attachments.present(db.get_attachment(attachment_id))


@app.post("/api/attachments/{attachment_id}/shrink")
def shrink_attachment(attachment_id: int):
    _attachment_or_404(attachment_id)
    try:
        return attachments.present(attachments.shrink_existing(attachment_id))
    except attachments.AttachmentError as e:
        raise HTTPException(422, str(e))


@app.post("/api/attachments/{attachment_id}/read")
def reread_attachment(attachment_id: int):
    """Try reading it again (after sharing a Google deck, say, or switching who reads attachments)."""
    att = _attachment_or_404(attachment_id)
    if not att["searchable"]:
        raise HTTPException(409, "Add it to the searchable notes first")
    attachments.read_later(attachment_id)
    return attachments.present(db.get_attachment(attachment_id))


@app.delete("/api/attachments/{attachment_id}", status_code=204)
def delete_attachment(attachment_id: int):
    _attachment_or_404(attachment_id)
    attachments.remove(attachment_id)


@app.get("/api/attachments/{attachment_id}/file")
def attachment_file(attachment_id: int, download: bool = False):
    att = _attachment_or_404(attachment_id)
    path = attachments.file_of(att)
    if not path or not path.exists():
        raise HTTPException(404, "No file for this attachment")
    return FileResponse(path, media_type=att["mime"], filename=att["name"],
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/attachments/{attachment_id}/preview")
def attachment_preview(attachment_id: int):
    path = attachments.preview_of(_attachment_or_404(attachment_id))
    if not path or not path.exists():
        raise HTTPException(404, "No preview")
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/attachments/{attachment_id}/reveal")
def reveal_attachment(attachment_id: int):
    """Where it is on disk, for "Show in Finder" in the Mac app."""
    path = attachments.file_of(_attachment_or_404(attachment_id))
    if not path or not path.exists():
        raise HTTPException(404, "No file for this attachment")
    return {"path": str(path)}


# ── Tasks ──────────────────────────────────────────────────────────────

@app.get("/api/tasks")
def list_tasks():
    return db.all_tasks()


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskPatch):
    meeting_id = db.update_task(task_id, done=body.done, deleted=body.deleted)
    if meeting_id is None:
        raise HTTPException(404, "Task not found")
    _reexport(meeting_id)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    """Hides the task without ticking it off. It stays hidden when the notes are rewritten, and is left out of
    exports; PATCH {"deleted": false} brings it back."""
    meeting_id = db.update_task(task_id, deleted=True)
    if meeting_id is None:
        raise HTTPException(404, "Task not found")
    _reexport(meeting_id)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/send")
def send_task(task_id: int, request: Request):
    """Makes a ticket from the task in the tracker picked in Settings → Tasks (trackers.py), once."""
    try:
        ticket = trackers.send(task_id, str(request.base_url))
    except LookupError:
        raise HTTPException(404, "Task not found")
    except trackers.AlreadySent:
        raise HTTPException(409, "This task was already sent")
    except trackers.TrackerError as e:
        raise HTTPException(502, str(e))
    _reexport(db.get_task(task_id)["meeting_id"])  # meeting.json carries the ticket
    return {"ticket": ticket}


@app.get("/api/trackers")
def list_trackers():
    return trackers.describe()


@app.get("/api/trackers/{tracker_id}/projects")
def tracker_projects(tracker_id: str):
    """The tracker's projects, for the project picker, read with the saved settings."""
    try:
        return trackers.get(tracker_id).projects(settings.get_all())
    except KeyError:
        raise HTTPException(404, "No such tracker")
    except trackers.TrackerError as e:
        raise HTTPException(502, str(e))


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
    attachments.remove_all(meeting_id)
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
