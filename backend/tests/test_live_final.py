import json

import numpy as np
import pytest

import audio_store as store
import database as db
import live
import pipeline
import resources

SR = store.SAMPLE_RATE


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    return tmp_path


@pytest.fixture
def fake_model(monkeypatch):
    """Every stretch of audio is "speech", and the model reports how many seconds it was given."""
    calls = []

    def transcribe_final(audio, prompt=None):
        calls.append(len(audio) / SR)
        return [{"start": 0.0, "end": len(audio) / SR, "text": f"heard {len(audio) / SR:.0f} seconds"}]

    for module in (live, pipeline):
        monkeypatch.setattr(module.vad, "speech_regions", lambda a: [(0, len(a))] if len(a) else [])
        monkeypatch.setattr(module.mlx_engine, "transcribe_final", transcribe_final)
    monkeypatch.setattr(pipeline.mlx_engine, "unload", lambda: None)
    monkeypatch.setattr(live.mlx_engine, "available", lambda: True)
    monkeypatch.setattr(live.models, "require", lambda repo: None)
    monkeypatch.setattr(live, "warm_up", lambda cfg, now=False: False)  # never the real model
    return calls


def speech(seconds: float, pause_at: float | None = None) -> np.ndarray:
    """Loud noise (counts as talking), with a half-second pause where asked."""
    audio = (np.random.default_rng(1).uniform(-0.3, 0.3, int(seconds * SR)) * 32767).astype(np.int16)
    if pause_at is not None:
        audio[int(pause_at * SR):int((pause_at + 0.5) * SR)] = 0
    return audio


def test_recording_transcribes_for_keeps_and_the_pipeline_only_does_the_tail(fresh_db, fake_model):
    mid = db.create_meeting("Standup", title_auto=False)
    folder = db.AUDIO_DIR / str(mid)
    db.update_meeting(mid, audio_dir=str(folder), status="recording")
    session = live.LiveSession(mid, folder, draft=False, cfg={"live_final": True, "transcribe_engine": "local"})
    assert session.final

    for ch in (0, 1):  # 8 s of talking with a pause at 5 s, on both sides
        session.write(bytes([ch]) + speech(8, pause_at=5).tobytes())
    session.draft_step()  # transcribes up to the pause on each track
    session.finish()
    session.close()

    saved = json.loads(db.get_meeting(mid)["live_json"])
    assert saved["complete"] and saved["engine"] == "local"
    assert saved["covered"]["mic"] == saved["covered"]["system"] == int(5.5 * SR)
    assert saved["tracks"]["mic"][0]["text"] == "heard 6 seconds"  # (5.5 s, rounded)

    fake_model.clear()
    m = db.get_meeting(mid)
    segments, labeled, duration = pipeline._finish_live(m, {"transcribe_engine": "local"})
    assert fake_model == [2.5, 2.5]  # only what came after the last live chunk, per track
    assert labeled and duration == 8
    assert [s["speaker"] for s in segments].count("Them") == 2
    assert segments[-1]["start"] == pytest.approx(5.5)
    assert db.get_meeting(mid)["live_json"] is None  # used once


def test_a_missed_chunk_means_transcribing_it_all_afterwards(fresh_db, fake_model, monkeypatch):
    mid = db.create_meeting("Standup", title_auto=False)
    folder = db.AUDIO_DIR / str(mid)
    db.update_meeting(mid, audio_dir=str(folder), status="recording")
    session = live.LiveSession(mid, folder, draft=False, cfg={"live_final": True, "transcribe_engine": "local"})

    def broken(audio, prompt=None):
        raise RuntimeError("out of memory")

    monkeypatch.setattr(live.mlx_engine, "transcribe_final", broken)
    session.write(bytes([0]) + speech(8, pause_at=5).tobytes())
    session.draft_step()
    session.finish()
    session.close()
    assert json.loads(db.get_meeting(mid)["live_json"])["complete"] is False
    assert pipeline._finish_live(db.get_meeting(mid), {"transcribe_engine": "local"}) is None


def test_live_transcript_is_ignored_when_the_engine_changed(fresh_db, fake_model):
    mid = db.create_meeting("Standup", title_auto=False)
    db.update_meeting(mid, audio_dir=str(db.AUDIO_DIR / str(mid)),
                      live_json=json.dumps({"tracks": {}, "covered": {}, "complete": True, "engine": "local"}))
    assert pipeline._finish_live(db.get_meeting(mid), {"transcribe_engine": "remote"}) is None


class _VM:
    def __init__(self, available_gb, total_gb=16):
        self.available, self.total = available_gb * resources.GB, total_gb * resources.GB


def test_memory_gate_lets_macos_swap_unless_it_is_dire(monkeypatch):
    state = {"vm": _VM(10), "disk": 0.5, "pressure": 1}
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: state["vm"])
    monkeypatch.setattr(resources, "_disk_free_share", lambda: state["disk"])
    monkeypatch.setattr(resources, "memory_pressure", lambda: state["pressure"])

    assert resources.check(5, "The summary model") is None  # plenty free
    state["vm"] = _VM(2)
    assert resources.check(5, "The summary model") is None  # short, but swap has room: go
    state["pressure"] = 2
    assert resources.check(5, "The summary model") is None  # macOS's "warning" level is fine too
    state["pressure"] = 4
    assert "critical memory pressure" in resources.check(5, "The summary model")
    state["pressure"], state["disk"] = 1, 0.05
    assert "disk free" in resources.check(5, "The summary model")
    state["disk"] = 0.5
    assert "more than this Mac can spare" in resources.check(15, "The summary model")


def test_cuts_land_at_pauses_or_between_words():
    talk = speech(10, pause_at=3).astype(np.float32) / 32768.0
    assert live.find_cut(talk[:int(4.5 * SR)]) == int(3.5 * SR)  # the pause
    assert live.find_cut(talk[int(3.5 * SR):int(6 * SR)]) is None  # under 5 s, nobody paused: wait
    nonstop = talk[int(3.5 * SR):].copy()
    nonstop[int(4.2 * SR):int(4.25 * SR)] *= 0.01  # a breath between two words just before the 5 s mark
    cut = live.find_cut(nonstop)
    assert int(4.2 * SR) <= cut <= int(4.25 * SR)


def test_each_chunk_gets_what_was_just_said_as_context(fresh_db, fake_model, monkeypatch):
    prompts = []

    def transcribe_final(audio, prompt=None):
        prompts.append(prompt)
        return [{"start": 0.0, "end": 1.0, "text": f"part {len(prompts)}"}]

    monkeypatch.setattr(live.mlx_engine, "transcribe_final", transcribe_final)
    mid = db.create_meeting("Standup", title_auto=False)
    folder = db.AUDIO_DIR / str(mid)
    session = live.LiveSession(mid, folder, draft=True, cfg={"live_final": True, "transcribe_engine": "local"})
    session.write(bytes([0]) + speech(4, pause_at=2.5).tobytes())
    session.draft_step()
    session.write(bytes([0]) + speech(4, pause_at=2.5).tobytes())
    session.draft_step()
    session.close()
    assert prompts == ["", "part 1"]
