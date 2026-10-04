"""Speech region detection: where in a track someone is actually talking.

Transcribing only those regions gives exact timestamps (each region is placed at its true offset), keeps
Whisper from inventing words in silence and background noise, and skips the idle stretches of a track (the
meeting-audio track is silent whenever you are the one speaking).

Uses the Silero voice-activity model (silero.py), which tells speech from noise, keyboard clatter and
music far better than loudness alone; falls back to a loudness threshold if its weights are missing.
"""
import numpy as np

import silero

SR = 16000
MERGE_GAP_S = 0.8   # pauses shorter than this stay inside one region
PAD_S = 0.3         # context kept around each region
MIN_LEN_S = 0.25    # shorter blips (clicks, taps) are ignored
MAX_LEN_S = 30.0    # longer regions are split at their least speech-like point (Whisper works in 30 s windows)
ON, OFF = 0.5, 0.35  # speech starts above ON and continues until the probability drops below OFF


def speech_regions(audio: np.ndarray) -> list[tuple[int, int]]:
    """Returns [(start_sample, end_sample), ...] covering the speech in a mono 16 kHz signal."""
    if silero.available():
        score, frame = silero.probabilities(audio), silero.CHUNK
        active = _hysteresis(score, ON, OFF)
    else:
        score, frame = _loudness(audio)
        active = score > min(max(0.003, float(np.percentile(score, 10)) * 3), 0.02) if len(score) else score
    return _regions(active, score, frame, len(audio))


def _hysteresis(p: np.ndarray, on: float, off: float) -> np.ndarray:
    active = np.zeros(len(p), dtype=bool)
    speaking = False
    for i, x in enumerate(p):
        speaking = x > on or (speaking and x > off)
        active[i] = speaking
    return active


def _loudness(audio: np.ndarray) -> tuple[np.ndarray, int]:
    frame = int(SR * 0.03)
    n = len(audio) // frame
    return np.sqrt(np.mean(np.square(audio[: n * frame].reshape(n, frame)), axis=1)), frame


def _regions(active: np.ndarray, score: np.ndarray, frame: int, total: int) -> list[tuple[int, int]]:
    n = len(active)
    if n == 0:
        return []
    frame_s = frame / SR
    edges = np.diff(np.concatenate(([0], active.astype(np.int8), [0])))
    runs = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))

    merged: list[list[int]] = []
    gap = int(MERGE_GAP_S / frame_s)
    for s, e in runs:
        if merged and s - merged[-1][1] <= gap:
            merged[-1][1] = e
        else:
            merged.append([int(s), int(e)])

    pad, max_len = int(PAD_S / frame_s), int(MAX_LEN_S / frame_s)
    regions = []
    for s, e in merged:
        if e - s < MIN_LEN_S / frame_s:
            continue
        while e - s > max_len:  # split at the least speech-like frame in the last third of the window
            lo, hi = s + max_len * 2 // 3, s + max_len
            cut = lo + int(np.argmin(score[lo:hi]))
            regions.append((s, cut))
            s = cut
        regions.append((s, e))

    out = []
    for s, e in regions:
        a, b = int(max(0, s - pad) * frame), int(min(n, e + pad) * frame)
        b = min(b, total)
        if out and a <= out[-1][1]:  # padding made neighbours touch
            if b - out[-1][0] <= (max_len + 2 * pad) * frame:
                out[-1] = (out[-1][0], b)
                continue
            a = out[-1][1]  # the two halves of a split region: keep them apart
        out.append((a, b))
    return out
