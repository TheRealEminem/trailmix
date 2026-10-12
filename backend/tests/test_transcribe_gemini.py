import numpy as np

import transcribe_gemini as g


def word(text, speaker, start, end):
    return {"text": text, "speaker": speaker, "start": start, "end": end}


def info(text, speaker, start, end):
    return {"type": "word_info", "text": text, "speaker": speaker, "start_offset": f"{start}s", "end_offset": f"{end}s"}


def test_parse_words_finds_annotations_however_they_are_nested():
    body = {"status": "completed", "steps": [
        {"type": "model_output", "content": [{"type": "text", "annotations": [
            info("Hello", "spk_1", 0.1, 0.45), info("there", "spk_1", 0.5, 0.9)]}]},
        {"content": [{"annotations": [info("Hi", "spk_2", 1.2, 1.4), {"type": "other", "text": "x"}]}]},
    ]}
    words = g.parse_words(body)
    assert [w["text"] for w in words] == ["Hello", "there", "Hi"]
    assert words[2] == {"text": "Hi", "speaker": "spk_2", "start": 1.2, "end": 1.4}


def test_parse_words_without_a_speaker_label_is_one_speaker():
    words = g.parse_words({"annotations": [{"type": "word_info", "text": "ok", "start_offset": "0s", "end_offset": "1s"}]})
    assert words[0]["speaker"] == "spk_1"


def test_group_words_splits_on_speaker_pause_and_length():
    words = [word("a", "spk_1", 0, 1), word("b", "spk_1", 1.2, 2), word("c", "spk_2", 2.1, 3),
             word("d", "spk_2", 5, 6),  # a pause of 2 s
             word("e", "spk_2", 6, 40)]  # runs on past SEGMENT_MAX_S
    segs = g.group_words(words)
    assert [(s["speaker"], s["text"]) for s in segs] == [
        ("spk_1", "a b"), ("spk_2", "c"), ("spk_2", "d"), ("spk_2", "e")]
    assert segs[0]["start"] == 0 and segs[0]["end"] == 2


def test_plan_chunks_single_when_short():
    assert g.plan_chunks(np.zeros(60 * g.SR, dtype=np.float32)) == [(0.0, 60.0)]


def test_plan_chunks_overlap_and_cut_at_quiet_moment():
    audio = np.ones(60 * 60 * g.SR, dtype=np.float32) * 0.1  # an hour of steady sound
    quiet = int((g.MAX_CHUNK_S - 30) * g.SR)
    audio[quiet:quiet + g.SR] = 0                            # one quiet second, 30 s before the first limit
    chunks = g.plan_chunks(audio)
    assert len(chunks) == 3
    assert abs(chunks[0][1] - (g.MAX_CHUNK_S - 30 + 0.5)) < 1
    assert chunks[1][0] == chunks[0][1] - g.OVERLAP_S
    assert chunks[-1][1] == 3600
    assert all(b - a <= g.MAX_CHUNK_S + 1 for a, b in chunks)


def test_match_speakers_pairs_labels_by_shared_speech():
    prev = [word("x", "spk_1", 100, 104), word("y", "spk_2", 104, 109)]
    cur = [word("x", "spk_2", 100, 104), word("y", "spk_1", 104, 109)]  # the other chunk numbered them the other way
    assert g.match_speakers(prev, cur, 90, 110) == {"spk_2": "spk_1", "spk_1": "spk_2"}


def test_match_speakers_ignores_a_brief_overlap():
    prev = [word("x", "spk_1", 100, 101)]
    cur = [word("x", "spk_1", 100, 101)]
    assert g.match_speakers(prev, cur, 90, 110) == {}


def test_stitch_keeps_labels_consistent_across_chunks():
    # Chunk 1 covers 0-100 s. Chunk 2 starts at 60 s and numbers the two people the other way round.
    first = [word("hi", "spk_1", 5, 6), word("a", "spk_1", 65, 75), word("yes", "spk_2", 80, 98)]
    second = [word("a", "spk_2", 5, 15), word("yes", "spk_1", 20, 38),     # the overlap, 65-98 s
              word("later", "spk_1", 45, 50),                              # 105 s: the person chunk 1 called spk_2
              word("new", "spk_3", 60, 70)]                                # 120 s: nobody seen before
    out = g.stitch([((0, 100), first), ((60, 140), second)])
    by_text = {w["text"]: w for w in out}
    assert by_text["later"]["speaker"] == "spk_2"
    assert by_text["later"]["start"] == 105
    assert by_text["new"]["speaker"].startswith("new_")
    assert [w["text"] for w in out].count("yes") == 1  # the overlap isn't repeated


def test_name_speakers_in_order_of_first_appearance():
    segs = [{"speaker": "spk_2", "text": "a"}, {"speaker": "spk_1", "text": "b"}, {"speaker": "spk_2", "text": "c"}]
    assert [s["speaker"] for s in g.name_speakers(segs)] == ["Speaker 1", "Speaker 2", "Speaker 1"]


def test_a_bad_key_is_reported_in_plain_words(monkeypatch):
    class Reply:
        status_code = 400
        text = '{"error": {"code": 400, "message": "API key not valid.", "details": [{"reason": "API_KEY_INVALID"}]}}'
        headers = {}

    monkeypatch.setattr(g.httpx, "request", lambda *a, **k: Reply())
    try:
        g._request("GET", "https://example.com", "bad")
    except g.GeminiError as e:
        assert "didn't accept the API key" in str(e)
    else:
        raise AssertionError("expected GeminiError")


def test_rate_limits_wait_as_long_as_google_asks_and_keep_trying(monkeypatch):
    class Reply:
        def __init__(self, status, text=""):
            self.status_code, self.text, self.headers = status, text, {}

    replies = [Reply(429, "Please retry in 8s or upgrade")] * 5 + [Reply(200)]
    waits = []
    monkeypatch.setattr(g.httpx, "request", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(g.time, "sleep", waits.append)
    assert g._request("POST", "https://example.com", "key").status_code == 200
    assert waits == [10.0] * 5  # 8 s asked for, plus a little


def test_rate_limit_that_never_clears_gives_a_plain_message(monkeypatch):
    class Reply:
        status_code, text, headers = 429, "slow down", {}

    monkeypatch.setattr(g.httpx, "request", lambda *a, **k: Reply())
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    try:
        g._request("POST", "https://example.com", "key")
    except g.GeminiError as e:
        assert "limiting how much audio" in str(e)
    else:
        raise AssertionError("expected GeminiError")
