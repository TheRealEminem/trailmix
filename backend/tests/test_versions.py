import json
import time

import pytest
from fastapi.testclient import TestClient

import compare
import database as db
import main
import pipeline
import transcribe_gemini


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    return tmp_path


def meeting_with_transcript(text="[00:00] Them: hello there", made_with="whisper-large-v3-turbo, after the meeting"):
    mid = db.create_meeting("Weekly sync", title_auto=False)
    segs = [{"start": 0, "end": 2, "speaker": "Them", "text": "hello there"}]
    db.update_meeting(mid, transcript=text, segments_json=segs, transcribed=1, transcribed_with=made_with,
                      status="done", duration_sec=2)
    return mid


# ── history ──

def test_every_change_to_a_tracked_field_is_recorded_with_old_and_new(fresh_db):
    mid = meeting_with_transcript()
    db.update_meeting(mid, why="you renamed it", title="Budget review")
    db.update_meeting(mid, title="Budget review")      # no change: nothing recorded
    db.update_meeting(mid, status="queued")             # bookkeeping isn't history
    rows = db.history_for(mid, "title")
    assert len(rows) == 1
    assert (rows[0]["old_value"], rows[0]["new_value"], rows[0]["why"]) == ("Weekly sync", "Budget review", "you renamed it")


def test_replacing_a_transcript_keeps_the_old_text_in_the_history(fresh_db):
    mid = meeting_with_transcript()
    db.update_meeting(mid, why="redone", transcript="[00:00] Speaker 1: hello there friend")
    row = db.history_for(mid, "transcript")[0]
    assert row["old_value"] == "[00:00] Them: hello there" and row["new_value"].endswith("friend")


def test_deleting_a_meeting_deletes_its_history_and_versions(fresh_db):
    mid = meeting_with_transcript()
    db.update_meeting(mid, title="x")
    db.transcript_versions(mid)
    db.delete_meeting(mid)
    assert db.history_for(mid) == [] and db.transcript_versions(mid) == []


# ── versions ──

def test_a_meeting_from_before_versions_gets_an_original(fresh_db):
    mid = meeting_with_transcript()
    versions = db.transcript_versions(mid)
    assert [(v["source"], v["applied"], v["made_with"]) for v in versions] == [
        ("original", 1, "whisper-large-v3-turbo, after the meeting")]
    assert len(db.transcript_versions(mid)) == 1  # not added again


def test_saving_a_new_transcript_keeps_the_old_one_as_a_version(fresh_db):
    mid = meeting_with_transcript()
    m = db.get_meeting(mid)
    pipeline._save_transcript(m, [{"start": 0, "end": 2, "speaker": "Speaker 1", "text": "hello there friend"}],
                              False, 2.0, made_with="gemini-3.5-transcribe, after the meeting",
                              engine="gemini", model="gemini-3.5-transcribe", seconds=5)
    versions = db.transcript_versions(mid)
    assert [(v["source"], v["applied"]) for v in versions] == [("original", 0), ("recording", 1)]
    assert db.get_meeting(mid)["transcript"].endswith("Speaker 1: hello there friend")
    assert db.get_meeting(mid)["transcribed_with"].startswith("gemini")


def test_switching_back_to_an_earlier_version(fresh_db):
    mid = meeting_with_transcript()
    original = db.transcript_versions(mid)[0]["id"]
    pipeline._save_transcript(db.get_meeting(mid), [{"start": 0, "end": 2, "speaker": None, "text": "something else"}],
                              False, 2.0, made_with="other", engine="gemini", model="m")
    pipeline.apply_version(mid, original)
    m = db.get_meeting(mid)
    assert m["transcript"] == "[00:00] Them: hello there" and m["transcribed_with"].startswith("whisper")
    assert [v["applied"] for v in db.transcript_versions(mid)] == [1, 0]
    why = db.history_for(mid, "transcript")[0]["why"]
    assert "you chose the transcript made with whisper" in why


def test_a_version_of_another_meeting_cannot_be_applied(fresh_db):
    a, b = meeting_with_transcript(), meeting_with_transcript()
    other = db.transcript_versions(b)[0]["id"]
    with pytest.raises(ValueError):
        pipeline.apply_version(a, other)


# ── trying another model ──

def test_a_trial_keeps_the_new_transcript_beside_the_old_without_touching_the_meeting(fresh_db, monkeypatch):
    mid = meeting_with_transcript()
    db.update_meeting(mid, audio_dir=str(fresh_db / "audio"), summary="the notes")
    monkeypatch.setattr(pipeline, "_transcribe_gemini", lambda m, cfg: (
        [{"start": 0, "end": 2, "speaker": "Speaker 1", "text": "hello there"},
         {"start": 2, "end": 4, "speaker": "Speaker 2", "text": "hi"}], False, 4.0))
    pipeline._trials[mid] = {"engine": "gemini", "model": None, "state": "queued", "detail": None, "version_id": None, "error": None}
    pipeline._run_trial(mid, "gemini", "gemini-next", apply=False, summarize=False)
    trial = pipeline.trial_status(mid)
    assert trial["state"] == "done"
    versions = db.transcript_versions(mid)
    assert [(v["source"], v["applied"], v["model"]) for v in versions] == [
        ("original", 1, None), ("upgrade", 0, "gemini-next")]
    m = db.get_meeting(mid)
    assert m["transcript"] == "[00:00] Them: hello there" and m["summary"] == "the notes" and m["status"] == "done"


