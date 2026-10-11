import importer


def test_granola_copy_transcript_format():
    text = """Me: Hey, thanks for joining.
Them: Of course. Shall we start with the launch plan?
Me: Yes. I think we ship Tuesday,
and Priya updates the docs.
Them: Sounds good."""
    segs = importer.parse_text(text)
    assert [s["speaker"] for s in segs] == ["You", "Them", "You", "Them"]
    assert segs[2]["text"] == "Yes. I think we ship Tuesday, and Priya updates the docs."
    assert all(a["start"] < b["start"] for a, b in zip(segs, segs[1:]))


def test_names_and_timestamps():
    text = "[00:05] Mark: Morning.\n[01:10] Dana: Hi Mark.\n[1:02:03] Mark: Wrapping up."
    segs = importer.parse_text(text, {"Mark"})
    assert [(s["start"], s["speaker"]) for s in segs] == [(5, "You"), (70, "Them"), (3723, "You")]


def test_plain_text_stays_unlabeled():
    segs = importer.parse_text("First paragraph of notes.\nSecond one, a bit longer than the first.")
    assert len(segs) == 2 and all(s["speaker"] is None for s in segs)


def test_captions_with_times_above_the_words():
    """YouTube and Google Drive transcripts: "0:04" on its own line, then the words; ">>" where someone new talks."""
    text = """0:04
And we have John here
0:08
>> and Maya, a potential member. She's a student
0:15
at SOU and has shown an interest.
0:19
>> Thanks. Shall we approve the minutes?
1:02:03
>> Second.
1:02:05
>> Okay: done."""
    segs = importer.parse_text(text)
    assert [s["start"] for s in segs] == [4, 8, 19, 3723, 3725]
    assert segs[1]["text"] == "and Maya, a potential member. She's a student at SOU and has shown an interest."
    assert all(s["speaker"] is None for s in segs[:4])
    assert all("0:" not in s["text"] for s in segs)  # the times aren't read as words
    assert segs[1]["end"] <= segs[2]["start"]
    assert segs[-1]["end"] < 3735  # its length comes from the times, not from counting lines


def test_long_caption_turns_are_split():
    lines = []
    for i in range(40):
        lines += [f"{i // 6}:{i * 10 % 60:02d}", (">> " if i % 20 == 0 else "") + "word " * 12]
    lines += ["9:00", ">> one", "9:05", ">> two"]
    segs = importer.parse_text("\n".join(lines))
    assert all(len(s["text"]) <= importer.TURN_MAX_CHARS + 70 for s in segs)
    assert len(segs) > 4


def test_granola_api_items_map_to_you_and_them():
    items = [
        {"speaker": {"source": "microphone", "attribution": "me"}, "text": "Hello", "start_time": "2026-01-27T15:30:00Z", "end_time": "2026-01-27T15:30:02Z"},
        {"speaker": {"source": "speaker", "attribution": "them"}, "text": "Hi there", "start_time": "2026-01-27T15:30:03Z", "end_time": "2026-01-27T15:30:05.5Z"},
        {"speaker": {"source": "speaker"}, "text": "No attribution", "start_time": "2026-01-27T15:30:06Z", "end_time": "2026-01-27T15:30:07Z"},
    ]
    segs = importer.granola_segments(items)
    assert [(s["start"], s["end"], s["speaker"]) for s in segs] == [(0, 2, "You"), (3, 5.5, "Them"), (6, 7, "Them")]


def test_ios_notes_are_unlabeled():
    items = [{"speaker": {"source": "microphone", "diarization_label": "Speaker A"}, "text": "One", "start_time": "2026-01-27T15:30:00Z", "end_time": "2026-01-27T15:30:01Z"}]
    assert importer.granola_segments(items)[0]["speaker"] is None


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


def test_granola_import_with_paged_transcript(monkeypatch, tmp_path):
    import database as db
    import pipeline

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(pipeline, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(importer, "_REQUEST_GAP_S", 0)
    item = lambda who, t, text: {"speaker": {"source": "microphone" if who == "me" else "speaker", "attribution": who},
                                 "text": text, "start_time": f"2026-01-27T15:30:{t:02d}Z", "end_time": f"2026-01-27T15:30:{t + 1:02d}Z"}
    note = {"id": "not_aaaaaaaaaaaaaa", "title": "Budget review", "created_at": "2026-01-27T15:29:00Z",
            "calendar_event": {"scheduled_start_time": "2026-01-27T15:30:00Z"}, "summary_markdown": "## Overview\nWe met.\n\n## Action Items\n- [ ] Dana: send the deck"}

    def fake(key, path, params=None):
        if path == "/notes":
            return FakeResponse(200, {"notes": [note], "hasMore": False, "cursor": None})
        if path.endswith("/transcript"):
            if not params.get("cursor"):
                return FakeResponse(200, {"transcript": [item("me", 0, "Hi all.")], "hasMore": True, "cursor": "next"})
            return FakeResponse(200, {"transcript": [item("them", 2, "Hello!")], "hasMore": False, "cursor": None})
        if params and params.get("include") == "transcript":
            return FakeResponse(413, {"error": "TRANSCRIPT_TOO_LARGE"})
        return FakeResponse(200, note)

    monkeypatch.setattr(importer, "_granola", fake)
    assert importer.granola_notes("grn_x")[0]["imported"] is False
    meeting_id = importer.import_granola_note("grn_x", note["id"], keep_summary=True)
    m = db.get_meeting(meeting_id)
    assert m["title"] == "Budget review" and m["status"] == "done" and m["source"] == "granola"
    assert m["created_at"].startswith("2026-01-27T15:30:00")
    assert "Them: Hello!" in m["transcript"] and db.tasks_for(meeting_id)[0]["text"].startswith("Dana")
    assert importer.import_granola_note("grn_x", note["id"], keep_summary=True) is None  # not twice
    assert importer.granola_notes("grn_x")[0]["imported"] is True
