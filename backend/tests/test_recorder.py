import recorder


def test_recorder_round_trip(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(recorder.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(recorder, "_seen_at", 0.0)
    assert recorder.status() == {"available": False}
    assert not recorder.request("start")

    assert recorder.check_in({"mic": "MacBook Air Microphone", "source": "All apps", "machine": "Mark's Air"}) == []
    assert recorder.status()["available"] and recorder.status()["mic"] == "MacBook Air Microphone"
    assert recorder.request("start") and recorder.request("start")  # asking twice queues it once
    assert recorder.check_in({}) == ["start"]
    assert recorder.check_in({}) == []

    now["t"] += 10
    assert recorder.status() == {"available": False}
