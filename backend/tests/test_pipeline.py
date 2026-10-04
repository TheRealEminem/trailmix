import threading
import time

import pipeline


class FakeDB:
    def __init__(self):
        self.updates = []

    def update_meeting(self, meeting_id, **fields):
        self.updates.append(fields)


def test_gate_waits_for_memory_then_continues(monkeypatch):
    free = {"ok": False}
    fake = FakeDB()
    monkeypatch.setattr(pipeline, "db", fake)
    monkeypatch.setattr(pipeline.resources, "check", lambda gb, what: None if free["ok"] else "Waiting for memory")
    monkeypatch.setattr(pipeline.live, "active_count", lambda: 0)
    monkeypatch.setattr(pipeline, "RAM_POLL_S", 1)
    done = threading.Event()
    threading.Thread(target=lambda: (pipeline._gate(7, False, 3.0, "The model"), done.set())).start()
    time.sleep(0.3)
    assert not done.is_set() and fake.updates[0]["status"] == "waiting_confirm"
    free["ok"] = True
    assert done.wait(3)


def test_proceed_anyway_cuts_the_wait_short(monkeypatch):
    monkeypatch.setattr(pipeline, "db", FakeDB())
    monkeypatch.setattr(pipeline.resources, "check", lambda gb, what: "Waiting for memory")
    monkeypatch.setattr(pipeline.live, "active_count", lambda: 0)
    done = threading.Event()
    threading.Thread(target=lambda: (pipeline._gate(8, False, 3.0, "The model"), done.set())).start()
    time.sleep(0.3)
    assert pipeline.proceed_now(8)
    assert done.wait(3)
    assert not pipeline.proceed_now(8)  # nothing is waiting any more
