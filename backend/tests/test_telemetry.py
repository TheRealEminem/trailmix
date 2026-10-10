import json

import pytest

import telemetry

CFG = {"share_stats": True, "stats_notice_seen": True, "install_id": "abc123", "stats_last_daily": "",
       "transcribe_engine": "local", "live_final": True, "live_notes": "auto", "summary_provider": "ollama",
       "summary_fallback": "none", "auto_transcribe": True, "auto_summarize": True, "auto_workspace": True,
       "ask_questions": True, "your_name": "Mark Morrison", "ollama_url": "http://x", "ollama_model": "qwen2.5:7b"}


@pytest.fixture
def posthog(monkeypatch):
    posted = []
    monkeypatch.setattr(telemetry, "KEY", "phc_test")
    monkeypatch.setattr(telemetry, "_post", posted.append)
    monkeypatch.setattr(telemetry._pool, "submit", lambda fn, body: fn(body))
    monkeypatch.setattr(telemetry, "_sent", telemetry.deque(maxlen=12))
    return posted


def test_nothing_is_sent_before_the_choice_was_seen(posthog):
    assert not telemetry.send("app_active", {}, {**CFG, "stats_notice_seen": False}) and not posthog


def test_nothing_is_sent_when_turned_off(posthog):
    assert not telemetry.send("app_active", {}, {**CFG, "share_stats": False}) and not posthog


def test_nothing_is_sent_from_a_build_without_a_key(monkeypatch):
    monkeypatch.setattr(telemetry, "KEY", "")
    assert not telemetry.send("app_active", {}, CFG)


def test_events_are_anonymous_to_posthog(posthog):
    telemetry.send("meeting_processed", {"minutes": 30}, CFG)
    body = posthog[0]
    assert body["api_key"] == "phc_test" and body["distinct_id"] == "abc123" and body["event"] == "meeting_processed"
    props = body["properties"]
    assert props["$process_person_profile"] is False and props["$geoip_disable"] is True
    assert props["minutes"] == 30 and props["app_version"] and props["os"]
    assert telemetry.recent()[0]["properties"] == props  # Settings shows exactly what went out


def test_the_daily_summary_holds_nothing_personal(posthog, monkeypatch):
    monkeypatch.setattr(telemetry.settings, "get_all", lambda: CFG)
    monkeypatch.setattr(telemetry.settings, "update", lambda c: {**CFG, **c})
    monkeypatch.setattr(telemetry, "usage", lambda: {"meetings": "51-200", "meetings_last_7_days": "6-20", "workspaces": "1-5"})
    monkeypatch.setattr(telemetry.llm_engine, "model_for", lambda pid, cfg: "qwen2.5:7b")
    telemetry.daily()
    props = posthog[0]["properties"]
    text = json.dumps(props)
    assert posthog[0]["event"] == "app_active" and props["notes_model"] == "qwen2.5:7b" and props["ram_gb"] > 0
    assert "Mark" not in text and "/Users" not in text and "http://x" not in text
    assert set(props) >= {"chip", "ram_gb", "disk_gb", "notes_provider", "live_notes", "meetings"}


def test_the_daily_summary_goes_once_a_day(posthog, monkeypatch):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    monkeypatch.setattr(telemetry.settings, "get_all", lambda: {**CFG, "stats_last_daily": today})
    telemetry.daily()
    assert not posthog


def test_turning_stats_off_forgets_the_install_id(tmp_path, monkeypatch):
    import database as db
    import settings
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "trailmix.db")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "audio")
    db.init_db()
    settings.update({"install_id": "abc123"})
    settings.update({"share_stats": False})
    assert settings.get_all()["install_id"] == ""


@pytest.mark.parametrize("n,label", [(0, "0"), (3, "1-5"), (6, "6-20"), (21, "21-50"), (120, "51-200"), (900, "200+")])
def test_counts_are_ranges(n, label):
    assert telemetry._bucket(n) == label


def test_going_back_is_noted(posthog, monkeypatch):
    monkeypatch.setattr(telemetry.settings, "get_all", lambda: CFG)
    telemetry.version_changed("0.13.0", "0.12.0")
    telemetry.version_changed("0.12.0", "0.12.0")  # same version: nothing
    telemetry.version_changed("", "0.12.0")  # a fresh install: nothing
    assert len(posthog) == 1 and posthog[0]["properties"]["went_back"] is True
