from datetime import datetime, timedelta, timezone

import pytest

import database as db
import fathom
import fireflies
import importer
import pipeline


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(pipeline, "enqueue", lambda *a, **kw: None)
    monkeypatch.setattr(fathom, "_HEAVY_GAP_S", 0)


class Response:
    def __init__(self, status, data, headers=None):
        self.status_code, self._data, self.headers, self.text = status, data, headers or {}, str(data)

    def json(self):
        return self._data


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Fathom ──────────────────────────────────────────────────────────────

ME = {"name": "Dale Morgan", "email": "dale@example.com"}


def fathom_meeting(rid: int, start: datetime) -> dict:
    return {"recording_id": rid, "title": "Impromptu Zoom Meeting", "meeting_title": "Pricing review",
            "created_at": iso(start), "recording_start_time": iso(start),
            "recording_end_time": iso(start + timedelta(minutes=30)), "recorded_by": ME,
            "action_items": [{"description": "Dana to send the price sheet"}]}


def fake_fathom(monkeypatch, meetings, summary="## Key Takeaways\n- Prices go up in May", calls=None):
    calls = calls if calls is not None else []

    def get(url, params=None, headers=None, timeout=None):
        path = url.removeprefix(fathom.API)
        calls.append((path, params))
        assert headers == {"X-Api-Key": "fk"}
        if path == "/meetings":
            cursor = dict(params).get("cursor")
            page = meetings[1:] if cursor else meetings[:1]
            return Response(200, {"items": page, "next_cursor": None if cursor or len(meetings) < 2 else "p2"})
        if path.endswith("/transcript"):
            return Response(200, {"transcript": [
                {"speaker": {"display_name": "Dale Morgan", "matched_calendar_invitee_email": "dale@example.com"},
                 "text": "Shall we raise prices?", "timestamp": "00:00:05"},
                {"speaker": {"display_name": "Dana", "matched_calendar_invitee_email": None},
                 "text": "In May, yes.", "timestamp": "00:00:09"}]})
        if path.endswith("/summary"):
            return Response(200, {"summary": {"template_name": "general", "markdown_formatted": summary}})
        raise AssertionError(path)

    monkeypatch.setattr(fathom.httpx, "get", get)
    return calls


def test_fathom_meetings_come_in_with_you_them_and_tasks(fresh_db, monkeypatch):
    start = datetime(2026, 10, 1, 15, tzinfo=timezone.utc)
    calls = fake_fathom(monkeypatch, [fathom_meeting(11, start), fathom_meeting(12, start - timedelta(days=1))])
    notes = fathom.notes("fk", start - timedelta(days=7))
    assert [n["id"] for n in notes] == ["11", "12"]  # both pages
    assert ("created_after", "2026-09-24T15:00:00.000Z") in calls[0][1]
    meeting_id = fathom.import_note("fk", notes[0], keep_summary=True)
    m = db.get_meeting(meeting_id)
    assert m["title"] == "Pricing review" and m["source"] == "fathom" and m["status"] == "done"
    assert "[00:05] You: Shall we raise prices?" in m["transcript"] and "Them: In May, yes." in m["transcript"]
    assert "Prices go up in May" in m["summary"]
    assert [t["text"] for t in db.tasks_for(meeting_id)] == ["Dana to send the price sheet"]
    assert fathom.import_note("fk", notes[0], keep_summary=True) is None  # not twice
    assert fathom.notes("fk", None)[0]["imported"] is True


def test_fathom_notes_still_being_written_wait(fresh_db, monkeypatch):
    just_now = datetime.now(timezone.utc) - timedelta(minutes=40)
    fake_fathom(monkeypatch, [fathom_meeting(21, just_now)], summary=None)
    note = fathom.notes("fk", None)[0]
    assert fathom.import_note("fk", note, keep_summary=True) is None
    assert fathom.import_note("fk", note, keep_summary=False)  # without its notes, it can come in now


def test_fathom_refuses_a_bad_key(fresh_db, monkeypatch):
    monkeypatch.setattr(fathom.httpx, "get", lambda *a, **k: Response(401, {}))
    with pytest.raises(importer.ImportError_, match="didn't accept"):
        fathom.notes("bad", None)


