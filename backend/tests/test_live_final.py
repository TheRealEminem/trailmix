import json
import threading

import numpy as np
import pytest

import audio_store as store
import database as db
import live
import pipeline
import platform_words
import resources
import settings

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

    def transcribe_final(audio, prompt=None, model=None):
        calls.append(len(audio) / SR)
        return [{"start": 0.0, "end": len(audio) / SR, "text": f"heard {len(audio) / SR:.0f} seconds"}]

    for module in (live, pipeline):
        monkeypatch.setattr(module.vad, "speech_regions", lambda a: [(0, len(a))] if len(a) else [])
        monkeypatch.setattr(module.local_engine, "transcribe_final", transcribe_final)
    monkeypatch.setattr(pipeline.local_engine, "unload", lambda: None)
    monkeypatch.setattr(live.local_engine, "available", lambda: True)
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


def _recording(*chunks: tuple[int, np.ndarray]) -> int:
    """A finished recording's meeting whose audio went in as (channel, samples), one draft_step after each."""
    mid = db.create_meeting("Standup", title_auto=False)
    folder = db.AUDIO_DIR / str(mid)
    db.update_meeting(mid, audio_dir=str(folder), status="recording")
    session = live.LiveSession(mid, folder, draft=False, cfg={"live_final": True, "transcribe_engine": "local"})
    for ch, samples in chunks:
        session.write(bytes([ch]) + samples.tobytes())
        while True:  # a long stretch takes several rounds (CATCH_UP_S at a time)
            before = session.transcribed_until, dict(session._pending_start)
            session.draft_step()
            if before == (session.transcribed_until, dict(session._pending_start)):
                break
    session.finish()
    session.close()
    return mid


def test_a_failed_chunk_is_all_that_gets_transcribed_again(fresh_db, fake_model, monkeypatch):
    monkeypatch.setattr(pipeline.cleanup, "drop_hallucinations", lambda segs: segs)  # the fake model's lines look invented
    good = live.local_engine.transcribe_final
    tries = []

    def fails_once(audio, prompt=None, model=None):
        tries.append(1)
        if len(tries) == 2:
            raise RuntimeError("out of memory")
        return good(audio, prompt, model)

    monkeypatch.setattr(live.local_engine, "transcribe_final", fails_once)
    mid = _recording((0, speech(8, pause_at=5)[:int(5.5 * SR)]),  # a chunk that works
                     (0, speech(8, pause_at=5)))  # this one fails

    monkeypatch.setattr(live.local_engine, "transcribe_final", good)
    fake_model.clear()
    segments, _, duration = pipeline._finish_live(db.get_meeting(mid), {"transcribe_engine": "local"})
    assert fake_model == [5.5, 2.5]  # not the part that worked: the chunk that failed, then the tail
    assert duration == pytest.approx(13.5)
    assert [round(s["start"], 1) for s in segments] == [0.0, 5.5, 11.0]  # in order


def test_a_long_tail_is_filled_in_instead_of_starting_over(fresh_db, fake_model, monkeypatch):
    """The live job fell four minutes behind in a 20-minute meeting: only those four minutes are left to do."""
    monkeypatch.setattr(pipeline.cleanup, "drop_hallucinations", lambda segs: segs)  # the fake model's lines look invented
    mid = _recording((0, speech(960, pause_at=955)))
    with open(db.AUDIO_DIR / str(mid) / "mic.pcm", "ab") as f:
        f.write(speech(240).tobytes())
    db.update_meeting(mid, duration_sec=1200)

    fake_model.clear()
    segments, _, duration = pipeline._finish_live(db.get_meeting(mid), {"transcribe_engine": "local"})
    assert sum(fake_model) == pytest.approx(244.5, abs=1)  # the end of the first part, and the four minutes
    assert duration == 1200 and segments[0]["start"] == 0


def test_a_transcript_missing_most_of_the_meeting_means_transcribing_it_all(fresh_db, fake_model):
    mid = _recording((0, speech(100, pause_at=95)))
    with open(db.AUDIO_DIR / str(mid) / "mic.pcm", "ab") as f:
        f.write(speech(300).tobytes())  # more than half of it, and more than a minute and a half
    fake_model.clear()
    assert pipeline._finish_live(db.get_meeting(mid), {"transcribe_engine": "local"}) is None
    assert fake_model == []  # it found out before spending anything


