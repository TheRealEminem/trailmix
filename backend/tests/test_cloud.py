import json
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

import audio_store as store
import catch_up
import cloud
import database as db
import live
import live_notes
import llm_engine
import preflight
import settings
import transcribe_gemini

SR = store.SAMPLE_RATE
PRO = {"plan": "pro", "status": "active", "level": "pro", "pro_hours_used": 3.0, "pro_hours_limit": 50.0}
USED_UP = {**PRO, "level": "basic", "pro_hours_used": 50.0}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    monkeypatch.setenv("TRAILMIX_CLOUD_URL", "https://cloud.example")
    db.init_db()
    return tmp_path


def cfg(account=PRO, **kw):
    return {**settings.DEFAULTS, "cloud_token": "tmc_device", "cloud_account": account, "transcribe_engine": "trailmix",
            "summary_provider": "trailmix", **kw}


def reply(status=200, body=None, level="pro"):
    return httpx.Response(status, json=body if body is not None else {}, headers={"x-trailmix-level": level},
                          request=httpx.Request("POST", "https://cloud.example"))


# ── Signing in ──────────────────────────────────────────────────────────

def test_signing_in_saves_the_token_and_the_account(fresh_db, monkeypatch):
    answers = iter([reply(202, {"status": "pending"}), reply(200, {"token": "tmc_new"}), reply(200, PRO)])
    sent = []

    def request(method, url, headers=None, **kw):
        sent.append((method, url, headers))
        return next(answers)

    monkeypatch.setattr(cloud.httpx, "request", request)
    assert cloud.poll("ABCD")["status"] == "pending"
    done = cloud.poll("ABCD")
    assert done["status"] == "done" and done["account"]["plan"] == "pro"
    cfg_now = settings.get_all()
    assert cfg_now["cloud_token"] == "tmc_new" and cfg_now["cloud_account"]["level"] == "pro"
    assert sent[-1] == ("GET", "https://cloud.example/v1/account", {"Authorization": "Bearer tmc_new"})


def test_the_settings_page_cannot_set_the_token(fresh_db):
    with pytest.raises(ValueError):
        settings.update({"cloud_token": "tmc_forged"})


