"""Energy-based speech region detection.

Transcribing only where someone is actually talking gives exact timestamps (each region is
placed at its true offset), avoids Whisper inventing words in silence, and skips the idle
stretches of a track (the meeting-audio track is silent whenever you are the one speaking).
"""
import numpy as np

SR = 16000
FRAME_S = 0.03
MERGE_GAP_S = 0.8   # pauses shorter than this stay inside one region
PAD_S = 0.3         # context kept around each region
MIN_LEN_S = 0.25    # shorter blips (clicks, taps) are ignored
MAX_LEN_S = 45.0    # longer regions are split at their quietest point


def speech_regions(audio: np.ndarray) -> list[tuple[int, int]]:
    """Returns [(start_sample, end_sample), ...] covering the speech in a mono 16 kHz signal."""
    frame = int(SR * FRAME_S)
    n = len(audio) // frame
    if n == 0:
        return []
    rms = np.sqrt(np.mean(np.square(audio[: n * frame].reshape(n, frame)), axis=1))
    floor = float(np.percentile(rms, 10))  # background noise level of this particular track
    active = rms > min(max(0.003, floor * 3), 0.02)

    # runs of consecutive active frames
    edges = np.diff(np.concatenate(([0], active.astype(np.int8), [0])))
    runs = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))

    merged: list[list[int]] = []
    gap = int(MERGE_GAP_S / FRAME_S)
    for s, e in runs:
        if merged and s - merged[-1][1] <= gap:
            merged[-1][1] = e
        else:
            merged.append([int(s), int(e)])

    pad, max_len = int(PAD_S / FRAME_S), int(MAX_LEN_S / FRAME_S)
    regions = []
    for s, e in merged:
        if e - s < MIN_LEN_S / FRAME_S:
            continue
        while e - s > max_len:  # split at the quietest frame in the last third of the window
            lo, hi = s + max_len * 2 // 3, s + max_len
            cut = lo + int(np.argmin(rms[lo:hi]))
            regions.append((s, cut))
            s = cut
        regions.append((s, e))

    out = []
    for s, e in regions:
        a, b = int(max(0, s - pad) * frame), int(min(n, e + pad) * frame)
        if out and a <= out[-1][1]:  # padding made neighbours touch
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out
