import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import live_notes
import llm_engine

GB = 1024**3
CFG = {"summary_provider": "ollama", "summary_fallback": "none", "live_notes": "auto", "your_name": "",
       "custom_template": "", "ollama_url": "http://x"}


@pytest.fixture
def mac(monkeypatch):
    """A Mac with settable memory, power, memory pressure and notes provider."""
    state = SimpleNamespace(ram=16, power={"battery": True, "plugged_in": True, "percent": 80, "low_power": False},
                            pressure=1, local=True)
    monkeypatch.setattr(live_notes.psutil, "virtual_memory", lambda: SimpleNamespace(total=state.ram * GB))
    monkeypatch.setattr(live_notes, "power", lambda: state.power)
    monkeypatch.setattr(live_notes.resources, "memory_pressure", lambda: state.pressure)
    monkeypatch.setattr(llm_engine, "is_local", lambda pid, cfg: state.local)
    monkeypatch.setattr(llm_engine, "ollama_model_size_gb", lambda cfg: 4.7)
    live_notes._state.clear()
    return state


def test_automatic_is_off_on_a_16_gb_mac_with_a_local_model(mac):
    plan = live_notes.decide(CFG)
    assert not plan["on"] and "16 GB" in plan["reason"] and "Choose On" in plan["reason"]


def test_automatic_is_on_with_32_gb(mac):
    mac.ram = 32
    plan = live_notes.decide(CFG)
    assert plan["on"] and plan["warning"] is None


def test_on_with_16_gb_warns(mac):
    plan = live_notes.decide({**CFG, "live_notes": "on"})
    assert plan["on"] and "16 GB" in plan["warning"] and "about 7 GB" in plan["warning"]


def test_a_cloud_ai_is_always_on_unless_turned_off(mac):
    mac.local = False
    assert live_notes.decide(CFG)["on"] and live_notes.decide({**CFG, "live_notes": "on"})["on"]
    assert not live_notes.decide({**CFG, "live_notes": "off"})["on"]
    mac.power = {"battery": True, "plugged_in": False, "percent": 5, "low_power": True}
    assert live_notes.wait_reason(CFG) is None  # nothing runs on this Mac, so battery doesn't matter


@pytest.mark.parametrize("power,pressure,expected", [
    ({"battery": True, "plugged_in": True, "percent": 10, "low_power": False}, 1, None),  # plugged in: fine
    ({"battery": True, "plugged_in": False, "percent": 70, "low_power": False}, 1, None),
    ({"battery": True, "plugged_in": False, "percent": 39, "low_power": False}, 1, "Paused on battery at 39%"),
    ({"battery": True, "plugged_in": False, "percent": 90, "low_power": True}, 1, "Paused while Low Power Mode is on"),
    ({"battery": False, "plugged_in": True, "percent": None, "low_power": False}, 2, None),  # Cloudy: fine
    ({"battery": False, "plugged_in": True, "percent": None, "low_power": False}, 4, "Paused while memory is critically low"),
])
def test_a_local_model_waits_for_power_and_memory(mac, power, pressure, expected):
    mac.power, mac.pressure = power, pressure
    assert live_notes.wait_reason(CFG) == expected


def test_power_reads_pmset(monkeypatch):
    out = {
        ("/usr/bin/pmset", "-g", "batt"): "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t37%; discharging; 2:10 remaining present: true\n",
        ("/usr/bin/pmset", "-g"): "System-wide power settings:\n lowpowermode         1\n",
    }
    monkeypatch.setattr(live_notes, "_power", None)
    monkeypatch.setattr(live_notes.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(stdout=out[tuple(cmd)]))
    assert live_notes.power() == {"battery": True, "plugged_in": False, "percent": 37, "low_power": True}


def test_a_mac_without_a_battery_counts_as_plugged_in(monkeypatch):
    monkeypatch.setattr(live_notes, "_power", None)
    monkeypatch.setattr(live_notes.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(stdout="Now drawing from 'AC Power'\n"))
    assert live_notes.power()["plugged_in"] and not live_notes.power()["battery"]


def _session(until: float, lines: list[tuple[float, str]]):
    return SimpleNamespace(meeting_id=7, draft=False, final=True, transcribed_until=until, has_system=True,
                           drafts=[{"start": t, "speaker": "Them", "text": x} for t, x in lines])


def _run_tick(monkeypatch, session, cfg, replies):
    prompts = []

    def fake_generate(pid, cfg, prompt, system=None, keep_alive=None, **kw):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(live_notes.live, "sessions", lambda: [session])
    monkeypatch.setattr(live_notes.settings, "get_all", lambda: cfg)
    monkeypatch.setattr(live_notes.db, "get_meeting", lambda mid: {"id": mid})
    monkeypatch.setattr(live_notes.questions, "vocabulary_prompt", lambda: "")
    saved = {}
    monkeypatch.setattr(live_notes.db, "update_meeting", lambda mid, **f: saved.update(f))
    monkeypatch.setattr(llm_engine, "generate", fake_generate)
    live_notes.tick()
    for t in threading.enumerate():
        if t.name == "live-notes":
            t.join(5)
    return prompts, saved


def test_notes_wait_for_ten_minutes_of_transcript(mac, monkeypatch):
    mac.ram = 32
    prompts, saved = _run_tick(monkeypatch, _session(590, [(5, "Hello")]), CFG, [])
    assert not prompts and live_notes.status(7)["notes_status"] == "First notes at 10:00"


