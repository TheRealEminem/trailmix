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


def test_notes_saved_as_you_type_make_one_history_entry_per_stretch(fresh_db):
    mid = meeting_with_transcript()
    client = TestClient(main.app)
    for text in ("# Plan", "# Plan\n", "# Plan B"):
        client.put(f"/api/meetings/{mid}/my-notes", json={"notes": [{"t": 3.0, "text": text}, {"t": 9.0, "text": "  - nested"}]})
    rows = db.history_for(mid, "my_notes_json")
    assert len(rows) == 1 and rows[0]["old_value"] is None
    assert json.loads(rows[0]["new_value"]) == [{"t": 3.0, "text": "# Plan B"}, {"t": 9.0, "text": "  - nested"}]
    db.update_meeting(mid, why="you renamed it", title="Other")          # another field doesn't end the stretch
    client.put(f"/api/meetings/{mid}/my-notes", json={"notes": [{"t": 3.0, "text": "# Plan C"}]})
    assert len(db.history_for(mid, "my_notes_json")) == 1
    client.put(f"/api/meetings/{mid}/my-notes", json={"notes": []})      # typed back to nothing: no change after all
    assert db.history_for(mid, "my_notes_json") == []


def test_a_merge_only_joins_an_entry_from_the_same_kind_of_change(fresh_db):
    mid = meeting_with_transcript()
    db.update_meeting(mid, why="merged a copy", my_notes_json=[{"t": None, "text": "from the copy"}])
    db.update_meeting(mid, why="you edited your notes", merge_s=120, my_notes_json=[{"t": None, "text": "mine"}])
    assert [r["why"] for r in db.history_for(mid, "my_notes_json")] == ["you edited your notes", "merged a copy"]


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
    assert (r["queued"], r["skipped"]) == (1, 1) and len(started) == 1
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


# ── spending a little at a time ──

def test_prices_per_minute_of_audio():
    import pricing

    assert pricing.per_minute("gemini", "gemini-3.5-transcribe") == 0.005
    assert pricing.per_minute("gemini", "models/gemini-3.5-transcribe-live") == 0.009
    assert pricing.per_minute("local", "whisper-large-v3-turbo") == 0.0
    assert pricing.per_minute("remote", "some-self-hosted-model") is None
    assert pricing.meeting_minutes(1800, True) == 60 and pricing.meeting_minutes(1800, False) == 30
    assert pricing.cost(60, "gemini", "gemini-3.5-transcribe") == 0.3


def _meetings_with_audio(fresh_db, n):
    ids = []
    for i in range(n):
        mid = meeting_with_transcript()
        db.update_meeting(mid, audio_dir=str(fresh_db / "audio"), duration_sec=1800, has_system=1,
                          created_at=f"2026-10-{10 + i:02d}T10:00:00.000Z")
        ids.append(mid)
    return ids


def test_bulk_trials_default_to_three_newest_and_estimate_the_cost_first(client, fresh_db, monkeypatch):
    ids = _meetings_with_audio(fresh_db, 5)
    monkeypatch.setattr(main.settings, "get_all", lambda: {**main.settings.DEFAULTS, "gemini_api_key": "k", "lockdown": False})
    monkeypatch.setattr(main.store, "audio_bytes", lambda m: 100)
    started = []
    monkeypatch.setattr(pipeline, "_executor", type("X", (), {"submit": lambda self, *args: started.append(args)})())
    pipeline._trials.clear()
    dry = client.post("/api/transcript-trials", json={"engine": "gemini", "scope": "all", "dry_run": True}).json()
    assert [r["id"] for r in dry["meetings"]] == [ids[4], ids[3], ids[2]]     # newest first, three
    assert dry["available"] == 5 and dry["minutes"] == 90.0                      # 3 x 30 min, the two tracks mixed into one
    assert dry["cost"] == 0.45 and dry["per_minute"] == 0.005
    assert started == [] and pipeline._trials == {}                              # a dry run spends nothing
    real = client.post("/api/transcript-trials", json={"engine": "gemini", "scope": "all", "limit": 1}).json()
    assert real["queued"] == 1 and real["cost"] == 0.15 and len(started) == 1


def test_a_free_engine_estimates_to_zero_and_an_unknown_price_to_none(client, fresh_db, monkeypatch):
    _meetings_with_audio(fresh_db, 1)
    monkeypatch.setattr(main.settings, "get_all", lambda: {**main.settings.DEFAULTS, "transcribe_url": "http://box:8770/v1",
                                                          "transcribe_model": "my-model", "lockdown": False})
    monkeypatch.setattr(main.store, "audio_bytes", lambda m: 100)
    pipeline._trials.clear()
    unknown = client.post("/api/transcript-trials", json={"engine": "remote", "scope": "all", "dry_run": True}).json()
    assert unknown["cost"] is None and unknown["minutes"] == 60.0   # a remote endpoint is sent each track


def test_usage_is_tallied_by_engine_and_summed(client, fresh_db):
    db.log_usage(1, "transcript", "gemini", "gemini-3.5-transcribe", 3600, 0.3)
    db.log_usage(1, "live captions", "gemini", "gemini-3.5-transcribe-live", 1800, 0.27)
    db.log_usage(2, "transcript", "remote", "weird", 600, None)
    u = client.get("/api/usage").json()
    assert u["cost"] == 0.57 and u["unpriced"] == 1
    assert {(r["kind"], r["engine"]) for r in u["rows"]} == {("transcript", "gemini"), ("live captions", "gemini"), ("transcript", "remote")}
    assert client.get("/api/usage", params={"days": 0}).json()["rows"] == []