def test_a_short_meeting_is_finished_even_if_nothing_was_transcribed_yet(fresh_db, fake_model):
    mid = _recording((0, speech(1.5)))  # stopped before the first chunk
    fake_model.clear()
    assert pipeline._finish_live(db.get_meeting(mid), {"transcribe_engine": "local"}) is not None
    assert fake_model == [1.5]


def test_a_meeting_finished_live_writes_notes_before_the_audio_is_compressed(fresh_db, fake_model, monkeypatch):
    """The real run: no re-transcribing, and the notes don't wait for ffmpeg."""
    mid = _recording((0, speech(8, pause_at=5)))
    release = threading.Event()
    compressed = threading.Event()
    real = pipeline.store.compress_folder

    def slow_compress(folder):
        release.wait(10)
        real(folder)
        compressed.set()

    monkeypatch.setattr(pipeline.store, "compress_folder", slow_compress)
    monkeypatch.setattr(pipeline, "_transcribe_local", lambda m: pytest.fail("transcribed the whole recording again"))
    monkeypatch.setattr(pipeline.settings, "get_all", lambda: {**settings.DEFAULTS, "transcribe_engine": "local", "auto_summarize": False, "auto_export": False, "duplicates": "keep"})
    db.update_meeting(mid, status="queued")
    pipeline._run_stages(mid, frozenset(), frozenset())

    m = db.get_meeting(mid)
    assert m["transcribed"] and m["status"] == "ready_summarize" and m["transcribed_with"].endswith("while recording")
    folder = db.AUDIO_DIR / str(mid)
    assert (folder / "mic.pcm").exists()  # still raw: it didn't wait

    release.set()
    assert compressed.wait(30)
    assert (folder / "mic.ogg").exists() and not (folder / "mic.pcm").exists()


def test_an_old_incomplete_transcript_is_still_redone(fresh_db, fake_model):
    mid = db.create_meeting("Standup", title_auto=False)
    db.update_meeting(mid, audio_dir=str(db.AUDIO_DIR / str(mid)),
                      live_json=json.dumps({"tracks": {}, "covered": {}, "complete": False, "engine": "local"}))
    assert pipeline._finish_live(db.get_meeting(mid), {"transcribe_engine": "local"}) is None


def test_compression_cut_short_by_a_quit_is_finished_at_the_next_start(fresh_db, monkeypatch):
    folder = db.AUDIO_DIR / "7"
    folder.mkdir(parents=True)
    (folder / "mic.pcm").write_bytes(speech(1).tobytes())
    done = []
    monkeypatch.setattr(pipeline, "_compress_soon", lambda folders: done.extend(folders))
    pipeline.recover_unfinished()
    assert done == [folder]


def _session(fresh_db_unused=None, **cfg) -> live.LiveSession:
    mid = db.create_meeting("Standup", title_auto=False)
    return live.LiveSession(mid, db.AUDIO_DIR / str(mid), draft=False,
                            cfg={"live_final": True, "transcribe_engine": "local", **cfg})


def test_stops_after_the_call_ends(fresh_db, fake_model):
    session = _session()
    on, off = {"available": True, "call_app": "Zoom"}, {"available": True, "call_app": None}
    for t in range(0, 300, 5):
        assert session.end_of_meeting(on, now=t) is None
    assert session.end_of_meeting(off, now=300) is None  # a hiccup, so far
    assert session.end_of_meeting(off, now=320) is None
    assert session.end_of_meeting(on, now=325) is None  # it was: still in the call
    assert session.end_of_meeting(off, now=340) is None
    assert session.end_of_meeting(off, now=356) == "The Zoom call ended"
    session.close()


def test_a_notification_sound_is_not_a_call(fresh_db, fake_model):
    session = _session()
    on, off = {"available": True, "call_app": "Slack"}, {"available": True, "call_app": None}
    for t in range(0, 15, 5):
        assert session.end_of_meeting(on, now=t) is None
    for t in range(15, 200, 5):
        assert session.end_of_meeting(off, now=t) is None
    session.close()