def test_a_trial_that_fails_says_why_and_changes_nothing(fresh_db, monkeypatch):
    mid = meeting_with_transcript()
    db.update_meeting(mid, audio_dir=str(fresh_db / "audio"))

    def fail(m, cfg):
        raise RuntimeError("Google didn't accept the API key")

    monkeypatch.setattr(pipeline, "_transcribe_gemini", fail)
    pipeline._trials[mid] = {"engine": "gemini", "model": None, "state": "queued", "detail": None, "version_id": None, "error": None}
    pipeline._run_trial(mid, "gemini", None, apply=False, summarize=False)
    assert pipeline.trial_status(mid)["state"] == "failed" and "API key" in pipeline.trial_status(mid)["error"]
    assert len(db.transcript_versions(mid)) == 1


# ── comparing ──

def test_diff_counts_agreement_and_lists_the_differences():
    d = compare.diff("the new laptops arrive on october 20th a week later than we hoped",
                     "the new laptops arrive on october 20th a week later than hoped")
    assert d["matched"] == 12 and d["agreement"] == round(12 / 13, 4)
    assert d["changes"] == [{"op": "delete", "a": "we", "b": "", "at": 11}]


def test_diff_ignores_case_punctuation_timestamps_and_speaker_labels():
    a = compare.plain(None, "[00:05] Them: Hello, there!\n[00:09] You: Fine.")
    b = compare.plain([{"text": "hello there"}, {"text": "fine"}])
    assert compare.diff(a, b)["agreement"] == 1.0


def test_inline_marks_what_only_one_side_has():
    out = compare.inline("a b c d", "a x c d")
    assert [(o["op"], o["text"]) for o in out] == [("equal", "a"), ("delete", "b"), ("insert", "x"), ("equal", "c d")]


# ── the API ──

@pytest.fixture
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_warm_loop", lambda: _never())
    return TestClient(main.app)


async def _never():
    return None


def test_api_lists_versions_and_compares_them(client, fresh_db, monkeypatch):
    mid = meeting_with_transcript()
    pipeline._save_transcript(db.get_meeting(mid), [{"start": 0, "end": 2, "speaker": "Speaker 1", "text": "hello there friend"}],
                              False, 2.0, made_with="gemini", engine="gemini", model="g")
    listing = client.get(f"/api/meetings/{mid}/versions").json()
    ids = [v["id"] for v in listing["versions"]]
    assert len(ids) == 2 and listing["versions"][1]["speakers"] == ["Speaker 1"]
    cmp = client.get(f"/api/meetings/{mid}/compare", params={"a": ids[0], "b": ids[1]}).json()
    assert cmp["a_words"] == 2 and cmp["b_words"] == 3 and cmp["changes"][0]["b"] == "friend"
    assert client.get(f"/api/meetings/{mid}/versions/{ids[0]}").json()["transcript"] == "[00:00] Them: hello there"
    history = client.get(f"/api/meetings/{mid}/history", params={"field": "transcript"}).json()
    assert history and history[0]["new_value"].endswith("friend")


def test_api_refuses_a_trial_it_cannot_run(client, fresh_db, monkeypatch):
    mid = meeting_with_transcript()
    db.update_meeting(mid, audio_dir=str(fresh_db / "audio"))
    monkeypatch.setattr(main.store, "audio_bytes", lambda m: 100)
    r = client.post(f"/api/meetings/{mid}/trial", json={"engine": "gemini"})
    assert r.status_code == 409 and "Google API key" in r.json()["detail"]
    assert client.post(f"/api/meetings/{mid}/trial", json={"engine": "nope"}).status_code == 422


# ── upgrading transcripts with a better speech engine ──

def test_speech_models_are_ranked_and_their_names_read_from_the_note_of_how_they_were_made():
    import model_ranks

    assert model_ranks.speech_model("whisper-large-v3-turbo, after the meeting") == "whisper-large-v3-turbo"
    assert model_ranks.speech_model("mlx-community/whisper-large-v3-turbo, while recording") == "whisper-large-v3-turbo"
    assert model_ranks.better("gemini-3.5-transcribe", "whisper-large-v3-turbo")
    assert not model_ranks.better("whisper-large-v3-turbo", "gemini-3.5-transcribe")
    assert not model_ranks.better("whisper-large-v3-turbo", "whisper-large-v3")  # not clearly better


