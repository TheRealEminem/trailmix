import threading
import time

import pipeline


class FakeDB:
    def __init__(self):
        self.updates = []

    def update_meeting(self, meeting_id, **fields):
        self.updates.append(fields)


def test_gate_waits_for_memory_then_continues(monkeypatch):
    free = {"ok": False}
    fake = FakeDB()
    monkeypatch.setattr(pipeline, "db", fake)
    monkeypatch.setattr(pipeline.resources, "check", lambda gb, what: None if free["ok"] else "Waiting for memory")
    monkeypatch.setattr(pipeline.live, "active_count", lambda: 0)
    monkeypatch.setattr(pipeline, "RAM_POLL_S", 1)
    done = threading.Event()
    threading.Thread(target=lambda: (pipeline._gate(7, False, 3.0, "The model"), done.set())).start()
    time.sleep(0.3)
    assert not done.is_set() and fake.updates[0]["status"] == "waiting_confirm"
    free["ok"] = True
    assert done.wait(3)


def test_proceed_anyway_cuts_the_wait_short(monkeypatch):
    monkeypatch.setattr(pipeline, "db", FakeDB())
    monkeypatch.setattr(pipeline.resources, "check", lambda gb, what: "Waiting for memory")
    monkeypatch.setattr(pipeline.live, "active_count", lambda: 0)
    done = threading.Event()
    threading.Thread(target=lambda: (pipeline._gate(8, False, 3.0, "The model"), done.set())).start()
    time.sleep(0.3)
    assert pipeline.proceed_now(8)
    assert done.wait(3)
    assert not pipeline.proceed_now(8)  # nothing is waiting any more


def seg(start, speaker, text="hello there everyone"):
    return {"start": start, "end": start + 2, "speaker": speaker, "text": text}


def test_label_keeps_gemini_speakers_on_the_other_side_of_a_call():
    per_track = {"mic": [seg(0, "Speaker 1", "this is my own line here")],
                 "system": [seg(5, "Speaker 1", "welcome to the planning call"), seg(9, "Speaker 2", "thanks for having me today")]}
    out = pipeline._label(per_track, ["mic", "system"], diarized=True)
    assert [s["speaker"] for s in out] == ["You", "Speaker 1", "Speaker 2"]


def test_label_one_voice_on_the_other_side_is_them():
    per_track = {"mic": [seg(0, "Speaker 1", "this is my own line here")],
                 "system": [seg(5, "Speaker 1", "welcome to the planning call")]}
    out = pipeline._label(per_track, ["mic", "system"], diarized=True)
    assert [s["speaker"] for s in out] == ["You", "Them"]


def test_label_single_track_keeps_speakers_unless_there_is_only_one():
    two = pipeline._label({"mic": [seg(0, "Speaker 1", "first person talking now"), seg(4, "Speaker 2", "second person answering")]},
                          ["mic"], diarized=True)
    assert [s["speaker"] for s in two] == ["Speaker 1", "Speaker 2"]
    one = pipeline._label({"mic": [seg(0, "Speaker 1", "just me dictating a note")]}, ["mic"], diarized=True)
    assert one[0]["speaker"] is None


def test_speaker_names_and_note_cover_diarized_speakers():
    import json

    import meeting_text

    m = {"segments_json": json.dumps([seg(0, "You"), seg(3, "Speaker 1"), seg(6, "Speaker 2")]),
         "speaker_names_json": json.dumps({"Speaker 1": "Dana"})}
    assert meeting_text.speaker_labels(m) == ["You", "Speaker 1", "Speaker 2"]
    names = meeting_text.speaker_names(m, {"your_name": "Dale"})
    assert names["Speaker 1"] == "Dana" and names["Speaker 2"] == "Speaker 2" and names["You"] == "Dale"
    note = meeting_text.speakers_note(m, {"your_name": "Dale"})
    assert '"Dale" is the person who recorded' in note and '"Dana"' in note and '"Speaker 2"' in note