# ── Fireflies ───────────────────────────────────────────────────────────

def fake_fireflies(monkeypatch, listed: int, status="processed", sentences=None, asked=None):
    asked = asked if asked is not None else []
    when = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)

    def post(url, json=None, headers=None, timeout=None):
        assert url == fireflies.API and headers == {"Authorization": "Bearer ffk"}
        asked.append(json["variables"])
        if "transcripts(" in json["query"]:
            skip = json["variables"]["skip"]
            page = [{"id": f"ff{i}", "title": f"Call {i}", "dateString": iso(when)}
                    for i in range(skip, min(listed, skip + fireflies._PAGE))]
            return Response(200, {"data": {"transcripts": page}})
        return Response(200, {"data": {"transcript": {
            "id": json["variables"]["id"], "title": "Hiring sync", "dateString": iso(when), "duration": 12,
            "user": {"email": "dale@example.com", "name": "Dale Morgan"},
            "sentences": sentences if sentences is not None else [
                {"speaker_name": "Dale Morgan", "text": "Who's next?", "start_time": "1.5", "end_time": "2.4"},
                {"speaker_name": "Dale Morgan", "text": "Priya?", "start_time": "2.6", "end_time": "3.1"},
                {"speaker_name": "Priya", "text": "Thursday works.", "start_time": "4.0", "end_time": "5.2"}],
            "summary": {"overview": "We picked a date.",
                        "action_items": "**Priya**\nBook the room (00:04)\n\n**Dale Morgan**\nSend the invite"},
            "meeting_info": {"summary_status": status}}}})

    monkeypatch.setattr(fireflies.httpx, "post", post)
    return asked


def test_fireflies_lists_in_pages_of_50(fresh_db, monkeypatch):
    asked = fake_fireflies(monkeypatch, listed=60)
    notes = fireflies.notes("ffk", datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len(notes) == 60 and [a["skip"] for a in asked] == [0, 50]
    assert asked[0]["fromDate"] == "2026-10-01T00:00:00.000Z"


def test_fireflies_meeting_comes_in_with_turns_and_tasks(fresh_db, monkeypatch):
    fake_fireflies(monkeypatch, listed=1)
    meeting_id = fireflies.import_note("ffk", fireflies.notes("ffk", None)[0], keep_summary=True)
    m = db.get_meeting(meeting_id)
    assert m["title"] == "Hiring sync" and m["source"] == "fireflies" and m["status"] == "done"
    assert "You: Who's next? Priya?" in m["transcript"] and "Them: Thursday works." in m["transcript"]
    assert [t["text"] for t in db.tasks_for(meeting_id)] == ["Priya: Book the room (00:04)",
                                                              "Dale Morgan: Send the invite"]


def test_fireflies_times_in_milliseconds_are_read_as_such(fresh_db):
    segs = fireflies.segments([{"speaker_name": "A", "text": "Hi", "start_time": 61000, "end_time": 62500}],
                              "", duration_min=5)
    assert segs[0]["start"] == 61.0 and segs[0]["end"] == 62.5


def test_fireflies_waits_while_its_summary_is_being_written(fresh_db, monkeypatch):
    fake_fireflies(monkeypatch, listed=1, status="processing")
    assert fireflies.import_note("ffk", {"id": "ff0", "title": ""}, keep_summary=True) is None


def test_fireflies_errors_say_what_happened(fresh_db, monkeypatch):
    def answer(code, status=200, retry=None):
        ext = {"code": code, **({"metadata": {"retryAfter": retry}} if retry else {})}
        monkeypatch.setattr(fireflies.httpx, "post", lambda *a, **k: Response(
            status, {"data": None, "errors": [{"message": "nope", "code": code, "extensions": ext}]}))

    answer("auth_failed")
    with pytest.raises(importer.ImportError_, match="didn't accept"):
        fireflies.notes("bad", None)
    answer("too_many_requests", 429, retry=36000)
    with pytest.raises(importer.ImportError_, match="used up"):
        fireflies.notes("ffk", None)
    answer("paid_required", 403)
    with pytest.raises(importer.ImportError_, match="paid plan"):
        fireflies.notes("ffk", None)
