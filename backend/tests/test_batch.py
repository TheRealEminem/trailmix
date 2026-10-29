import json

import pytest

import batch_jobs
import database as db
import gemini_batch
import llm_engine
import pricing

SUCCEEDED = {  # the shape Google really returned for a finished job (2026-10-12)
    "name": "batches/abc", "done": True,
    "metadata": {"state": "BATCH_STATE_SUCCEEDED", "batchStats": {"requestCount": "2", "successfulRequestCount": "2"}},
    "response": {"inlinedResponses": {"inlinedResponses": [
        {"response": {"candidates": [{"content": {"parts": [{"text": "## Summary\nThey met.\n\n## Action Items\n- [ ] Dana: send it",
                                                            "thoughtSignature": "xyz"}], "role": "model"}}],
                      "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 800}},
         "metadata": {"key": "m1"}},
        {"error": {"message": "blocked"}, "metadata": {"key": "m2"}},
    ]}},
}


def test_the_results_of_a_finished_job_are_read_by_key():
    out = gemini_batch.parse_results(SUCCEEDED)
    assert out["m1"]["text"].startswith("## Summary") and out["m1"]["usage"]["promptTokenCount"] == 1000
    assert out["m2"] == {"text": None, "error": "blocked", "usage": {}}


def test_results_are_found_wherever_the_reply_puts_them():
    in_metadata = {"metadata": {"output": SUCCEEDED["response"]}}
    assert set(gemini_batch.parse_results(in_metadata)) == {"m1", "m2"}
    assert gemini_batch.parse_results({"dest": {"inlinedResponses": SUCCEEDED["response"]["inlinedResponses"]["inlinedResponses"]}}).keys() == {"m1", "m2"}


@pytest.mark.parametrize("raw,state", [("BATCH_STATE_PENDING", "pending"), ("BATCH_STATE_RUNNING", "running"),
                                       ("JOB_STATE_SUCCEEDED", "succeeded"), ("BATCH_STATE_CANCELLED", "cancelled"),
                                       ("JOB_STATE_EXPIRED", "expired"), ("BATCH_STATE_FAILED", "failed")])
def test_both_spellings_of_the_state_are_understood(monkeypatch, raw, state):
    monkeypatch.setattr(gemini_batch, "_call", lambda *a, **k: {"metadata": {"state": raw}})
    assert gemini_batch.status("batches/x", "k")["state"] == state


def test_big_jobs_are_split_to_fit_google_s_limit(monkeypatch):
    monkeypatch.setattr(gemini_batch, "INLINE_LIMIT_BYTES", 1000)
    reqs = [{"key": f"m{i}", "prompt": "x" * 500} for i in range(5)]
    groups = gemini_batch.split(reqs)
    assert [len(g) for g in groups] == [1, 1, 1, 1, 1] and sum(len(g) for g in groups) == 5


def test_text_prices_and_the_batch_discount():
    assert pricing.text_cost("gemini-3.8-flash", 1_000_000, 0) == 0.75
    assert pricing.text_cost("gemini-3.8-flash", 1_000_000, 0, batch=True) == 0.375
    assert pricing.text_cost("models/gemini-flash-lite-latest", 0, 1_000_000) == 2.5
    assert pricing.text_cost("some-local-model", 10, 10) is None


# ── the whole job ──

@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    cfg = {"gemini_api_key": "k", "gemini_model": "gemini-3.8-flash", "lockdown": False, "custom_template": "",
           "summary_template": "general", "your_name": "Dale", "vocabulary": [], "auto_title": False, "summary_provider": "ollama"}
    monkeypatch.setattr(batch_jobs.settings, "get_all", lambda: cfg)
    sent = {}

    def create(model, requests, key, name="x"):
        sent["model"], sent["requests"] = model, requests
        return "batches/abc"

    monkeypatch.setattr(gemini_batch, "create", create)
    return cfg, sent


def meeting(summary="Old notes by a local model."):
    mid = db.create_meeting("Weekly", title_auto=False)
    segs = [{"start": 0, "end": 5, "speaker": "You", "text": "Dana will send the budget on Friday."}]
    db.update_meeting(mid, transcript="[00:00] You: Dana will send the budget on Friday.", segments_json=segs, summary=summary,
                      summary_provider="ollama", summary_model="qwen2.5:7b", status="done", duration_sec=300, transcribed=1)
    return mid


def test_notes_are_sent_as_one_job_with_the_same_prompt_the_app_writes_notes_with(world):
    cfg, sent = world
    a, b = meeting(), meeting()
    out = batch_jobs.submit_notes([a, b], cfg)
    assert out["submitted"] == 2 and out["skipped"] == [] and len(out["jobs"]) == 1
    assert sent["model"] == "gemini-3.8-flash" and [r["key"] for r in sent["requests"]] == [f"m{a}", f"m{b}"]
    assert "Dana will send the budget" in sent["requests"][0]["prompt"]
    job = db.batch_jobs()[0]
    assert job["name"] == "batches/abc" and job["state"] == "pending" and job["est_cost"] > 0


def test_a_finished_job_replaces_the_notes_and_keeps_the_old_ones_in_the_history(world, monkeypatch):
    cfg, _ = world
    mid = meeting()
    batch_jobs.submit_notes([mid], cfg)
    answer = {"state": "succeeded", "results": {f"m{mid}": {"text": "## Summary\nThe budget.\n\n## Action Items\n- [ ] Dana: send the budget",
                                                         "error": None, "usage": {"promptTokenCount": 2000, "candidatesTokenCount": 300, "thoughtsTokenCount": 700}}}}
    monkeypatch.setattr(gemini_batch, "status", lambda name, key: answer)
    assert batch_jobs.check() == 1
    m = db.get_meeting(mid)
    assert "The budget" in m["summary"] and m["summary_model"] == "gemini-3.8-flash" and m["summary_provider"] == "gemini"
    assert [t["text"] for t in db.tasks_for(mid)] == ["Dana: send the budget"]
    old = db.history_for(mid, "summary")[0]
    assert old["old_value"] == "Old notes by a local model." and "batch job" in old["why"]
    job = db.batch_jobs()[0]
    assert job["applied"] == 1 and json.loads(job["result_json"])["applied"] == 1
    assert db.usage_since("2000-01-01")[0]["kind"] == "notes (batch)"
    assert batch_jobs.check() == 0            # nothing left to ask about


def test_notes_you_changed_while_it_was_away_are_left_alone(world, monkeypatch):
    cfg, _ = world
    mid = meeting()
    batch_jobs.submit_notes([mid], cfg)
    db.update_meeting(mid, summary="My own rewrite.")
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "succeeded", "results": {f"m{mid}": {"text": "## Summary\nNew", "error": None, "usage": {}}}})
    batch_jobs.check()
    assert db.get_meeting(mid)["summary"] == "My own rewrite."
    assert json.loads(db.batch_jobs()[0]["result_json"])["skipped"] == {"you changed its notes or transcript meanwhile": 1}


