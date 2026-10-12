import numpy as np

import mixdown

SR = mixdown.SR


def speech(seconds, level, seed):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * level).astype(np.float32)


def tracks_for(plan, echo=0.0):
    """plan: [(speaker, seconds)]. You speak into the mic; the others come through the call's audio, and your
    microphone hears them at `echo` of their volume (laptop speakers)."""
    mic, system = [], []
    for who, secs in plan:
        sound = speech(secs, 0.2, hash(who) % 1000)
        if who == "you":
            mic.append(sound)
            system.append(np.zeros_like(sound))
        else:
            system.append(sound)
            mic.append(sound * echo)
    return np.concatenate(mic), np.concatenate(system)


def segments(plan, labels):
    out, t = [], 0.0
    for who, secs in plan:
        out.append({"start": t, "end": t + secs, "speaker": labels[who], "text": f"{who} talking"})
        t += secs
    return out


PLAN = [("you", 4), ("dana", 5), ("you", 3), ("sam", 4), ("dana", 3), ("you", 2)]
LABELS = {"you": "spk_2", "dana": "spk_1", "sam": "spk_3"}   # the engine's numbering means nothing in itself


def test_the_speaker_on_your_microphone_is_you_the_rest_are_numbered():
    mic, system = tracks_for(PLAN)
    out = mixdown.name_you(segments(PLAN, LABELS), mic, system)
    assert [s["speaker"] for s in out] == ["You", "Speaker 1", "You", "Speaker 2", "Speaker 1", "You"]


def test_it_still_works_when_your_microphone_hears_the_call_through_speakers():
    mic, system = tracks_for(PLAN, echo=0.5)
    out = mixdown.name_you(segments(PLAN, LABELS), mic, system)
    assert [s["speaker"] for s in out] == ["You", "Speaker 1", "You", "Speaker 2", "Speaker 1", "You"]


def test_one_voice_on_the_other_side_is_them():
    plan = [("you", 4), ("dana", 5), ("you", 3)]
    mic, system = tracks_for(plan)
    out = mixdown.name_you(segments(plan, {"you": "a", "dana": "b"}), mic, system)
    assert [s["speaker"] for s in out] == ["You", "Them", "You"]


def test_nobody_on_the_microphone_means_nobody_is_you():
    plan = [("dana", 5), ("sam", 4)]
    mic, system = tracks_for(plan)
    out = mixdown.name_you(segments(plan, {"dana": "a", "sam": "b"}), mic, system)
    assert [s["speaker"] for s in out] == ["Speaker 1", "Speaker 2"]


def test_tracks_recorded_at_different_volumes_compare_fairly():
    mic, system = tracks_for(PLAN)
    out = mixdown.name_you(segments(PLAN, LABELS), mic * 0.05, system * 2)   # a quiet mic, a loud call
    assert out[0]["speaker"] == "You" and out[1]["speaker"] == "Speaker 1"


def test_mix_adds_the_tracks_and_never_clips():
    a, b = np.full(100, 0.8, dtype=np.float32), np.full(150, 0.8, dtype=np.float32)
    mixed = mixdown.mix(a, b)
    assert len(mixed) == 150 and float(np.max(np.abs(mixed))) <= 0.9801
    quiet = mixdown.mix(np.full(10, 0.1, dtype=np.float32), np.full(10, 0.2, dtype=np.float32))
    assert np.allclose(quiet, 0.3)


def test_with_several_people_on_your_microphone_the_one_who_talks_most_is_you():
    plan = [("guest", 3), ("you", 6), ("remote", 4), ("you", 5), ("guest", 2)]
    mic, system = [], []
    for who, secs in plan:
        sound = speech(secs, 0.2, hash(who) % 1000)
        (system if who == "remote" else mic).append(sound)
        (mic if who == "remote" else system).append(np.zeros_like(sound))
    mic, system = np.concatenate(mic), np.concatenate(system)
    out = mixdown.name_you(segments(plan, {"guest": "a", "you": "b", "remote": "c"}), mic, system)
    assert [s["speaker"] for s in out] == ["Speaker 1", "You", "Speaker 2", "You", "Speaker 1"]