def test_losing_touch_with_the_recorder_is_not_the_call_ending(fresh_db, fake_model):
    session = _session()
    for t in range(0, 300, 5):
        session.end_of_meeting({"available": True, "call_app": "Zoom"}, now=t)
    assert session.end_of_meeting({"available": False}, now=1000) is None
    session.close()


def test_stops_after_ten_quiet_minutes_only_once_someone_has_spoken(fresh_db, fake_model, monkeypatch):
    nobody = {"available": True, "call_app": None}
    session = _session()
    session._pending_start = {0: 700 * SR, 1: 0}
    session._written = {0: 700 * SR, 1: 0}
    assert session.end_of_meeting(nobody, now=0) is None  # no words yet: you may just be early
    session._last_speech = 110.0
    assert session.end_of_meeting(nobody, now=0) is None  # 590 s
    session._last_speech = 90.0
    assert session.end_of_meeting(nobody, now=0) == "Nobody has spoken for 10 minutes"
    assert session.end_of_meeting({"available": True, "call_app": "Zoom"}, now=0) is None  # on hold, or a long pause
    session.close()


def test_quiet_only_counts_audio_that_was_actually_transcribed(fresh_db, fake_model):
    """A track far behind says nothing about the audio it hasn't reached."""
    session = _session()
    session._last_speech = 90.0
    session._written = {0: 700 * SR, 1: 0}
    session._pending_start = {0: 200 * SR, 1: 0}
    assert session.end_of_meeting({"available": True, "call_app": None}, now=0) is None
    session.close()


def test_a_failed_chunk_does_not_look_like_silence(fresh_db, fake_model, monkeypatch):
    def broken(audio, prompt=None, model=None):
        raise RuntimeError("out of memory")

    monkeypatch.setattr(live.local_engine, "transcribe_final", broken)
    session = _session()
    session.write(bytes([0]) + speech(8, pause_at=5).tobytes())
    session.draft_step()
    assert session._last_speech == 0.0 and session.missed[0] == [[0, int(5.5 * SR)]]
    session.close()


def test_a_track_that_never_pauses_catches_up_after_falling_behind():
    """A mic with a steady hum has no quiet frames. At 5 s a round it could never make up for a slow moment."""
    hum = np.random.default_rng(2).uniform(-0.03, 0.03, 300 * SR).astype(np.float32)
    assert live.rms(hum) > live.SILENCE_RMS
    cut = live.find_cut(hum)
    assert int(58.5 * SR) <= cut <= 60 * SR  # most of a minute at once
    assert live.find_cut(hum[:int(30 * SR)]) >= 28 * SR  # all of a shorter backlog
    assert live.find_cut(hum[:int(6 * SR)]) <= 5 * SR  # not behind: as before


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
    assert f"more than {platform_words.this_computer()} can spare" in resources.check(15, "The summary model")


def test_a_small_server_can_still_run_a_small_model(monkeypatch):
    monkeypatch.setattr(resources, "_disk_free_share", lambda: 0.5)
    monkeypatch.setattr(resources, "memory_pressure", lambda: 1)
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: _VM(0.6, total_gb=2))  # a 2 GB NAS
    assert resources.check(0.5, "The transcription model") is None  # the tiny model: swap if need be
    assert "can spare" in resources.check(1.6, "The transcription model")  # turbo doesn't fit
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: _VM(1, total_gb=8))
    assert "can spare" in resources.check(6.5, "The summary model")  # 8 GB and up: 2 GB kept, as before


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

    def transcribe_final(audio, prompt=None, model=None):
        prompts.append(prompt)
        return [{"start": 0.0, "end": 1.0, "text": f"part {len(prompts)}"}]

    monkeypatch.setattr(live.local_engine, "transcribe_final", transcribe_final)
    mid = db.create_meeting("Standup", title_auto=False)
    folder = db.AUDIO_DIR / str(mid)
    session = live.LiveSession(mid, folder, draft=True, cfg={"live_final": True, "transcribe_engine": "local"})
    session.write(bytes([0]) + speech(4, pause_at=2.5).tobytes())
    session.draft_step()
    session.write(bytes([0]) + speech(4, pause_at=2.5).tobytes())
    session.draft_step()
    session.close()
    assert prompts == ["", "part 1"]