def test_an_answer_that_did_not_come_back_is_counted_and_nothing_else_is_touched(world, monkeypatch):
    cfg, _ = world
    a, b = meeting(), meeting()
    batch_jobs.submit_notes([a, b], cfg)
    ok = {"text": "## Summary\nGood\n\n## Action Items\n- None", "error": None, "usage": {}}
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "succeeded", "results": {f"m{a}": ok, f"m{b}": {"text": None, "error": "blocked", "usage": {}}}})
    batch_jobs.check()
    assert "Good" in db.get_meeting(a)["summary"] and db.get_meeting(b)["summary"] == "Old notes by a local model."
    result = json.loads(db.batch_jobs()[0]["result_json"])
    assert (result["applied"], result["failed"]) == (1, 1)


def test_a_waiting_job_is_left_open_and_a_failed_one_is_closed(world, monkeypatch):
    cfg, _ = world
    batch_jobs.submit_notes([meeting()], cfg)
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "running", "results": None})
    assert batch_jobs.check() == 1 and db.batch_jobs()[0]["state"] == "running"
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "failed", "results": None, "error": "quota"})
    batch_jobs.check()
    job = db.batch_jobs()[0]
    assert job["state"] == "failed" and job["error"] == "quota" and not db.batch_jobs(open_only=True)


