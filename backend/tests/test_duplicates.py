import json
import random
from datetime import datetime, UTC

import pytest
from fastapi.testclient import TestClient

import database as db
import duplicates
import importer
import main
import pipeline
import settings

VOCAB = ["pilot", "customer", "cold", "chain", "sensor", "vaccine", "budget", "launch", "vendor", "contract", "onboarding", "clinic", "freezer", "battery", "gateway", "dashboard", "alert", "investor", "grant", "hiring", "roadmap", "demo", "partner", "county", "pharmacy", "shipment", "temperature", "warranty", "pricing", "margin", "supplier", "firmware", "cloud", "latency", "report", "county", "board", "meeting", "rural", "hospital", "outreach", "survey", "interview", "feedback", "feature", "backlog", "sprint", "design", "prototype", "enclosure"]


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(pipeline, "enqueue", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "reexport", lambda *a, **kw: None)
    duplicates._prints.clear()
    return tmp_path


def talk(seed: int, lines: int = 60) -> list[list[str]]:
    rng = random.Random(seed)
    return [[rng.choice(VOCAB) for _ in range(18)] for _ in range(lines)]


def heard(said: list[list[str]], seed: int, start: float = 0.0, garble: float = 0.1) -> list[dict]:
    """The same talk as another transcriber writes it: a few words different, the clock starting elsewhere."""
    rng = random.Random(seed)
    segs = []
    for i, words in enumerate(said):
        text = " ".join(w if rng.random() > garble else rng.choice(VOCAB) for w in words)
        segs.append({"start": start + i * 6, "end": start + i * 6 + 5.5, "speaker": "Them" if i % 2 else "You", "text": text})
    return segs


def recording(segs: list[dict], summary: str | None = "## Overview\nTrailmix notes\n\n## Action Items\n- Old task") -> int:
    mid = db.create_meeting("Meeting Oct 03", title_auto=True)
    db.update_meeting(mid, transcript="\n".join(pipeline._line(s) for s in segs), segments_json=segs, transcribed=1,
                      status="done", duration_sec=segs[-1]["end"], summary=summary, summary_provider="ollama",
                      summary_model="qwen2.5:7b", bookmarks_json=[{"t": 30, "note": "flag"}])
    return mid


def granola(segs: list[dict], note_id="g1", summary="## Overview\nGranola notes\n\n## Action Items\n- Send the deck") -> int:
    return importer.save_imported("Cinder Labs check-in", datetime.now(UTC), segs, "granola", note_id, summary)


def test_the_same_talk_scores_high_and_another_meeting_low():
    a = duplicates.phrases(" ".join(s["text"] for s in heard(talk(1), 2)))
    b = duplicates.phrases(" ".join(s["text"] for s in heard(talk(1), 3)))
    c = duplicates.phrases(" ".join(s["text"] for s in heard(talk(9), 4)))
    assert duplicates.score(a, b) > 0.4
    assert duplicates.score(a, c) < 0.05


def test_granola_copy_merges_into_the_recording_and_lines_up_with_it(fresh_db):
    said = talk(1)
    rec = recording(heard(said, 2, start=30))      # the recording starts 30 s before the talk Granola captured
    kept = granola(heard(said, 3, start=0))
    assert kept == rec
    m = db.get_meeting(rec)
    assert len(db.list_meetings()) == 1
    assert "Granola notes" in m["summary"] and m["transcribed_with"] == "Granola" and m["title"] == "Cinder Labs check-in"
    assert abs(json.loads(m["segments_json"])[0]["start"] - 30) < 3        # moved onto the recording's clock
    assert json.loads(m["bookmarks_json"])[0]["note"] == "flag"             # your flags stay
    assert [t["text"] for t in db.tasks_for(rec)] == ["Send the deck"]
    versions = db.transcript_versions(rec)
    assert len(versions) == 2 and versions[-1]["applied"] and not versions[0]["applied"]
    assert db.imported_ids("granola") == {"g1": rec}                        # not imported again


def test_a_pasted_copy_without_notes_keeps_trailmix_notes(fresh_db):
    said = talk(1)
    rec = recording(heard(said, 2))
    kept = importer.save_imported("", datetime.now(UTC), heard(said, 3), "paste", None, None)
    m = db.get_meeting(kept)
    assert kept == rec and "Trailmix notes" in m["summary"] and m["transcribed_with"] == "a pasted transcript"


def test_a_different_meeting_is_left_alone(fresh_db):
    rec = recording(heard(talk(1), 2))
    other = granola(heard(talk(7), 3))
    assert other != rec and len(db.list_meetings()) == 2


def test_undo_brings_the_copy_back_and_it_isnt_merged_again(fresh_db):
    said = talk(1)
    rec = recording(heard(said, 2))
    granola(heard(said, 3))
    back = duplicates.unmerge(rec)
    m, g = db.get_meeting(rec), db.get_meeting(back)
    assert "Trailmix notes" in m["summary"] and m["title"] == "Meeting Oct 03" and m["merged_json"] is None
    assert "Granola notes" in g["summary"] and g["source"] == "granola" and g["external_id"] == "g1"
    assert [t["text"] for t in db.tasks_for(rec)] == ["Old task"]
    assert duplicates.check(back, settings.get_all()) == back and len(db.list_meetings()) == 2


def test_ask_mode_flags_instead_of_merging(fresh_db):
    settings.update({"duplicates": "ask"})
    said = talk(1)
    rec = recording(heard(said, 2))
    g = granola(heard(said, 3))
    assert g != rec and db.get_meeting(rec)["duplicate_of"] == g
    r = TestClient(main.app).get(f"/api/meetings/{rec}").json()
    assert r["duplicate_of"]["from"] == "Granola"


def test_find_and_merge_the_library(fresh_db):
    settings.update({"duplicates": "keep"})
    said = talk(1)
    rec = recording(heard(said, 2))
    granola(heard(said, 3))
    client = TestClient(main.app)
    pairs = client.get("/api/duplicates").json()["pairs"]
    assert len(pairs) == 1 and pairs[0]["keep"]["id"] == rec and pairs[0]["other"]["label"] == "Granola"
    assert client.post("/api/duplicates/merge", json={"all": True}).json()["merged"] == 1
    assert client.get(f"/api/meetings/{rec}").json()["merged"][0]["from"] == "Granola"


def test_a_new_recording_takes_granola_notes_instead_of_writing_its_own(fresh_db, monkeypatch):
    said = talk(1)
    granola(heard(said, 3))
    rec = db.create_meeting("Meeting", title_auto=True)
    segs = heard(said, 2, start=12)
    monkeypatch.setattr(pipeline, "_finish_live", lambda m, cfg: (segs, True, segs[-1]["end"]))
    monkeypatch.setattr(pipeline.store, "compress_pending", lambda m: None)
    monkeypatch.setattr(pipeline.llm_engine, "summarize", lambda *a, **kw: pytest.fail("wrote notes anyway"))
    monkeypatch.setattr(pipeline.telemetry, "send", lambda *a, **kw: None)
    db.update_meeting(rec, live_json="[]", status="queued")
    pipeline._run_stages(rec, frozenset(), frozenset())
    m = db.get_meeting(rec)
    assert "Granola notes" in m["summary"] and m["status"] == "done" and len(db.list_meetings()) == 1
