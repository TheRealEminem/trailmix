from fastapi.testclient import TestClient

import main
import recorder
import settings


def test_recorder_round_trip(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(recorder.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(recorder, "_seen_at", 0.0)
    assert recorder.status() == {"available": False}
    assert not recorder.request("start")

    assert recorder.check_in({"mic": "MacBook Air Microphone", "source": "All apps", "machine": "Dale's Air"}) == []
    assert recorder.status()["available"] and recorder.status()["mic"] == "MacBook Air Microphone"
    assert recorder.request("start") and recorder.request("start")  # asking twice queues it once
    assert recorder.check_in({}) == ["start"]
    assert recorder.check_in({}) == []

    now["t"] += 10
    assert recorder.status() == {"available": False}


def test_capabilities_default_to_the_mac_set(monkeypatch):
    monkeypatch.setattr(recorder.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(recorder, "_seen_at", 0.0)  # put back afterwards, so other tests see no recorder
    recorder.check_in({"mic": "MacBook Air Microphone"})  # an older Mac app: no platform or capabilities
    s = recorder.status()
    assert s["capabilities"] == list(recorder.CAPABILITIES)
    assert s["platform"] == "macos"


def test_capabilities_reduced_list(monkeypatch):
    monkeypatch.setattr(recorder.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(recorder, "_seen_at", 0.0)  # put back afterwards, so other tests see no recorder
    recorder.check_in({"platform": "linux-x64", "capabilities": ["system_audio", "hotkeys"]})
    s = recorder.status()
    assert s["platform"] == "linux-x64"
    assert s["capabilities"] == ["system_audio", "hotkeys"]
    recorder.check_in({"platform": "windows-x64", "capabilities": []})  # nothing beyond recording the mic
    assert recorder.status()["capabilities"] == []


def test_capabilities_pass_through_the_api(monkeypatch):
    monkeypatch.setattr(recorder.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(recorder, "_seen_at", 0.0)  # put back afterwards, so other tests see no recorder
    monkeypatch.setattr(settings, "locked", lambda: False)
    client = TestClient(main.app)
    client.post("/api/recorder/check-in", json={"platform": "windows-x64", "capabilities": ["sound_check"]})
    s = client.get("/api/recorder").json()
    assert s["platform"] == "windows-x64" and s["capabilities"] == ["sound_check"]
    client.post("/api/recorder/check-in", json={"mic": "Built-in"})
    assert client.get("/api/recorder").json()["capabilities"] == list(recorder.CAPABILITIES)
