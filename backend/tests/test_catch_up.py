from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import catch_up
import database as db
import live
import live_notes
import llm_engine
import main
import pricing
import settings

GB = 1024**3
SR = live.SR
TAGS = [
    {"name": "qwen2.5:7b", "size": int(4.7 * GB), "details": {"parameter_size": "7.6B"}},
    {"name": "qwen2.5:3b", "size": int(1.9 * GB), "details": {"parameter_size": "3.1B"}},
    {"name": "nomic-embed-text", "size": int(0.3 * GB), "details": {"parameter_size": "137M"}},
]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A meeting 12 minutes into recording, with a live transcript, on a 16 GB Mac; every AI call is recorded."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(live, "warm_up", lambda cfg, now=False: False)
    monkeypatch.setattr(llm_engine, "_ollama_tags", lambda cfg: TAGS)
    monkeypatch.setattr(catch_up.psutil, "virtual_memory", lambda: SimpleNamespace(total=16 * GB))
    monkeypatch.setattr(catch_up.resources, "memory_pressure", lambda: 1)
    calls = []
    reported = {}  # tokens the fake Gemini reports, when a test sets them

    def generate(pid, cfg, prompt, system=None, keep_alive=None, usage=None, **kw):
        calls.append({"pid": pid, "model": llm_engine.model_for(pid, cfg), "prompt": prompt, "keep_alive": keep_alive})
        if usage is not None:
            usage.update(reported)
        return "Dana wants the launch moved to March [11:40]."

    monkeypatch.setattr(llm_engine, "generate", generate)
    cfg = {**settings.DEFAULTS, "transcribe_engine": "remote", "live_final": False, "summary_provider": "gemini",
           "summary_fallback": "ollama", "gemini_api_key": "k", "gemini_model": "gemini-flash-latest",
           "ollama_url": "http://localhost:11434", "ollama_model": "qwen2.5:7b"}
    state = SimpleNamespace(cfg=cfg, calls=calls, reported=reported)
    monkeypatch.setattr(main.settings, "get_all", lambda: state.cfg)
    mid = db.create_meeting("Launch sync", title_auto=False)
    session = live.LiveSession(mid, tmp_path / "rec", draft=True, cfg=cfg)
    session._written = {0: 720 * SR, 1: 720 * SR}
    session.drafts = [{"start": t, "speaker": "Them" if i % 2 else "You", "text": f"line {i} at {t} seconds"}
                      for i, t in enumerate(range(0, 720, 20))]
    session.interim = {1: {"start": 715.0, "speaker": "Them", "text": "so the real question is"}}
    state.session, state.mid = session, mid
    live_notes._state.clear()
    yield state
    session.close()


def test_a_cloud_notes_ai_answers_with_the_whole_meeting_and_a_price(world):
    plan = catch_up.plan(world.session, db.get_meeting(world.mid), world.cfg)
    assert plan["ready"] and plan["provider"] == "gemini" and not plan["local"]
    assert 0 < plan["cost"] < 0.01 and plan["warning"] is None
    entry = catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "missed")
    call = world.calls[-1]
    assert call["pid"] == "gemini"
    assert "[07:00] to now ([12:00])" in call["prompt"]
    assert "line 0 at 0 seconds" in call["prompt"]                      # from the top
    assert "so the real question is (still talking)" in call["prompt"]  # the words being said right now
    assert entry["q"] == "What did I miss?" and "[11:40]" in entry["a"] and entry["live"] and entry["t"] == 720


def test_cloud_comes_first_even_when_a_local_model_is_the_notes_ai(world):
    world.cfg = {**world.cfg, "summary_provider": "ollama", "summary_fallback": "gemini"}
    assert catch_up.providers(world.cfg) == ["gemini", "ollama"]


