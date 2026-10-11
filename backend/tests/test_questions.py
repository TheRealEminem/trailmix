import json

import pytest
from fastapi.testclient import TestClient

import database as db
import llm_engine
import questions
import settings

CFG = {"ask_questions": True, "ollama_model": "qwen2.5:7b"}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    settings.update({"your_name": "Mark"})
    return tmp_path


@pytest.fixture
def ai(monkeypatch):
    state = {"people": "None", "terms": "None"}
    monkeypatch.setattr(llm_engine, "chain", lambda choice, cfg: ["ollama"])

    def generate(pid, cfg, prompt, system=None, keep_alive=None, exact=False):
        return state["people"] if "Who took part" in prompt else state["terms"] if "transcription mistakes" in prompt else "None"

    monkeypatch.setattr(llm_engine, "generate", generate)
    return state


def meeting(summary: str, transcript: str = "", has_system: bool = True) -> int:
    mid = db.create_meeting("Weekly sync", title_auto=False)
    segs = [{"start": 0, "end": 2, "speaker": "Them", "text": line} for line in transcript.splitlines() if line]
    db.update_meeting(mid, summary=summary, transcript=transcript, segments_json=segs, has_system=int(has_system),
                      status="done")
    db.replace_tasks(mid, ["Jef: send the Cooper Netties quote"])
    return mid


def by_kind(mid: int) -> dict:
    return {q["kind"]: q for q in questions.open_for(mid)}


def test_asks_who_was_on_the_call_and_remembers_the_answer(fresh_db, ai):
    ai["people"] = "Mark, Priya Shah, Dana"
    mid = meeting("## Overview\nMark and Priya Shah planned the pilot with Dana.")
    questions.ask_about(mid, CFG)
    q = by_kind(mid)["them"]
    assert [o["value"] for o in q["options"]] == ["Priya Shah", "Dana", "several"]  # not you
    questions.answer(q["id"], "Priya Shah")
    assert json.loads(db.get_meeting(mid)["speaker_names_json"])["Them"] == "Priya Shah"
    assert "Priya Shah" in questions.vocabulary()
    assert not questions.open_for(mid)


def test_a_name_one_letter_off_a_known_one_is_fixed_everywhere(fresh_db, ai):
    questions.learn("Jeff")
    mid = meeting("## Overview\nJef will send the quote.", "Jef: I'll send the quote.", has_system=False)
    questions.ask_about(mid, CFG)
    q = by_kind(mid)["spelling"]
    assert q["prompt"] == "Is “Jef” the same as “Jeff”?"
    questions.answer(q["id"], "yes")
    m = db.get_meeting(mid)
    assert "Jef " not in m["summary"] and "Jeff will send" in m["summary"]
    assert json.loads(m["segments_json"])[0]["text"] == "Jeff: I'll send the quote."
    assert db.tasks_for(mid)[0]["text"] == "Jeff: send the Cooper Netties quote"


def test_a_corrected_name_is_corrected_in_the_other_questions_too(fresh_db, ai):
    questions.learn("Stacey Hoshimiya")
    ai["people"] = "Stacy Hoshimia"
    mid = meeting("## Overview\nStacy Hoshimia reviewed the pitch.")
    questions.ask_about(mid, CFG)
    questions.answer(by_kind(mid)["spelling"]["id"], "yes")
    assert [o["label"] for o in by_kind(mid)["them"]["options"]][0] == "Stacey Hoshimiya"


def test_misheard_words_are_checked_against_the_meeting_and_never_asked_twice(fresh_db, ai):
    ai["terms"] = "Cooper Netties → Kubernetes\nWidgetron → Widgetronic"  # the second isn't in the meeting
    mid = meeting("## Overview\nWe moved the Cooper Netties cluster.", has_system=False)
    questions.ask_about(mid, CFG)
    q = by_kind(mid)["term"]
    assert q["prompt"] == "Where it says “Cooper Netties”, did they say “Kubernetes”?"
    assert len(questions.open_for(mid)) == 1
    questions.dismiss(q["id"])
    questions.ask_about(mid, CFG)
    assert not questions.open_for(mid)  # dismissed stays dismissed


def test_typing_the_right_word_fixes_it_and_no_teaches_the_spelling(fresh_db, ai):
    ai["terms"] = "Cooper Netties → Kubernetes"
    mid = meeting("## Overview\nThe Cooper Netties cluster.", has_system=False)
    questions.ask_about(mid, CFG)
    questions.answer(by_kind(mid)["term"]["id"], "K8s")
    assert "K8s cluster" in db.get_meeting(mid)["summary"] and "K8s" in questions.vocabulary()


def test_a_partial_workspace_fit_becomes_a_question(fresh_db, ai, monkeypatch):
    import workspaces

    cepac = db.create_workspace("CEPAC", about="city climate commission")
    db.create_workspace("Personal")
    mid = meeting("## Overview\nThe energy work group met.", has_system=False)
    ai["terms"] = "Workspace: CEPAC\nFit: partial"  # the sorter's answer
    monkeypatch.setattr(llm_engine, "model_for", lambda pid, cfg: "qwen2.5:7b")
    monkeypatch.setattr(llm_engine, "generate", lambda *a, **k: "Workspace: CEPAC\nFit: partial")
    workspaces.after_notes(mid, {**CFG, "auto_workspace": True})
    assert db.get_meeting(mid)["workspace_id"] is None  # not moved on a partial fit
    q = by_kind(mid)["workspace"]
    assert q["prompt"] == "Does this meeting belong in CEPAC?"
    questions.answer(q["id"], str(cepac))
    assert db.get_meeting(mid)["workspace_id"] == cepac and db.get_meeting(mid)["workspace_auto"] == 0


def test_answering_over_the_api(fresh_db, ai):
    import main

    ai["people"] = "Dana"
    mid = meeting("## Overview\nDana joined.")
    questions.ask_about(mid, CFG)
    client = TestClient(main.app)
    got = client.get(f"/api/meetings/{mid}/questions").json()
    assert client.get("/api/questions").json() == {str(mid): 1}
    assert client.post(f"/api/questions/{got[0]['id']}", json={"value": "Dana"}).status_code == 200
    assert client.get(f"/api/meetings/{mid}/questions").json() == []


def test_spellings_are_seeded_from_your_name_workspaces_and_people(fresh_db, ai):
    db.create_workspace("Cold Connect", about="Sales for ColdCap, our B2G cold-chain product, and the CEPAC board.")
    mid = meeting("## Overview\nWe met.")
    db.update_meeting(mid, speaker_names_json={"Them": "Priya Nair"})
    assert questions.seeded() == ["Mark", "Cold Connect", "ColdCap", "B2G", "CEPAC", "Priya Nair"]
    assert "ColdCap" in questions.vocabulary_prompt() and "Mark" in questions.vocabulary_prompt()


def test_a_known_spelling_is_never_questioned(fresh_db, ai):
    db.create_workspace("Cold Connect", about="Sales for ColdCap.")
    ai["terms"] = "ColdCap → CoolCap"
    mid = meeting("## Overview\nThe ColdCap pitch went well.", "The ColdCap pitch went well.", has_system=False)
    questions.ask_about(mid, CFG)
    assert "term" not in by_kind(mid)