def test_signing_out_forgets_the_token_even_if_the_server_is_unreachable(fresh_db, monkeypatch):
    settings.update({"cloud_token": "tmc_x", "cloud_account": PRO}, internal=True)

    def unreachable(*a, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(cloud.httpx, "request", unreachable)
    cloud.sign_out()
    assert settings.get_all()["cloud_token"] == "" and settings.get_all()["cloud_account"] == {}


# ── Notes through Trailmix Cloud ────────────────────────────────────────

def test_notes_go_to_the_cloud_by_role_and_cost_nothing_extra(fresh_db, monkeypatch):
    seen = []

    def post(url, **kw):
        seen.append((url, kw["headers"]))
        return SimpleNamespace(json=lambda: {"candidates": [{"content": {"parts": [{"text": "Notes"}]}}],
                                             "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 100}})

    monkeypatch.setattr(llm_engine, "_post", post)
    with llm_engine.purpose("notes", 3):
        assert llm_engine.generate("trailmix", cfg(), "transcript") == "Notes"
    llm_engine.generate("trailmix", cfg(), "a quick question")
    assert seen[0] == ("https://cloud.example/v1beta/models/notes:generateContent", {"Authorization": "Bearer tmc_device"})
    assert seen[1][0].endswith("/models/quick:generateContent")
    rows = db.usage_since("2000-01-01")
    assert {r["engine"] for r in rows} == {"trailmix"} and all(r["cost"] == 0 for r in rows)


def test_a_refusal_shows_the_servers_own_sentence(fresh_db, monkeypatch):
    refused = reply(402, {"error": {"message": "Your free hours are used up. Pick a plan to keep going."}}, "basic")
    monkeypatch.setattr(llm_engine.httpx, "post", lambda url, **kw: refused)
    with pytest.raises(llm_engine.LLMError, match="^Your free hours are used up"):
        llm_engine.generate("trailmix", cfg(), "q")


def test_signed_out_says_how_to_sign_in(fresh_db):
    ok, why = llm_engine.configured("trailmix", cfg(cloud_token=""))
    assert not ok and "Sign in to Trailmix Cloud" in why
    assert "Sign in" in preflight.engine_problem("trailmix", cfg(cloud_token=""))


def test_lockdown_keeps_trailmix_cloud_out(fresh_db):
    assert not llm_engine.configured("trailmix", cfg(lockdown=True))[0]
    assert "Lockdown" in preflight.engine_problem("trailmix", cfg(lockdown=True))


# ── Levels ──────────────────────────────────────────────────────────────

def test_the_basic_level_switches_off_the_live_features_with_a_reason():
    assert cloud.features_off(cfg()) == {}
    off = cloud.features_off(cfg(account={"plan": "basic", "status": "active", "level": "basic"}))
    assert set(off) == {"live_captions", "catch_up", "live_notes"} and "part of Trailmix Cloud Pro" in off["catch_up"]
    assert "live hours are used" in cloud.features_off(cfg(account=USED_UP))["live_captions"]
    assert cloud.features_off(cfg(account=USED_UP, transcribe_engine="local", summary_provider="ollama")) == {}


def test_a_lapsed_or_unknown_account_is_basic():
    assert cloud.level(cfg(account={**PRO, "status": "lapsed"})) == "basic"
    assert cloud.level(cfg(account={})) == "basic"
    assert cloud.level(cfg(cloud_token="")) == "basic"


def test_live_notes_and_catch_up_say_why_they_are_off_on_basic(fresh_db):
    basic = cfg(account={"plan": "basic", "status": "active", "level": "basic"})
    assert not live_notes.decide(basic)["on"] and "Pro" in live_notes.decide(basic)["reason"]
    assert catch_up.providers(basic) == []


def test_a_reply_that_says_the_level_changed_updates_the_account(fresh_db):
    settings.update({"cloud_token": "tmc_x", "cloud_account": PRO}, internal=True)
    cloud.note_level("basic")
    assert settings.get_all()["cloud_account"]["level"] == "basic"


# ── Near-live captions ──────────────────────────────────────────────────

def speech(seconds: float, pause_at: float | None = None) -> np.ndarray:
    audio = (np.random.default_rng(1).uniform(-0.3, 0.3, int(seconds * SR)) * 32767).astype(np.int16)
    if pause_at is not None:
        audio[int(pause_at * SR):int((pause_at + 0.5) * SR)] = 0
    return audio


@pytest.fixture
def session_for(fresh_db, monkeypatch):
    monkeypatch.setattr(live.vad, "speech_regions", lambda a: [(0, len(a))] if len(a) else [])
    monkeypatch.setattr(live, "warm_up", lambda cfg, now=False: False)
    made = []

    def make(settings_now):
        mid = db.create_meeting("Call", title_auto=False)
        folder = db.AUDIO_DIR / str(mid)
        db.update_meeting(mid, audio_dir=str(folder), status="recording")
        made.append(live.LiveSession(mid, folder, draft=True, cfg=settings_now))
        return made[-1]

    yield make
    for s in made:
        s.close()


def test_at_the_pro_level_clips_cut_at_pauses_are_the_captions_and_the_transcript(session_for, monkeypatch):
    clips = []

    def transcribe_clip(audio, cred, language, model, context="", usage=None):
        clips.append((len(audio) / SR, cred, model))
        return [{"start": 0.0, "end": 1.0, "text": "hello there"}]

    monkeypatch.setattr(live.transcribe_gemini, "transcribe_clip", transcribe_clip)
    session = session_for(cfg())
    assert session.cloud_live and session.final and session.polls_drafts
    session.write(bytes([0]) + speech(5).tobytes())
    assert session.draft_step() == []  # too short to send: clips are at least 8 s
    session.write(bytes([0]) + speech(7, pause_at=4).tobytes())
    lines = session.draft_step()
    assert clips == [(9.5, "tmc_device", "transcribe_chunk")]  # cut at the pause, 9.5 s in
    assert lines[0]["text"] == "hello there"
    session.finish()
    saved = json.loads(db.get_meeting(session.meeting_id)["live_json"])
    assert saved["complete"] and saved["engine"] == "trailmix" and saved["tracks"]["mic"][0]["text"] == "hello there"
    assert db.usage_since("2000-01-01")[0]["kind"] == "live captions"


def test_at_the_basic_level_nothing_is_sent_while_recording(session_for):
    session = session_for(cfg(account=USED_UP))
    assert not session.cloud_live and not session.final and not session.polls_drafts
    session.write(bytes([0]) + speech(20, pause_at=10).tobytes())
    assert session.draft_step() == [] and session._pending == {0: [], 1: []}
    assert "live hours are used" in session.captions_off_reason


def test_running_out_of_live_hours_mid_meeting_stops_the_captions(session_for, monkeypatch):
    def refused(*a, **kw):
        raise transcribe_gemini.BasicLevel("This month's live hours are used.")

    monkeypatch.setattr(live.transcribe_gemini, "transcribe_clip", refused)
    session = session_for(cfg())
    session.write(bytes([0]) + speech(12, pause_at=9).tobytes())
    out = session.draft_step()
    assert out[0]["type"] == "status" and "Live captions stop here" in out[0]["message"]
    assert not session.final and not session.final_complete and not session.polls_drafts
    session.finish()
    assert db.get_meeting(session.meeting_id)["live_json"] is None  # the pipeline transcribes it all afterwards


def test_a_clip_refused_for_the_level_raises_basic_level(fresh_db, monkeypatch):
    monkeypatch.setattr(transcribe_gemini.httpx, "request", lambda *a, **kw: reply(
        409, {"error": {"message": "This month's live hours are used."}, "level": "basic"}, "basic"))
    monkeypatch.setattr(transcribe_gemini, "encode", lambda audio: b"ogg")
    with pytest.raises(transcribe_gemini.BasicLevel, match="live hours"):
        transcribe_gemini.transcribe_clip(np.zeros(SR, dtype=np.float32), "tmc_x", None, "transcribe_chunk")


def test_a_clip_comes_back_as_timed_lines(fresh_db, monkeypatch):
    turns = json.dumps([{"start": "00:01", "speaker": "Speaker 1", "text": "Morning all"},
                        {"start": "00:04", "speaker": "Speaker 1", "text": "shall we start"}])
    monkeypatch.setattr(transcribe_gemini.httpx, "request", lambda *a, **kw: reply(
        200, {"candidates": [{"content": {"parts": [{"text": turns}]}}]}))
    monkeypatch.setattr(transcribe_gemini, "encode", lambda audio: b"ogg")
    lines = transcribe_gemini.transcribe_clip(np.zeros(6 * SR, dtype=np.float32), "tmc_x", None, "transcribe_chunk")
    assert [(round(l["start"]), l["text"]) for l in lines] == [(1, "Morning all"), (4, "shall we start")]