def test_on_a_16_gb_mac_a_small_local_model_answers(world):
    world.cfg = {**world.cfg, "summary_provider": "ollama", "summary_fallback": "none"}
    plan = catch_up.plan(world.session, db.get_meeting(world.mid), world.cfg)
    assert plan["local"] and plan["cost"] == 0 and plan["model"] == "qwen2.5:3b" and "Basic and private" in plan["note"]
    catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "explain")
    call = world.calls[-1]
    assert call["model"] == "qwen2.5:3b" and call["keep_alive"] == catch_up.KEEP_ALIVE
    assert "Explain the last thing" in call["prompt"]


def test_a_small_model_is_for_basic_questions_only(world, monkeypatch):
    monkeypatch.setattr(catch_up.psutil, "virtual_memory", lambda: SimpleNamespace(total=64 * GB))
    world.cfg = {**world.cfg, "summary_provider": "ollama", "summary_fallback": "none", "ollama_model": "qwen2.5:3b"}
    plan = catch_up.plan(world.session, db.get_meeting(world.mid), world.cfg)
    assert list(plan["off"]) == ["decided"] and "Basic and private" in plan["note"]  # your own small model, too
    with pytest.raises(catch_up.NotReady, match="cloud AI or a bigger model"):
        catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "decided")
    assert world.calls == []
    catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "missed")
    catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "question", "Who owns the migration?")
    assert len(world.calls) == 2
    world.cfg = {**world.cfg, "summary_provider": "gemini", "summary_fallback": "ollama"}  # a cloud AI answers it all
    assert catch_up.plan(world.session, db.get_meeting(world.mid), world.cfg)["off"] == {}


def test_without_a_small_model_you_are_warned_about_memory(world, monkeypatch):
    monkeypatch.setattr(llm_engine, "_ollama_tags", lambda cfg: TAGS[:1])
    world.cfg = {**world.cfg, "summary_provider": "ollama", "summary_fallback": "none"}
    plan = catch_up.plan(world.session, db.get_meeting(world.mid), world.cfg)
    assert plan["model"] == "qwen2.5:7b" and "16 GB" in plan["warning"] and plan["note"] is None and plan["off"] == {}


def test_a_big_mac_keeps_your_model(world, monkeypatch):
    monkeypatch.setattr(catch_up.psutil, "virtual_memory", lambda: SimpleNamespace(total=64 * GB))
    world.cfg = {**world.cfg, "summary_provider": "ollama", "summary_fallback": "none"}
    plan = catch_up.plan(world.session, db.get_meeting(world.mid), world.cfg)
    assert plan["model"] == "qwen2.5:7b" and plan["warning"] is None and plan["note"] is None


def test_a_local_model_reads_only_the_last_20_minutes(world):
    world.session._written = {0: 3600 * SR, 1: 3600 * SR}
    world.session.drafts = [{"start": t, "speaker": "Them", "text": f"said at {t}"} for t in range(0, 3600, 60)]
    world.cfg = {**world.cfg, "summary_provider": "ollama", "summary_fallback": "none"}
    catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "missed")
    prompt = world.calls[-1]["prompt"]
    assert "said at 2400" in prompt and "said at 2340" not in prompt and "the start is left out" in prompt


def test_lockdown_keeps_it_on_this_computer(world):
    world.cfg = {**world.cfg, "lockdown": True}  # Gemini first, Ollama on this Mac as the fallback
    assert catch_up.providers(world.cfg) == ["ollama"]
    catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "question", "Who is Dana?")
    assert [c["pid"] for c in world.calls] == ["ollama"]
    # Neither of your AIs runs here: Ollama on this Mac still may; with Ollama elsewhere, nothing does.
    world.cfg = {**world.cfg, "summary_fallback": "none"}
    assert catch_up.providers(world.cfg) == ["ollama"]
    world.cfg = {**world.cfg, "ollama_url": "http://studio.local:11434"}
    plan = catch_up.plan(world.session, db.get_meeting(world.mid), world.cfg)
    assert not plan["ready"] and "Lockdown" in plan["reason"]
    with pytest.raises(catch_up.NotReady, match="Lockdown"):
        catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "missed")


