"""One stream of what's going on, instead of every window asking every couple of seconds.

GET /api/events (main.py) is a server-sent events stream: a window gets the whole picture on connecting, then
only what changed. The picture is made of topics, each a function in `topics` (set by main.py): the menu bar
recorder and what it would record, recordings in progress, this Mac's memory and disk, the speech models'
downloads. The menu bar recorder's own socket (recorder.py) subscribes the same way, to just the recordings.

The ticker looks every TICK_S, once for everyone, and only at the topics somebody is listening to (nobody
connected: nothing is computed). Whoever changes something that shouldn't wait, such as a recording starting,
calls notify() and the ticker looks straight away.
"""
import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable

from starlette.concurrency import run_in_threadpool

log = logging.getLogger("trailmix.events")

TICK_S = 2.0
KEEPALIVE_S = 15.0  # a comment line this often, so proxies and browsers keep an idle stream open

Snapshot = dict[str, str]  # topic -> its state as JSON
topics: dict[str, Callable[[], object]] = {}

_loop: asyncio.AbstractEventLoop | None = None
_wake: asyncio.Event | None = None
_snapshot_lock = threading.Lock()
_last: Snapshot = {}  # what subscribers were last sent, per topic
_subscribers: dict[asyncio.Queue, frozenset[str]] = {}  # queue -> the topics it wants


def notify() -> None:
    """Something changed that listeners should see now, not at the next tick. Safe to call from any thread."""
    loop, wake = _loop, _wake
    if loop and wake:
        loop.call_soon_threadsafe(wake.set)


def _changes(wanted: frozenset[str]) -> Snapshot:
    """Takes a snapshot of the wanted topics; returns the ones that differ from what was last sent."""
    out: Snapshot = {}
    with _snapshot_lock:
        for name in wanted:
            fn = topics.get(name)
            if fn is None:
                continue
            try:
                text = json.dumps(fn(), separators=(",", ":"))
            except Exception:
                log.exception("Couldn't take the %s snapshot for the event stream", name)
                continue
            if _last.get(name) != text:
                _last[name] = text
                out[name] = text
    return out


def _wanted() -> frozenset[str]:
    return frozenset().union(*_subscribers.values()) if _subscribers else frozenset()


def _broadcast(changed: Snapshot) -> None:
    for queue, wanted in list(_subscribers.items()):
        part = {k: v for k, v in changed.items() if k in wanted}
        if part:
            queue.put_nowait(part)


async def run() -> None:
    """The ticker, started with the server (main.py's lifespan)."""
    global _loop, _wake
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    while True:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_wake.wait(), TICK_S)
        _wake.clear()
        wanted = _wanted()
        if not wanted:
            continue
        try:
            _broadcast(await run_in_threadpool(_changes, wanted))
        except Exception:
            log.exception("The event stream's tick failed")


def frame(changed: Snapshot) -> str:
    """One server-sent event: {"topic": state, ...} for the topics that changed."""
    body = ",".join(f'"{name}":{text}' for name, text in changed.items())
    return f"event: state\ndata: {{{body}}}\n\n"


async def subscribe(wanted: frozenset[str] | None = None) -> tuple[asyncio.Queue, Snapshot]:
    """Starts listening to `wanted` topics (all by default). Returns the queue the changes arrive on, and the
    current state of those topics to send first."""
    wanted = wanted if wanted is not None else frozenset(topics)
    queue: asyncio.Queue = asyncio.Queue()
    _broadcast(await run_in_threadpool(_changes, wanted))  # others hear of anything that moved meanwhile
    _subscribers[queue] = wanted
    return queue, {name: _last[name] for name in wanted if name in _last}


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.pop(queue, None)


async def stream() -> AsyncIterator[str]:
    """The server-sent events for one window: everything first, then changes, with a keep-alive when quiet."""
    queue, current = await subscribe()
    try:
        yield "retry: 2000\n" + frame(current)
        while True:
            try:
                changed = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield frame(changed)
    finally:
        unsubscribe(queue)
