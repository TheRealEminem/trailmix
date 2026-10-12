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
           "summary_template": "general", "your_name": "Mark", "vocabulary": [], "auto_title": False, "summary_provider": "ollama"}
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