def test_lockdown_and_a_missing_key_stop_a_submission(world):
    cfg, sent = world
    mid = meeting()
    with pytest.raises(batch_jobs.NotReady, match="Lockdown"):
        batch_jobs.submit_notes([mid], {**cfg, "lockdown": True})
    with pytest.raises(batch_jobs.NotReady, match="API key"):
        batch_jobs.submit_notes([mid], {**cfg, "gemini_api_key": ""})
    assert sent == {}


def test_a_meeting_too_long_for_one_request_is_skipped_not_cut_off(world, monkeypatch):
    cfg, sent = world
    mid = meeting()
    monkeypatch.setattr(llm_engine, "context_chars", lambda pid, cfg: 10)
    out = batch_jobs.submit_notes([mid], cfg)
    assert out["submitted"] == 0 and out["skipped"] == [{"id": mid, "why": "too long to write in one go"}] and sent == {}


def test_the_notes_the_job_would_replace_are_found_by_rank(world):
    import main

    cfg, _ = world
    meeting()  # written by qwen2.5:7b (rated 60)
    up = main._upgradable({**main.settings.DEFAULTS, **cfg}, "gemini-3.8-flash")
    assert up["notes"]["count"] == 1 and up["notes"]["from"] == {"qwen2.5:7b": 1}


# ── transcripts as a batch job ──

import numpy as np  # noqa: E402

import audio_store  # noqa: E402
import transcribe_gemini as tg  # noqa: E402


@pytest.fixture
def audio_world(world, monkeypatch, tmp_path):
    cfg, sent = world
    cfg.update(transcribe_engine="gemini", gemini_transcribe_model="gemini-3.5-flash-lite", gemini_mix_tracks=True)
    deleted, uploads = [], []
    monkeypatch.setattr(tg, "upload", lambda data, mime, key, name="audio": uploads.append(name) or {"name": f"files/{name}", "uri": f"https://f/{name}"})
    monkeypatch.setattr(tg, "delete_file", lambda file, key: deleted.append(file["name"]))
    monkeypatch.setattr(tg, "encode", lambda audio: b"ogg")

    def track(folder, name):
        rng = np.random.default_rng(1 if name == "mic" else 2)
        return (rng.standard_normal(16000 * 20) * 0.2).astype(np.float32)

    monkeypatch.setattr(audio_store, "load_track", track)
    monkeypatch.setattr(batch_jobs.store, "load_track", track)
    return cfg, sent, deleted, uploads


def meeting_with_audio(tmp_path):
    mid = meeting()
    (tmp_path / "audio").mkdir(exist_ok=True)
    db.update_meeting(mid, audio_dir=str(tmp_path / "audio"), has_system=1, duration_sec=20)
    return mid


def turns(*items):
    return json.dumps([{"start": s, "speaker": who, "text": t} for s, who, t in items])


def test_audio_is_uploaded_per_chunk_and_sent_as_one_job_of_file_requests(audio_world, tmp_path):
    cfg, sent, deleted, uploads = audio_world
    mid = meeting_with_audio(tmp_path)
    out = batch_jobs.submit_transcripts([mid], cfg, "gemini-3.5-flash-lite")
    assert out["submitted"] == 1 and uploads == [f"batch-m{mid}-c0"] and deleted == []
    request = sent["requests"][0]
    assert request["key"] == f"t{mid}c0"
    assert request["body"]["contents"][0]["parts"][0]["fileData"]["fileUri"] == f"https://f/batch-m{mid}-c0"
    job = db.batch_jobs()[0]
    assert job["kind"] == "transcripts" and job["est_cost"] > 0


