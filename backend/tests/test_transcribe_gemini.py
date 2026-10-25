import math

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


# ── stretches the model skipped ──

def test_find_gaps_is_a_long_wordless_stretch_with_speech_in_it(monkeypatch):
    monkeypatch.setattr(g.vad, "speech_regions", lambda audio: [(0, 300 * g.SR)])  # someone talks all the way through
    audio = np.zeros(300 * g.SR, dtype=np.float32)
    words = [word("a", "spk_1", t, t + 1) for t in range(0, 100, 2)] + [word("b", "spk_1", 200, 201)]
    assert g.find_gaps(audio, words) == [(99, 200), (201, 300)]
    pauses = [word("a", "spk_1", t, t + 1) for t in range(0, 300, 20)]  # 19 s pauses: normal
    assert g.find_gaps(audio, pauses) == []


def test_find_gaps_ignores_a_long_silence(monkeypatch):
    monkeypatch.setattr(g.vad, "speech_regions", lambda audio: [(0, 100 * g.SR), (290 * g.SR, 300 * g.SR)])
    words = [word("a", "spk_1", t, t + 1) for t in range(0, 100, 2)]
    assert g.find_gaps(np.zeros(300 * g.SR, dtype=np.float32), words) == []  # 10 s of speech in 200 s


def _fake_google(monkeypatch, answered_until=None):
    """Google faked: the audio's samples hold their own time (seconds / 1000), so the fake knows which stretch it was
    sent. It answers with a word every 2 s, the speaker changing every 10 s; the first answer stops at
    `answered_until`, and later ones (redos) number the speakers the other way round."""
    import settings

    sent = []

    def call(file, mime, key, language, diarize, model, usage):
        piece = file["audio"]
        offset = float(piece[0]) * 1000
        end = offset + len(piece) / g.SR
        sent.append((round(offset), round(end)))
        redo = len(sent) > 1
        out = []
        for at in range(2 * math.ceil(offset / 2), math.ceil(end), 2):  # a word just after every even second
            t = at + 0.1 - offset
            if not 0 <= t < end - offset:
                continue
            if not redo and answered_until is not None and at >= answered_until:
                break
            first = (at // 10) % 2 == 0
            out.append(word(str(at), "spk_1" if first != redo else "spk_2", t, t + 1))
        return out

    monkeypatch.setattr(settings, "locked", lambda: False)
    monkeypatch.setattr(g, "encode", lambda audio: audio)
    monkeypatch.setattr(g, "upload", lambda data, mime, key, name="": {"name": name, "audio": data})
    monkeypatch.setattr(g, "delete_file", lambda file, key: None)
    monkeypatch.setattr(g, "call", call)
    monkeypatch.setattr(g.vad, "speech_regions", lambda audio: [(0, len(audio))])
    return sent


def _clock_audio(seconds):
    return (np.arange(seconds * g.SR, dtype=np.float64) / g.SR / 1000).astype(np.float32)


def test_a_stretch_the_model_skipped_is_transcribed_again_and_put_in_place(monkeypatch):
    sent = _fake_google(monkeypatch, answered_until=150)
    usage = {}
    segs = g.transcribe(_clock_audio(400), "key", usage=usage)
    assert sent == [(0, 400), (129, 400)]                       # once, then the skipped part with 20 s before it
    said = [int(w) for s in segs for w in s["text"].split()]
    assert said == list(range(0, 400, 2))                       # every word once, in order
    for s in segs:                                              # the redo's speakers matched to the first answer's
        assert s["speaker"] == ("Speaker 1" if (int(s["text"].split()[0]) // 10) % 2 == 0 else "Speaker 2")
    assert abs(usage["redo_seconds"] - (400 - 129.1)) < 0.01   # the last word ended at 149.1 s


def test_a_complete_answer_is_not_sent_again(monkeypatch):
    sent = _fake_google(monkeypatch)
    usage = {}
    g.transcribe(_clock_audio(400), "key", usage=usage)
    assert sent == [(0, 400)] and "redo_seconds" not in usage


def test_a_long_skipped_stretch_is_redone_in_smaller_requests(monkeypatch):
    sent = _fake_google(monkeypatch, answered_until=100)
    segs = g.transcribe(_clock_audio(g.MAX_CHUNK_S - 60), "key")
    assert sent[0] == (0, g.MAX_CHUNK_S - 60)
    assert len(sent) > 2 and all(b - a <= g.REDO_MAX_S + 1 for a, b in sent[1:])
    said = [int(w) for s in segs for w in s["text"].split()]
    assert said == list(range(0, g.MAX_CHUNK_S - 60, 2))
