"""One recording instead of two: your microphone and the call's audio mixed, so a speech engine is paid for once.

Sending both tracks separately costs twice, but is what tells "you" from "them" for free. With one mixed file the
engine still tells voices apart (Speaker 1, Speaker 2...); which of them is *you* is then worked out here, from
the two tracks Trailmix still has: when you speak your microphone is loud and the call's audio is quiet, and when
someone else speaks it's the other way round, even if your microphone hears them through your speakers.
"""
import numpy as np

SR = 16000
FRAME_S = 0.25
YOU_SHARE = 0.55   # a speaker is "you" if your microphone carries at least this much of the sound when they speak


def mix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The two tracks added together (padded to the longer), scaled down only if the sum would clip."""
    n = max(len(a), len(b))
    out = np.zeros(n, dtype=np.float32)
    out[:len(a)] += a
    out[:len(b)] += b
    peak = float(np.max(np.abs(out), initial=0))
    return out / peak * 0.98 if peak > 0.98 else out


def _levels(audio: np.ndarray) -> np.ndarray:
    """Loudness per quarter second, scaled so the track's loud moments are about 1 (tracks recorded at
    different volumes then compare fairly)."""
    frame = int(FRAME_S * SR)
    n = len(audio) // frame
    if n == 0:
        return np.zeros(1)
    rms = np.sqrt(np.mean(np.square(audio[:n * frame].reshape(n, frame)), axis=1))
    loud = float(np.percentile(rms, 95))
    return rms / loud if loud > 1e-6 else rms


def mic_share(segments: list[dict], mic: np.ndarray, system: np.ndarray) -> dict[str, float]:
    """Per speaker: how much of the sound, while they speak, was on your microphone (0: all on the call's audio,
    1: all on your microphone). Several people can be on your microphone (a meeting in your room)."""
    lm, ls = _levels(mic), _levels(system)
    total: dict[str, float] = {}
    weight: dict[str, float] = {}
    for s in segments:
        who = s.get("speaker")
        if who is None:
            continue
        a, b = int(s["start"] / FRAME_S), max(int(s["end"] / FRAME_S), int(s["start"] / FRAME_S) + 1)
        m = float(np.mean(lm[a:b])) if a < len(lm) else 0.0
        c = float(np.mean(ls[a:b])) if a < len(ls) else 0.0
        if m + c < 1e-6:
            continue
        w = max(s["end"] - s["start"], 0.1)
        total[who] = total.get(who, 0.0) + w * m / (m + c)
        weight[who] = weight.get(who, 0.0) + w
    return {who: total[who] / weight[who] for who in total}


def name_you(segments: list[dict], mic: np.ndarray, system: np.ndarray) -> list[dict]:
    """Relabels the segments. Of the speakers on your microphone, the one who talks the most is "You" (it's your
    microphone); the others are numbered "Speaker 1", "Speaker 2"... in order of appearance, or all just "Them"
    if there is only one."""
    share = mic_share(segments, mic, system)
    talk: dict[str, float] = {}
    for s in segments:
        if s.get("speaker"):
            talk[s["speaker"]] = talk.get(s["speaker"], 0.0) + s["end"] - s["start"]
    on_mic = [who for who, v in share.items() if v >= YOU_SHARE]
    you = max(on_mic, key=lambda who: talk.get(who, 0.0)) if on_mic else None
    others = list(dict.fromkeys(s["speaker"] for s in segments if s.get("speaker") and s["speaker"] != you))
    names = {who: ("Them" if len(others) == 1 else f"Speaker {i + 1}") for i, who in enumerate(others)}
    names[you] = "You"
    return [{**s, "speaker": names.get(s.get("speaker"), s.get("speaker"))} for s in segments]
