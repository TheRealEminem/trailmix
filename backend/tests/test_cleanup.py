import cleanup


def seg(start, end, text, **metrics):
    return {"start": start, "end": end, "text": text, **metrics}


def texts(segs):
    return [s["text"] for s in segs]


def test_keeps_ordinary_speech():
    segs = [seg(0, 2.5, "Let's start with the launch plan."), seg(3, 5, "Sounds good."), seg(6, 6.4, "Yeah.")]
    assert cleanup.drop_hallucinations(segs) == segs


def test_drops_decoding_loops():
    loop = [seg(18.5 + i * 0.12, 18.5 + i * 0.12 + 0.11, "I have a really good one!") for i in range(8)]
    kept = cleanup.drop_hallucinations([seg(17.5, 18.6, "I have a really good one!")] + loop)
    assert texts(kept) == ["I have a really good one!"]


def test_drops_zero_length_lines():
    assert cleanup.drop_hallucinations([seg(584.47, 584.47, "I have."), seg(584.5, 584.54, "I have.")]) == []


def test_drops_a_filled_in_window():
    assert cleanup.drop_hallucinations([seg(462.4, 492.4, "Thank you.")]) == []


def test_filler_phrases_only_go_when_doubtful():
    sure = seg(10, 11, "Thank you.", avg_logprob=-0.2, no_speech_prob=0.05)
    doubtful = seg(20, 21, "Thank you.", avg_logprob=-1.3, no_speech_prob=0.4)
    assert cleanup.drop_hallucinations([sure, doubtful]) == [sure]


def test_drops_repetitive_text():
    assert cleanup.drop_hallucinations([seg(0, 8, "a a a a a a a a a", compression_ratio=3.1)]) == []


def test_echo_of_the_other_side_is_removed():
    them = [seg(675.3, 681.3, "the calendar we don't have to get into this super in-depth but it just I mean")]
    mine = seg(675.4, 688.6, "The calendar, we don't have to get into this super in-depth, but it just, I mean")
    assert cleanup.remove_echo([mine], them) == []


def test_own_words_that_share_a_few_words_stay():
    them = [seg(768.0, 770.0, "Yeah, I think I think it's totally doable")]
    mine = seg(766.3, 767.3, "Today, I think.")
    assert cleanup.remove_echo([mine], them) == [mine]


def test_short_echo_must_line_up():
    them = [seg(100.0, 100.5, "Yeah.")]
    assert cleanup.remove_echo([seg(100.2, 100.6, "Yeah.")], them) == []
    assert cleanup.remove_echo([seg(101.5, 101.9, "Yeah.")], them) != []


def test_metrics_are_stripped_before_saving():
    out = cleanup.strip_metrics([seg(0, 1, "Hi.", avg_logprob=-0.1, no_speech_prob=0.0, compression_ratio=1.0)])
    assert out == [{"start": 0, "end": 1, "text": "Hi."}]