def test_upgrades_offer_transcripts_a_better_engine_you_have_would_redo(client, fresh_db, monkeypatch):
    weak = meeting_with_transcript(made_with="whisper-large-v3-turbo, after the meeting")
    strong = meeting_with_transcript(made_with="gemini-3.5-transcribe, after the meeting")
    gone = meeting_with_transcript()
    for mid in (weak, strong):
        db.update_meeting(mid, audio_dir=str(fresh_db / "audio"))
    db.update_meeting(gone, audio_deleted=1)  # no audio: it can't be redone
    monkeypatch.setattr(main.settings, "get_all", lambda: {**main.settings.DEFAULTS, "gemini_api_key": "k", "lockdown": False})
    monkeypatch.setattr(main.mlx_engine, "available", lambda: False)
    ups = client.get("/api/upgrades").json()["transcripts"]
    assert ups["best"]["engine"] == "gemini" and ups["count"] == 1
    assert ups["from"] == {"whisper-large-v3-turbo": 1}


def test_a_key_in_lockdown_does_not_count_as_a_better_engine(client, fresh_db, monkeypatch):
    mid = meeting_with_transcript()
    db.update_meeting(mid, audio_dir=str(fresh_db / "audio"))
    monkeypatch.setattr(main.settings, "get_all", lambda: {**main.settings.DEFAULTS, "gemini_api_key": "k", "lockdown": True})
    monkeypatch.setattr(main.mlx_engine, "available", lambda: False)
    assert client.get("/api/upgrades").json()["transcripts"]["best"] is None


def test_bulk_trials_queue_only_meetings_with_audio_and_can_be_cancelled(client, fresh_db, monkeypatch):
    a, b = meeting_with_transcript(), meeting_with_transcript()
    db.update_meeting(a, audio_dir=str(fresh_db / "audio"))
    monkeypatch.setattr(main.settings, "get_all", lambda: {**main.settings.DEFAULTS, "gemini_api_key": "k", "lockdown": False})
    monkeypatch.setattr(main.store, "audio_bytes", lambda m: 100 if m["id"] == a else 0)
    started = []
    monkeypatch.setattr(pipeline, "_executor", type("X", (), {"submit": lambda self, *args: started.append(args)})())
    pipeline._trials.clear()
    r = client.post("/api/transcript-trials", json={"engine": "gemini", "meeting_ids": [a, b]}).json()
    assert r == {"queued": 1, "skipped": 1} and len(started) == 1
    assert client.get("/api/transcript-trials").json()["counts"] == {"queued": 1}
    assert client.delete("/api/transcript-trials").json() == {"cancelled": 1}
    pipeline._run_trial(a, "gemini", None, False, False)  # a cancelled one does nothing when its turn comes
    assert pipeline.trial_status(a)["state"] == "cancelled"
    assert len(db.transcript_versions(a)) == 1


# ── choosing the engine for one recording ──

def test_a_recording_remembers_the_engine_picked_for_it(client, fresh_db, monkeypatch):
    monkeypatch.setattr(main.settings, "get_all", lambda: {**main.settings.DEFAULTS, "gemini_api_key": "k", "lockdown": False})
    r = client.post("/api/meetings", json={"title": "", "engine": "gemini"})
    assert r.status_code == 201
    assert db.get_meeting(r.json()["id"])["requested_engine"] == "gemini"
    db.update_meeting(r.json()["id"], status="done")  # not recording any more
    plain = client.post("/api/meetings", json={"title": ""})
    assert plain.status_code == 201 and db.get_meeting(plain.json()["id"])["requested_engine"] is None


def test_a_recording_with_an_engine_that_cannot_work_is_refused_up_front(client, fresh_db, monkeypatch):
    monkeypatch.setattr(main.settings, "get_all", lambda: {**main.settings.DEFAULTS, "gemini_api_key": "", "lockdown": False})
    r = client.post("/api/meetings", json={"title": "", "engine": "gemini"})
    assert r.status_code == 409 and "Google API key" in r.json()["detail"]


def test_the_pipeline_uses_the_engine_the_recording_asked_for(fresh_db, monkeypatch):
    mid = db.create_meeting("x", title_auto=False, requested_engine="gemini")
    db.update_meeting(mid, status="queued", audio_dir=str(fresh_db / "audio"))
    seen = {}

    def fake(m, cfg):
        seen["engine"] = cfg["transcribe_engine"]
        return [{"start": 0, "end": 1, "speaker": None, "text": "hello"}], False, 1.0

    monkeypatch.setattr(pipeline, "_transcribe_gemini", fake)
    monkeypatch.setattr(pipeline.store, "compress_pending", lambda m: None)
    monkeypatch.setattr(pipeline.settings, "get_all", lambda: {**pipeline.settings.DEFAULTS, "auto_summarize": False,
                                                              "transcribe_engine": "local", "auto_export": False})
    pipeline._run_stages(mid, frozenset(), frozenset())
    assert seen["engine"] == "gemini"
    assert db.get_meeting(mid)["transcribed_with"].startswith("gemini-3.5-transcribe")
