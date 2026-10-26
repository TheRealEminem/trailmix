"""The event stream (events.py) and the menu bar recorder's socket (recorder.py, /api/recorder/ws)."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import events
import recorder
import settings


@pytest.fixture
def fresh(monkeypatch):
    state = {"live": [], "system": {"cpu": 1}}
    monkeypatch.setattr(events, "topics", {k: (lambda k=k: state[k]) for k in state})
    monkeypatch.setattr(events, "_last", {})
    monkeypatch.setattr(events, "_subscribers", {})
    monkeypatch.setattr(recorder, "_seen_at", 0.0)
    monkeypatch.setattr(recorder, "_push", None)
    monkeypatch.setattr(recorder, "_commands", [])
    return state


def test_only_changes_are_sent_and_only_to_those_listening(fresh):
    async def scenario():
        window, first = await events.subscribe()
        assert json.loads(first["live"]) == [] and json.loads(first["system"]) == {"cpu": 1}
        helper, first = await events.subscribe(frozenset({"live"}))
        assert set(first) == {"live"}

        events._broadcast(events._changes(events._wanted()))
        assert window.empty() and helper.empty()  # nothing changed

        fresh["system"]["cpu"] = 2
        events._broadcast(events._changes(events._wanted()))
        assert json.loads(window.get_nowait()["system"]) == {"cpu": 2}
        assert helper.empty()  # it didn't ask about the system

        fresh["live"].append({"id": 1})
        events._broadcast(events._changes(events._wanted()))
        assert set(window.get_nowait()) == {"live"} and set(helper.get_nowait()) == {"live"}

    asyncio.run(scenario())


def test_the_stream_sends_everything_first_and_stops_listening_when_closed(fresh):
    async def scenario():
        stream = events.stream()
        first = await stream.__anext__()
        assert first.startswith("retry: 2000\nevent: state\ndata: ")
        assert json.loads(first.split("data: ", 1)[1]) == {"live": [], "system": {"cpu": 1}}
        assert len(events._subscribers) == 1
        await stream.aclose()
        assert events._subscribers == {}

    asyncio.run(scenario())


def test_frame_is_one_event_with_the_changed_topics():
    assert events.frame({"live": "[]", "models": '{"local":true}'}) == 'event: state\ndata: {"live":[],"models":{"local":true}}\n\n'


def test_recorder_socket_hears_commands_at_once_and_live_recordings(fresh, monkeypatch):
    import main

    monkeypatch.setattr(settings, "locked", lambda cfg=None: False)
    client = TestClient(main.app)
    assert recorder.status() == {"available": False}
    assert not recorder.request("start")
    with client.websocket_connect("/api/recorder/ws") as ws:
        assert ws.receive_json() == {"live": []}  # the recordings in progress, straight away
        assert recorder.status()["available"]  # just by being connected
        assert recorder.request("start")
        assert ws.receive_json() == {"commands": ["start"]}
        # Its report is taken in, and a lockdown setting out of step with the server's is put right.
        ws.send_json({"mic": "Built-in", "source": "All apps", "lockdown": True})
        assert ws.receive_json() == {"commands": ["no-lockdown"]}
        assert recorder.status()["mic"] == "Built-in"
    for _ in range(50):  # the server notices the close a moment later
        if not recorder.status()["available"]:
            break
        time.sleep(0.02)
    assert recorder.status() == {"available": False}
    assert not recorder.request("start")


def test_an_older_recorder_still_checks_in_over_post(fresh, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(recorder.time, "monotonic", lambda: now["t"])
    assert recorder.check_in({"mic": "Built-in"}) == []
    assert recorder.status()["available"]
    assert recorder.request("sound-check")
    assert recorder.check_in({"mic": "Built-in"}) == ["sound-check"]
    now["t"] += 10
    assert recorder.status() == {"available": False}