def test_notes_so_far_and_your_notes_are_read_too(world):
    live_notes._state[world.mid] = {"notes": [{"start": 0, "end": 600, "text": "- Budget agreed at $40k"}],
                                    "status": None, "busy": False, "retry_at": 0}
    db.update_meeting(world.mid, my_notes_json=[{"t": 65.0, "text": "ask about hiring"}])
    catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "decided")
    prompt = world.calls[-1]["prompt"]
    assert "NOTES SO FAR:\n00:00 to 10:00:\n- Budget agreed at $40k" in prompt and "[01:05] ask about hiring" in prompt


def test_a_long_transcript_keeps_its_latest_part():
    session = SimpleNamespace(duration=100.0, has_system=False, meeting_id=0, interim={},
                              drafts=[{"start": t, "speaker": None, "text": "x" * 90 + str(t)} for t in range(100)])
    text = catch_up.material(session, {"title": "T"}, settings.DEFAULTS, 2000)
    assert "x99" in text and "x0\n" not in text and "the start is left out" in text and len(text) <= 2000


def test_nothing_to_read_yet(world):
    world.session.drafts, world.session.interim = [], {}
    with pytest.raises(catch_up.NotReady, match="Nothing has been transcribed"):
        catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "missed")
    world.session.draft = False
    assert "doesn't have one" in catch_up.plan(world.session, db.get_meeting(world.mid), world.cfg)["reason"]


def test_the_api_answers_keeps_the_answers_and_refuses_a_finished_meeting(world):
    client = TestClient(main.app)
    plan = client.get(f"/api/meetings/{world.mid}/catch-up").json()
    assert plan["ready"] and plan["history"] == [] and set(plan["kinds"]) == {"missed", "explain", "decided"}
    r = client.post(f"/api/meetings/{world.mid}/catch-up", json={"kind": "question", "question": "When is launch?"})
    assert r.status_code == 200 and r.json()["q"] == "When is launch?"
    assert client.post(f"/api/meetings/{world.mid}/catch-up", json={"kind": "question"}).status_code == 409
    assert [e["q"] for e in client.get(f"/api/meetings/{world.mid}/catch-up").json()["history"]] == ["When is launch?"]
    assert client.get(f"/api/meetings/{world.mid}").json()["qa"][0]["live"]  # kept with the meeting's questions
    used = db.usage_since("2000-01-01")
    assert used[0]["kind"] == "catch up" and used[0]["cost"] > 0
    assert client.get("/api/meetings/999999/catch-up").status_code == 409


def test_the_spend_tally_has_what_gemini_reported(world):
    world.reported.update({"in": 2000, "out": 300})
    catch_up.ask(world.session, db.get_meeting(world.mid), world.cfg, "missed")
    [row] = db.usage_since("2000-01-01")
    assert row["cost"] == pytest.approx(pricing.text_cost("gemini-flash-latest", 2000, 300))
    assert row["cost"] == pytest.approx((2000 * 0.75 + 300 * 3.75) / 1e6, abs=1e-5)  # Flash: $0.75 in, $3.75 out


def test_gemini_reports_its_tokens(monkeypatch):
    cfg = {**settings.DEFAULTS, "gemini_api_key": "k", "gemini_model": "gemini-flash-latest"}
    reply = {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}],
             "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 80, "thoughtsTokenCount": 400}}
    monkeypatch.setattr(llm_engine, "_post", lambda url, **kw: SimpleNamespace(json=lambda: reply))
    usage = {}
    assert llm_engine.generate("gemini", cfg, "q", usage=usage) == "Hi"
    assert usage == {"in": 1200, "out": 480}  # thinking is billed as output
    events = [{"candidates": [{"content": {"parts": [{"text": "H"}]}}], "usageMetadata": {"promptTokenCount": 1200}},
              {"candidates": [{"content": {"parts": [{"text": "i"}]}}], **{k: v for k, v in reply.items() if k != "candidates"}}]
    monkeypatch.setattr(llm_engine, "_sse", lambda url, **kw: iter(events))
    streamed = {}
    assert llm_engine.generate("gemini", cfg, "q", on_text=lambda t: None, usage=streamed) == "Hi"
    assert streamed == {"in": 1200, "out": 480}  # the last event's totals, counted once
