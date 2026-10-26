"""Task trackers: where "Send to …" on a task makes a ticket. You pick one in Settings → Tasks (`task_tracker`; empty
means none, and the action isn't offered). Each tracker says what it needs set up (its FIELDS, which Settings shows as
rows, all stored as ordinary settings), lists its projects for the project picker, and makes a ticket. Adding another
tracker is adding it to TRACKERS.

A task remembers its ticket (action_items.ticket_json), so it's sent once: the ticket is kept when the notes are
rewritten (database.replace_tasks) and travels in meeting.json.

Traverse is the user's own projects-and-tasks app, usually on this Mac at 127.0.0.1:8793. POST /api/tasks makes a
task ({text, notes, project_id}); GET /api/tasks/projects lists projects. It asks for a Bearer token only when its
sign-in is on. A task opens at <address>/#/tasks/t/<id>.
"""
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import httpx

import database as db
import settings

TIMEOUT = httpx.Timeout(15, connect=3)
_transport: httpx.BaseTransport | None = None     # tests put a pretend tracker here


class TrackerError(Exception):
    """Something to tell you: not set up, not answering, or it said no."""


class AlreadySent(Exception):
    pass


@dataclass
class Field:
    key: str          # the setting it's stored in
    label: str
    hint: str
    kind: str         # url | secret | project (picked from the tracker's projects)
    placeholder: str = ""


class Traverse:
    id = "traverse"
    name = "Traverse"
    blurb = "Your projects and tasks app. A task becomes a Traverse task, with a link back to its meeting."
    fields = [
        Field("traverse_url", "Address", "Where Traverse is running. On this Mac it's usually http://127.0.0.1:8793.",
              "url", "http://127.0.0.1:8793"),
        Field("traverse_api_key", "Access token",
              "Only if Traverse asks for a sign-in: its access token (TRAVERSE_ACCESS_TOKEN where it runs).", "secret"),
        Field("traverse_project", "Project", "Where new tasks go. No project: they land in My tasks.", "project"),
    ]

    def _client(self, cfg: dict) -> httpx.Client:
        base = (cfg.get("traverse_url") or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise TrackerError("Set Traverse's address in Settings → Tasks.")
        if settings.locked(cfg) and not settings.on_this_computer(base):
            raise TrackerError(f"{settings.LOCKDOWN}, so tasks can only go to a Traverse on this computer.")
        key = cfg.get("traverse_api_key") or ""
        return httpx.Client(base_url=base, timeout=TIMEOUT, transport=_transport, follow_redirects=False,
                            headers={"Authorization": f"Bearer {key}"} if key else {})

    def _call(self, cfg: dict, method: str, path: str, **kw) -> httpx.Response:
        try:
            with self._client(cfg) as client:
                r = client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise TrackerError(f"Traverse isn't answering at {cfg.get('traverse_url')}. Is it open? ({e})") from e
        if r.status_code in (401, 403):
            raise TrackerError("Traverse asks for a sign-in. Paste its access token in Settings → Tasks.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            raise TrackerError(f"Traverse said no ({r.status_code}): {detail or r.text[:200]}")
        return r

    def projects(self, cfg: dict) -> list[dict]:
        """Its open projects: [{id, name}]."""
        found = self._call(cfg, "GET", "/api/tasks/projects").json()
        return [{"id": p["id"], "name": p["name"]} for p in found if p.get("id") and not p.get("archived")]

    def create(self, cfg: dict, title: str, notes: str) -> dict:
        """Makes the task: {id, url}."""
        body = {"text": title, "notes": notes}
        if cfg.get("traverse_project"):
            body["project_id"] = cfg["traverse_project"]
        made = self._call(cfg, "POST", "/api/tasks", json=body).json()
        if not made.get("id"):
            raise TrackerError("Traverse didn't say which task it made.")
        return {"id": made["id"], "url": f"{cfg['traverse_url'].rstrip('/')}/#/tasks/t/{made['id']}"}


TRACKERS = {t.id: t for t in (Traverse(),)}


def describe() -> dict:
    """For Settings and the task lists: the one you picked (or "") and what each needs set up."""
    return {"active": settings.get_all()["task_tracker"],
            "trackers": [{"id": t.id, "name": t.name, "blurb": t.blurb, "fields": [asdict(f) for f in t.fields]}
                         for t in TRACKERS.values()]}


def get(tracker_id: str):
    if tracker_id not in TRACKERS:
        raise KeyError(tracker_id)
    return TRACKERS[tracker_id]


def _note(meeting: dict, link: str) -> str:
    when = datetime.fromisoformat(meeting["created_at"].replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone()
    return f"From the meeting [{meeting['title']}]({link}) on {local.day} {local:%B %Y}, in Trailmix."


_sending: set[int] = set()
_lock = threading.Lock()


def send(task_id: int, trailmix_url: str) -> dict:
    """Makes a ticket from the task in the tracker you picked and remembers it on the task. trailmix_url is where
    this Trailmix is, for the link back to the meeting. Raises LookupError (no such task), AlreadySent, TrackerError."""
    cfg = settings.get_all()
    if not cfg["task_tracker"]:
        raise TrackerError("Pick where tasks go in Settings → Tasks.")
    tracker = get(cfg["task_tracker"])
    with _lock:  # a second click while the first is on its way
        if task_id in _sending:
            raise AlreadySent()
        _sending.add(task_id)
    try:
        task = db.get_task(task_id)
        if task is None or task["deleted"]:
            raise LookupError(task_id)
        if task["ticket"]:
            raise AlreadySent()
        meeting = db.get_meeting(task["meeting_id"])
        made = tracker.create(cfg, task["text"], _note(meeting, f"{trailmix_url.rstrip('/')}/#meeting-{meeting['id']}"))
        ticket = {"tracker": tracker.id, "name": tracker.name, "id": made["id"], "url": made["url"],
                  "at": datetime.now(timezone.utc).isoformat()}
        db.update_task(task_id, ticket=ticket)
        return ticket
    finally:
        with _lock:
            _sending.discard(task_id)