def test_the_transcribe_model_cannot_be_batched(audio_world, tmp_path):
    cfg, sent, _, _ = audio_world
    with pytest.raises(batch_jobs.NotReady, match="can't run as a batch job"):
        batch_jobs.submit_transcripts([meeting_with_audio(tmp_path)], cfg, "gemini-3.5-transcribe")
    assert sent == {}


def test_finished_transcripts_become_versions_and_the_audio_is_deleted_at_google(audio_world, monkeypatch, tmp_path):
    cfg, _, deleted, _ = audio_world
    mid = meeting_with_audio(tmp_path)
    batch_jobs.submit_transcripts([mid], cfg, "gemini-3.5-flash-lite")
    answer = {"text": turns(("00:00", "Speaker 1", "Dana will send the budget on Friday."), ("00:05", "Speaker 2", "Thanks Dana.")),
              "error": None, "usage": {"promptTokenCount": 10000, "candidatesTokenCount": 2000}}
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "succeeded", "results": {f"t{mid}c0": answer}})
    batch_jobs.check()
    versions = db.transcript_versions(mid)
    assert [(v["source"], v["applied"]) for v in versions] == [("original", 1), ("upgrade", 0)]
    new = versions[1]
    assert new["made_with"] == "gemini-3.5-flash-lite, redone later (batch)" and "budget" in new["transcript"]
    assert "batch job" in new["note"]
    assert db.get_meeting(mid)["transcript"].startswith("[00:00] You: Dana")      # the meeting itself is untouched
    assert deleted == [f"files/batch-m{mid}-c0"]
    assert db.usage_since("2000-01-01")[0]["kind"] == "transcript (batch)"
    assert json.loads(db.batch_jobs()[0]["result_json"])["applied"] == 1


def test_with_apply_the_new_transcript_is_used_unless_you_changed_it_meanwhile(audio_world, monkeypatch, tmp_path):
    cfg, _, _, _ = audio_world
    a, b = meeting_with_audio(tmp_path), meeting_with_audio(tmp_path)
    batch_jobs.submit_transcripts([a, b], cfg, "gemini-3.5-flash-lite", apply=True)
    db.update_meeting(b, transcript="[00:00] You: I fixed this by hand.")
    ok = {"text": turns(("00:00", "Speaker 1", "A brand new transcript.")), "error": None, "usage": {}}
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "succeeded", "results": {f"t{a}c0": ok, f"t{b}c0": ok}})
    batch_jobs.check()
    assert "brand new" in db.get_meeting(a)["transcript"]
    assert db.get_meeting(b)["transcript"] == "[00:00] You: I fixed this by hand."
    assert any(v["source"] == "upgrade" for v in db.transcript_versions(b))        # but its version is kept


def test_a_chunk_that_did_not_come_back_fails_the_meeting_not_the_rest(audio_world, monkeypatch, tmp_path):
    cfg, _, _, _ = audio_world
    a, b = meeting_with_audio(tmp_path), meeting_with_audio(tmp_path)
    batch_jobs.submit_transcripts([a, b], cfg, "gemini-3.5-flash-lite")
    good = {"text": turns(("00:00", "Speaker 1", "Fine.")), "error": None, "usage": {}}
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "succeeded", "results": {
        f"t{a}c0": good, f"t{b}c0": {"text": None, "error": "blocked", "usage": {}}}})
    batch_jobs.check()
    result = json.loads(db.batch_jobs()[0]["result_json"])
    assert (result["applied"], result["failed"]) == (1, 1)


def test_audio_is_deleted_at_google_when_a_job_fails_or_is_cancelled(audio_world, monkeypatch, tmp_path):
    cfg, _, deleted, _ = audio_world
    mid = meeting_with_audio(tmp_path)
    batch_jobs.submit_transcripts([mid], cfg, "gemini-3.5-flash-lite")
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "failed", "results": None, "error": "quota"})
    batch_jobs.check()
    assert deleted == [f"files/batch-m{mid}-c0"]


