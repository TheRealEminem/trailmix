"""No speech model in memory while idle: a recording starts at once, loads it in the background and catches up."""
import threading
import time

import numpy as np
import pytest

import database as db
import live
import local_engine
import main
import pipeline

SR = live.SR


def speech(seconds: float, pause_at: float | None = None) -> bytes:
    audio = (np.random.default_rng(1).uniform(-0.3, 0.3, int(seconds * SR)) * 32767).astype(np.int16)
    if pause_at is not None:
        audio[int(pause_at * SR):int((pause_at + 0.5) * SR)] = 0
    return b"\x00" + audio.tobytes()


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    return tmp_path


@pytest.fixture
def engine(monkeypatch):
    """A fake speech model that takes as long to load as the test says (`gate`)."""
    state = {"loaded": None, "chunks": [], "loads": 0, "unloads": 0, "gate": threading.Event(), "fail": None}
    state["gate"].set()

    def warm(model):
        state["gate"].wait(5)
        if state["fail"]:
            raise state["fail"]
        state["loads"] += 1
        state["loaded"] = model
        return True

    def final(audio, prompt=None, model=None):
        state["chunks"].append(len(audio) / SR)
        return [{"start": 0.0, "end": len(audio) / SR, "text": f"chunk {len(state['chunks'])}"}]

    def unload():
        state["unloads"] += 1
        state["loaded"] = None

    for name, fn in {"warm": warm, "transcribe_final": final, "unload": unload, "loaded": lambda: state["loaded"],
                     "available": lambda: True, "final_model": lambda cfg=None: "m"}.items():
        monkeypatch.setattr(local_engine, name, fn)
    monkeypatch.setattr(live.models, "installed", lambda m: True)
    monkeypatch.setattr(live.models, "require", lambda m: None)
    monkeypatch.setattr(live.vad, "speech_regions", lambda a: [(0, len(a))] if len(a) else [])
    monkeypatch.setattr(live, "_load_error", None)
    monkeypatch.setattr(live, "_last_recording", 0.0)
    return state


def session(draft=True):
    mid = db.create_meeting("Standup", title_auto=False)
    folder = db.AUDIO_DIR / str(mid)
    db.update_meeting(mid, audio_dir=str(folder), status="recording")
    return live.LiveSession(mid, folder, draft=draft, cfg={"live_final": True, "transcribe_engine": "local"})


def wait_loaded():
    for _ in range(200):
        if not live._warming.locked():
            return
        time.sleep(0.01)
    raise AssertionError("still loading")


def test_recording_starts_at_once_and_catches_up_once_the_model_is_ready(fresh_db, engine):
    engine["gate"].clear()  # a slow load
    s = session()
    s.write(speech(150, pause_at=70))  # audio arrives although nothing has loaded
    assert s.duration == 150
    assert s.draft_step() == [] and not engine["chunks"]  # held back, not dropped
    assert s.model_status.startswith("Loading transcription model")
    engine["gate"].set()
    wait_loaded()
    assert s.model_status == "Catching up on the transcript…"
    lines = [line for _ in range(10) for line in s.draft_step()]
    assert max(engine["chunks"]) <= live.CATCH_UP_S  # not one giant chunk
    assert sum(engine["chunks"]) > 145  # all of it but the last moments
    assert [x["start"] for x in lines] == sorted(x["start"] for x in lines)
    assert s.model_status is None
    s.close()


def test_new_audio_during_catch_up_is_kept_in_order(fresh_db, engine):
    s = session()
    wait_loaded()
    s.write(speech(120))
    s.draft_step()
    s.write(speech(10))
    for _ in range(10):
        s.draft_step()
    assert s._pending_start[0] + sum(len(p) for p in s._pending[0]) == s._written[0] == 130 * SR
    assert sum(engine["chunks"]) == pytest.approx(s._pending_start[0] / SR)
    s.close()


def test_find_cut_stays_in_size_with_a_pause_at_the_far_end():
    audio = np.frombuffer(speech(300, pause_at=290)[1:], dtype=np.int16).astype(np.float32) / 32768
    assert live.find_cut(audio) <= live.CATCH_UP_S * SR


def test_a_failed_load_keeps_the_audio_and_leaves_the_transcript_to_the_pipeline(fresh_db, engine):
    engine["fail"] = RuntimeError("bad model")
    s = session()
    wait_loaded()
    s.write(speech(20))
    assert s.draft_step() == []
    assert "couldn't load" in s.model_status and not engine["chunks"]
    s.write(speech(5))  # still recording
    assert s.duration == 25
    s.finish()
    s.close()
    assert not db.get_meeting(s.meeting_id).get("live_json")  # so the pipeline does a full pass over the recording


def test_a_short_meeting_all_inside_the_load_only_fills_the_gap(fresh_db, engine, monkeypatch):
    monkeypatch.setattr(pipeline.cleanup, "drop_hallucinations", lambda segs: segs)  # the fake lines look invented
    engine["gate"].clear()
    s = session()
    s.write(speech(30))
    s.draft_step()
    s.finish()
    s.close()
    engine["gate"].set()
    wait_loaded()
    m = db.get_meeting(s.meeting_id)
    assert m["live_json"]
    monkeypatch.setattr(pipeline.store, "track_dir", lambda _m: s.folder)
    segments, _, duration = pipeline._finish_live(m, {"transcribe_engine": "local"})
    assert duration == 30 and segments  # filled in here, no full pass through the worker


def test_the_model_goes_after_the_meeting_and_nothing_brings_it_back(fresh_db, engine):
    s = session()
    wait_loaded()
    assert engine["loaded"] == "m"
    assert not live.unload_if_idle()  # recording
    s.close()
    assert not live.unload_if_idle()  # just stopped: the pipeline may still want it
    later = time.monotonic() + live.UNLOAD_AFTER_S + 1
    db.update_meeting(s.meeting_id, status="transcribing")
    assert not live.unload_if_idle(later)
    db.update_meeting(s.meeting_id, status="done")
    assert live.unload_if_idle(later)
    assert engine["loaded"] is None and engine["unloads"] == 1
    assert not live.unload_if_idle(later)  # nothing to do, and no load either
    assert engine["loads"] == 1


def test_nothing_at_startup_or_idle_loads_a_model():
    assert not hasattr(main, "_warm_loop")


def test_no_captions_and_no_final_transcript_load_nothing(fresh_db, engine):
    mid = db.create_meeting("Quiet", title_auto=False)
    folder = db.AUDIO_DIR / str(mid)
    s = live.LiveSession(mid, folder, draft=False, cfg={"live_final": False, "transcribe_engine": "local"})
    wait_loaded()
    assert engine["loads"] == 0
    s.close()


def test_asking_the_mlx_engine_about_its_model_does_not_import_mlx(monkeypatch):
    import sys

    import mlx_engine
    monkeypatch.delitem(sys.modules, "mlx_whisper.transcribe", raising=False)
    assert mlx_engine.loaded() is None
    mlx_engine.unload()
    assert "mlx_whisper.transcribe" not in sys.modules


def test_an_earlier_failed_load_does_not_stop_the_next_recording_captions(fresh_db, engine):
    live._load_error = "out of memory"       # the previous recording's load failed
    engine["loaded"] = "m"                   # since then the model got loaded (by a job, say)
    s = session()
    s.write(speech(30))
    s.draft_step()
    assert not s._no_model
    s.close()