def test_a_gemini_transcript_is_logged_with_its_estimated_cost(fresh_db, monkeypatch):
    import numpy as np

    mid = meeting_with_transcript()
    db.update_meeting(mid, audio_dir=str(fresh_db / "audio"))
    audio = (np.ones(16000 * 60, dtype=np.float32) * 0.2)
    monkeypatch.setattr(pipeline.store, "load_track", lambda folder, name: audio if name == "system" else None)
    monkeypatch.setattr(pipeline.transcribe_gemini, "transcribe", lambda *a, **k: [
        {"start": 0, "end": 1, "speaker": "Speaker 1", "text": "hello"}])
    pipeline._transcribe_gemini(db.get_meeting(mid), {**pipeline.settings.DEFAULTS, "gemini_api_key": "k"})
    row = db.usage_since("2000-01-01")[0]
    assert (row["engine"], row["seconds"], row["cost"]) == ("gemini", 60.0, 0.005)


def test_a_trial_can_send_the_tracks_separately_and_the_version_says_how(fresh_db, monkeypatch):
    mid = meeting_with_transcript()
    db.update_meeting(mid, audio_dir=str(fresh_db / "audio"))
    seen = []

    def fake(m, cfg):
        seen.append(cfg["gemini_mix_tracks"])
        return [{"start": 0, "end": 2, "speaker": "You", "text": "hello there"}], True, 2.0

    monkeypatch.setattr(pipeline, "_transcribe_gemini", fake)
    for mix in (True, False):
        pipeline._trials[mid] = {"engine": "gemini", "model": None, "state": "queued", "detail": None, "version_id": None, "error": None}
        pipeline._run_trial(mid, "gemini", None, False, False, mix)
    assert seen == [True, False]
    notes = [v["note"] for v in db.transcript_versions(mid) if v["source"] == "upgrade"]
    assert notes == ["mic and call mixed into one recording", "mic and call sent as separate recordings"]


def test_the_estimate_follows_whether_tracks_are_mixed(client, fresh_db, monkeypatch):
    mid = meeting_with_transcript()
    db.update_meeting(mid, duration_sec=3600, has_system=1)
    monkeypatch.setattr(main.settings, "get_all", lambda: {**main.settings.DEFAULTS, "gemini_api_key": "k", "lockdown": False})
    one = client.get(f"/api/meetings/{mid}/trial/estimate", params={"engine": "gemini"}).json()
    two = client.get(f"/api/meetings/{mid}/trial/estimate", params={"engine": "gemini", "mix_tracks": "false"}).json()
    assert (one["minutes"], one["cost"], two["minutes"], two["cost"]) == (60.0, 0.3, 120.0, 0.6)


# ── economy transcripts from a general model ──

def test_the_economy_model_is_routed_through_generate_content_not_the_transcribe_api():
    import transcribe_gemini as g

    assert g.uses_transcribe_api("gemini-3.5-transcribe") and not g.uses_transcribe_api("gemini-3.5-flash-lite")


def test_turns_from_a_general_model_become_timed_segments():
    import transcribe_gemini as g

    turns = g.parse_turns(json.dumps([
        {"start": "00:00", "speaker": "Speaker 1", "text": "Okay, let's get started."},
        {"start": "00:05", "speaker": "Speaker 2", "text": "Thanks."},
        {"start": "1:02:03", "speaker": "Speaker 1", "text": "  "},                      # empty: dropped
        {"start": "1:02:10", "speaker": "Speaker 2", "text": "Back after the break, everyone."}]))
    assert [(t["speaker"], t["start"]) for t in turns] == [("Speaker 1", 0.0), ("Speaker 2", 5.0), ("Speaker 2", 3730.0)]
    assert turns[0]["end"] <= 5.0 and turns[1]["end"] > 5.0      # a turn ends where the next begins, or by word count


def test_a_reply_that_is_not_the_asked_for_json_is_an_error_after_one_retry(monkeypatch):
    import transcribe_gemini as g

    calls = []

    class Reply:
        def json(self):
            calls.append(1)
            return {"candidates": [{"content": {"parts": [{"text": "Sure! Here's the transcript: ..."}]}, "finishReason": "STOP"}]}

    monkeypatch.setattr(g, "_request", lambda *a, **k: Reply())
    with pytest.raises(g.GeminiError, match="form asked for"):
        g.call_llm({"uri": "u"}, "audio/ogg", "k", None, "gemini-3.5-flash-lite")
    assert len(calls) == 2


def test_the_economy_path_sends_the_audio_and_reports_the_tokens_used(monkeypatch):
    import transcribe_gemini as g

    seen = {}

    class Reply:
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps([{"start": "00:01", "speaker": "Speaker 1", "text": "hello"}])}]},
                                    "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 34000, "candidatesTokenCount": 7000, "thoughtsTokenCount": 0}}

    def request(method, url, key, **kw):
        seen["url"], seen["body"] = url, kw["json"]
        return Reply()

    monkeypatch.setattr(g, "_request", request)
    usage = {}
    turns = g.call_llm({"uri": "files/abc"}, "audio/ogg", "k", "en", "gemini-3.5-flash-lite", usage)
    assert seen["url"].endswith("models/gemini-3.5-flash-lite:generateContent")
    assert seen["body"]["contents"][0]["parts"][0]["fileData"]["fileUri"] == "files/abc"
    assert "(the language is en)" in seen["body"]["contents"][0]["parts"][1]["text"]
    assert usage == {"in": 34000, "out": 7000} and turns[0]["text"] == "hello"
    import pricing

    assert pricing.transcript_cost("gemini-3.5-flash-lite", 1368, usage) == pytest.approx(0.0277, abs=0.0005)   # exact tokens
    assert pricing.transcript_cost("gemini-3.5-flash-lite", 60, None) == 0.0013                                # a guess before