def test_a_failed_submission_deletes_the_audio_it_had_uploaded(audio_world, monkeypatch, tmp_path):
    cfg, _, deleted, _ = audio_world
    mid = meeting_with_audio(tmp_path)

    def boom(*a, **k):
        raise gemini_batch.BatchError("Google is down")

    monkeypatch.setattr(gemini_batch, "create", boom)
    with pytest.raises(gemini_batch.BatchError):
        batch_jobs.submit_transcripts([mid], cfg, "gemini-3.5-flash-lite")
    assert deleted == [f"files/batch-m{mid}-c0"] and db.batch_jobs() == []


# ── regressions found in review ──

def test_two_checks_at_once_apply_a_job_only_once(world, monkeypatch):
    cfg, _ = world
    mid = meeting()
    batch_jobs.submit_notes([mid], cfg)
    batch_jobs._checking.acquire()                  # another check (the background loop) is at it
    try:
        monkeypatch.setattr(gemini_batch, "status", lambda *a: pytest.fail("a second check asked Google too"))
        assert batch_jobs.check() == 0
    finally:
        batch_jobs._checking.release()


def test_batch_results_refresh_an_exported_meeting(world, monkeypatch):
    cfg, _ = world
    mid = meeting()
    batch_jobs.submit_notes([mid], cfg)
    refreshed = []
    monkeypatch.setattr(batch_jobs.pipeline, "reexport", refreshed.append)
    monkeypatch.setattr(gemini_batch, "status", lambda n, k: {"state": "succeeded", "results": {
        f"m{mid}": {"text": "## Summary\nNew\n\n## Action Items\n- None", "error": None, "usage": {}}}})
    batch_jobs.check()
    assert refreshed == [mid]


def test_notes_imported_from_another_app_are_never_rewritten_by_a_batch(world):
    import main

    cfg, _ = world
    mine = meeting()
    imported = meeting()
    db.update_meeting(imported, summary_provider="granola", summary_model=None)
    up = main._upgradable({**main.settings.DEFAULTS, **cfg}, "gemini-3.8-flash")["notes"]
    assert up["ids"] == [mine]


def test_a_muted_microphone_does_not_turn_a_remote_speaker_into_you():
    import numpy as np

    import mixdown

    rng = np.random.default_rng(3)
    # two remote people on the call's audio, talking in bursts with pauses (so it is quieter on average than at its loudest)
    envelope = np.repeat(np.tile([1.0, 0.15, 0.4, 0.15], 3 * 12), 4000)[:16000 * 12]
    system = (rng.standard_normal(16000 * 12) * 0.2 * envelope).astype(np.float32)
    mic = (rng.standard_normal(16000 * 12) * 0.003).astype(np.float32)          # only a steady noise floor
    segs = [{"start": 0, "end": 6, "speaker": "a", "text": "remote talking"}, {"start": 6, "end": 12, "speaker": "b", "text": "also remote"}]
    assert [s["speaker"] for s in mixdown.name_you(segs, mic, system)] == ["Speaker 1", "Speaker 2"]


def test_the_original_is_recorded_once_even_when_asked_for_at_the_same_moment(world):
    import threading

    mid = meeting()
    threads = [threading.Thread(target=db.transcript_versions, args=(mid,)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [v["source"] for v in db.transcript_versions(mid)] == ["original"]


def test_the_gemini_model_cannot_be_emptied():
    import settings

    with pytest.raises(ValueError, match="can't be empty"):
        settings.update({"gemini_transcribe_model": "  "})


def test_gemini_with_captions_unavailable_never_falls_back_to_the_local_model(tmp_path, monkeypatch):
    import live

    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    for key, why in (("", "add a Google API key"), ("k", "Lockdown")):
        cfg = {**__import__("settings").DEFAULTS, "transcribe_engine": "gemini", "gemini_api_key": key, "live_final": False,
               "lockdown": key == "k"}
        s = live.LiveSession(1, tmp_path / "a", draft=True, cfg=cfg)
        try:
            assert not s.polls_drafts and why in s.captions_off_reason
        finally:
            s.close()
