"""What speech engines cost, so Trailmix can say so before it spends your money (and tally what it did).

Prices are Google's and OpenAI's published per-minute rates for audio, as of 2026-10-12, in US dollars. They are
estimates: rates change (Google's Flash prices rise on 2027-01-01), and a meeting is billed per track (your
microphone and the call's audio are transcribed separately), so a two-track meeting costs twice its length.
"""
import fnmatch

AS_OF = "2026-10-12"

# Per minute of audio sent, input and output together. Local transcription is free.
PER_MINUTE = [
    ("gemini-3.5-transcribe-live*", 0.009),   # $0.005 audio in + $0.004 text out
    ("gemini-3.5-transcribe*", 0.005),        # $0.003 audio in + $0.002 text out
    ("gpt-4o-mini-transcribe*", 0.003),
    ("gpt-4o-transcribe*", 0.006),
    ("whisper-1*", 0.006),
]


def per_minute(engine: str, model: str | None) -> float | None:
    """Dollars per minute of audio, or None if the price isn't known (a self-hosted endpoint, a new model)."""
    if engine == "local":
        return 0.0
    name = (model or "").lower().rsplit("/", 1)[-1]
    for pattern, price in PER_MINUTE:
        if fnmatch.fnmatch(name, pattern):
            return price
    return None


def meeting_minutes(duration_sec: float | None, has_system: bool) -> float:
    """Minutes of audio a meeting would send: each recorded track counts. (An upper bound: a silent track isn't sent.)"""
    return (duration_sec or 0) / 60 * (2 if has_system else 1)


def cost(minutes: float, engine: str, model: str | None) -> float | None:
    price = per_minute(engine, model)
    return None if price is None else round(minutes * price, 4)