def test_notes_cover_each_ten_minutes_in_turn(mac, monkeypatch):
    mac.ram = 32
    session = _session(640, [(5, "We picked Tuesday."), (599, "Budget is ten."), (605, "Next stretch.")])
    prompts, saved = _run_tick(monkeypatch, session, CFG, ["- [00:05] Picked Tuesday"])
    assert len(prompts) == 1 and "Budget is ten." in prompts[0] and "Next stretch." not in prompts[0]
    assert "still going" in prompts[0]
    assert saved["live_notes_json"] == [{"start": 0.0, "end": 600.0, "text": "- [00:05] Picked Tuesday"}]
    status = live_notes.status(7)
    assert status["notes"][0]["text"] == "- [00:05] Picked Tuesday" and status["notes_status"] == "Next notes at 20:00"


def test_paused_on_low_battery_with_a_local_model(mac, monkeypatch):
    mac.power = {"battery": True, "plugged_in": False, "percent": 20, "low_power": False}
    prompts, _ = _run_tick(monkeypatch, _session(700, [(5, "Hi")]), {**CFG, "live_notes": "on"}, [])
    assert not prompts
    assert live_notes.status(7)["notes_status"] == "Paused on battery at 20%. They catch up after the meeting."


def test_off_on_16_gb_does_nothing(mac, monkeypatch):
    prompts, _ = _run_tick(monkeypatch, _session(1300, [(5, "Hi")]), CFG, [])
    assert not prompts and live_notes.status(7)["notes_status"] is None


def test_a_failure_says_so_and_retries_later(mac, monkeypatch):
    mac.ram = 32

    def boom(*a, **k):
        raise llm_engine.LLMError("Ollama isn't reachable")

    monkeypatch.setattr(live_notes.live, "sessions", lambda: [_session(700, [(5, "Hi")])])
    monkeypatch.setattr(live_notes.settings, "get_all", lambda: CFG)
    monkeypatch.setattr(live_notes.db, "get_meeting", lambda mid: {"id": mid})
    monkeypatch.setattr(live_notes.questions, "vocabulary_prompt", lambda: "")
    monkeypatch.setattr(llm_engine, "generate", boom)
    live_notes.tick()
    live_notes.wait_until_idle(7, timeout=5)
    st = live_notes.status(7)["notes_status"]
    assert st.startswith("Couldn't write notes just now") and live_notes._state[7]["retry_at"] > time.time()


def test_usable_only_with_the_transcript_made_while_recording():
    notes = [{"start": 0, "end": 600, "text": "- a"}, {"start": 600, "end": 1200, "text": ""}]
    m = {"live_notes_json": json.dumps(notes), "transcribed_with": "whisper-large-v3-turbo, while recording"}
    assert live_notes.usable(m) == ([notes[0]], 1200)
    assert live_notes.usable({**m, "transcribed_with": "whisper-large-v3-turbo, after the meeting"}) is None
    assert live_notes.usable({"live_notes_json": None, "transcribed_with": "x, while recording"}) is None


def test_after_keeps_the_rest_with_some_overlap():
    text = "[08:00] You: early\n[09:00] Them: just before\ncontinued\n[10:30] You: after\n[1:02:03] Them: late"
    assert llm_engine.after(text, 600) == "[09:00] Them: just before\ncontinued\n[10:30] You: after\n[1:02:03] Them: late"


def test_a_long_meeting_is_written_from_the_notes_taken_during_it(monkeypatch):
    calls = []

    def fake_generate(pid, cfg, prompt, system=None, keep_alive=None, on_text=None):
        calls.append(prompt)
        return "- rest" if "TRANSCRIPT (PART" in prompt else "## Overview\nNotes.\n\n## Action Items\n- None"

    monkeypatch.setattr(llm_engine, "generate", fake_generate)
    monkeypatch.setattr(llm_engine, "unload", lambda pid, cfg: None)
    monkeypatch.setattr(llm_engine, "context_chars", lambda pid, cfg: 5000)
    transcript = "\n".join(f"[{i // 60:02d}:{i % 60:02d}] You: " + "word " * 30 for i in range(0, 2400, 6))
    early = ([{"start": 0, "end": 1800, "text": "- the first half hour"}], 1800)
    drafts = []
    summary, _, _ = llm_engine.summarize(transcript, CFG, "auto", "general", [], want_title=False,
                                         on_draft=drafts.append, early_notes=early)
    notes_calls = [c for c in calls if "TRANSCRIPT (PART" in c]
    assert notes_calls and "[28:30]" in notes_calls[0] and "[10:00]" not in "".join(notes_calls)  # only the rest
    assert "NOTES ON [00:00] TO [30:00]:\n- the first half hour" in calls[-1]
    assert drafts[0].startswith("## Notes on 00:00 to 30:00")  # shown straight away


def test_a_meeting_that_fits_ignores_the_notes(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_engine, "generate", lambda pid, cfg, prompt, **k: calls.append(prompt) or "## Action Items\n- None")
    monkeypatch.setattr(llm_engine, "unload", lambda pid, cfg: None)
    llm_engine.summarize("[00:01] You: short", CFG, "auto", "general", [], want_title=False,
                         early_notes=([{"start": 0, "end": 600, "text": "- x"}], 600))
    assert len(calls) == 1 and "[00:01] You: short" in calls[0] and "NOTES ON" not in calls[0]
