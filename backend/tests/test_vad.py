import numpy as np

import silero
import vad

SR = 16000


def tone_speech(seconds, rng):
    """Not speech, but voiced-like: a buzzy harmonic signal with a syllable-rate envelope."""
    t = np.arange(int(seconds * SR)) / SR
    f0 = 120 + 30 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / SR
    voice = sum(np.sin(k * phase) / k for k in range(1, 20))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t) ** 2
    return (0.1 * voice * envelope + 0.002 * rng.standard_normal(len(t))).astype(np.float32)


def test_silence_has_no_speech():
    assert silero.probabilities(np.zeros(SR * 3, dtype=np.float32)).max() < 0.1
    assert vad.speech_regions(np.zeros(SR * 3, dtype=np.float32)) == []


def test_steady_noise_is_not_speech():
    noise = (0.01 * np.random.default_rng(1).standard_normal(SR * 5)).astype(np.float32)
    assert vad.speech_regions(noise) == []


def test_probabilities_cover_every_chunk():
    p = silero.probabilities(np.zeros(1000, dtype=np.float32))
    assert p.shape == (2,)


def test_regions_never_exceed_whisper_window():
    rng = np.random.default_rng(0)
    audio = tone_speech(75, rng)
    for a, b in vad.speech_regions(audio):
        assert (b - a) / SR <= vad.MAX_LEN_S + 2 * vad.PAD_S + 0.1


def test_regions_are_in_order_and_disjoint():
    rng = np.random.default_rng(0)
    audio = np.concatenate([tone_speech(4, rng), np.zeros(SR * 3, np.float32), tone_speech(4, rng)])
    regions = vad.speech_regions(audio)
    assert all(a < b for a, b in regions)
    assert all(regions[i][1] <= regions[i + 1][0] for i in range(len(regions) - 1))
