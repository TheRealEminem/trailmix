"""Tidies Whisper's output before anyone sees it: hallucinations out, speaker echo out.

Whisper is a superb transcriber of speech and a confident inventor of it elsewhere. On silence, noise or a
stuck window it produces tell-tale output: the same short line repeated many times within a second or two,
lines with (almost) no duration, a whole 30-second window "transcribed" as "Thank you.", or highly
repetitive text. With laptop speakers, your mic also hears the other side, so their words come back as yours.
"""
import re
from difflib import SequenceMatcher

# Phrases Whisper is known to invent on silence and noise (from subtitles in its training data). Only dropped
# when the line also looks doubtful, since people do say them.
FILLER = {
    "thank you", "thank you very much", "thanks", "thanks for watching", "thank you for watching",
    "please subscribe", "bye", "you", "so", "um", "uh", "hmm", "i have", "subtitles by the amaraorg community",
}

MIN_DURATION_S = 0.15     # shorter lines are almost always a decoding loop
REPEAT_WINDOW_S = 3.0     # the same line again this soon after is a loop, not a person
WINDOW_FILLER_S = 20.0    # a line this long with only a few words is a window Whisper filled in
MAX_COMPRESSION = 2.4     # Whisper's own repetition measure (zlib ratio of the text)
DOUBTFUL_LOGPROB = -0.8
DOUBTFUL_NO_SPEECH = 0.5


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _norm(text: str) -> str:
    return " ".join(words(text)).replace("'", "")


def drop_hallucinations(segments: list[dict]) -> list[dict]:
    """One track's segments in time order, without the lines Whisper made up."""
    out: list[dict] = []
    for s in sorted(segments, key=lambda s: s["start"]):
        norm = _norm(s["text"])
        duration = s["end"] - s["start"]
        if not norm or duration < MIN_DURATION_S:
            continue
        if duration >= WINDOW_FILLER_S and len(norm.split()) <= 4:
            continue
        if s.get("compression_ratio", 0) > MAX_COMPRESSION:
            continue
        doubtful = s.get("avg_logprob", 0) < DOUBTFUL_LOGPROB or s.get("no_speech_prob", 0) > DOUBTFUL_NO_SPEECH
        if norm in FILLER and (doubtful or duration > 5):
            continue
        if out and _norm(out[-1]["text"]) == norm and s["start"] - out[-1]["end"] < REPEAT_WINDOW_S:
            continue
        out.append(s)
    return out


def is_echo(mine: dict, theirs: list[dict], slack: float = 1.5) -> bool:
    """True if a mic line is mostly the other side's words from around the same time, heard through the speakers."""
    said = words(mine["text"])
    if not said:
        return False
    near = [o for o in theirs if o["start"] < mine["end"] + slack and mine["start"] < o["end"] + slack]
    if not near:
        return False
    heard = [w for o in near for w in words(o["text"])]
    if len(said) < 3:  # "Yeah." on both sides at once: only an echo if it lines up closely
        return any(abs(o["start"] - mine["start"]) < 0.7 and words(o["text"]) == said for o in near)
    matched = sum(b.size for b in SequenceMatcher(None, said, heard, autojunk=False).get_matching_blocks())
    # Short lines share common words ("I think") by chance, so they must match more closely.
    return matched >= 3 and matched / len(said) >= (0.6 if len(said) >= 6 else 0.8)


def remove_echo(mic: list[dict], system: list[dict]) -> list[dict]:
    return [s for s in mic if not is_echo(s, system)]


METRICS = ("avg_logprob", "no_speech_prob", "compression_ratio")


def strip_metrics(segments: list[dict]) -> list[dict]:
    return [{k: v for k, v in s.items() if k not in METRICS} for s in segments]
