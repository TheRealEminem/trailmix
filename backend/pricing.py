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
    # General models asked to transcribe (transcribe_gemini.call_llm): 25 audio tokens per second in, about 330 tokens
    # of text per minute out (Flash adds thinking tokens). Measured on one 23-minute meeting, so only roughly.
    ("gemini-3.5-flash-lite*", 0.0013),
    ("gemini-3.1-flash-lite*", 0.0013),
    ("gemini-flash-lite-latest*", 0.0013),
    ("gemini-3.8-flash*", 0.0042),
    ("gemini-3.6-flash*", 0.0042),
    ("gemini-flash-latest*", 0.0042),
    ("gemini-3.5-transcribe*", 0.005),        # $0.003 audio in + $0.002 text out
    ("gpt-4o-mini-transcribe*", 0.003),
    ("gpt-4o-transcribe*", 0.006),
    ("whisper-1*", 0.006),
]


# Text models, dollars per million tokens (input, output), as of AS_OF. Thinking tokens count as output. The Batch API
# charges half. "latest" names follow whichever model Google points them at today.
TEXT_MODELS = [
    ("gemini-flash-lite-latest", (0.30, 2.50)),
    ("gemini-flash-latest", (0.75, 3.75)),
    ("gemini-3.8-flash*", (0.75, 3.75)),
    ("gemini-3.6-flash*", (0.75, 3.75)),
    ("gemini-3.5-flash-lite*", (0.30, 2.50)),
    ("gemini-3.1-flash-lite*", (0.25, 1.50)),
]
BATCH_DISCOUNT = 0.5


def text_cost(model: str, tokens_in: int, tokens_out: int, batch: bool = False) -> float | None:
    """Dollars for this many tokens in and out (thinking included in out), or None for a model whose price isn't known."""
    name = model.lower().removeprefix("models/")
    for pattern, (pin, pout) in TEXT_MODELS:
        if fnmatch.fnmatch(name, pattern):
            return round((tokens_in * pin + tokens_out * pout) / 1e6 * (BATCH_DISCOUNT if batch else 1), 5)
    return None


def per_minute(engine: str, model: str | None) -> float | None:
    """Dollars per minute of audio, or None if the price isn't known (a self-hosted endpoint, a new model)."""
    if engine == "local":
        return 0.0
    name = (model or "").lower().rsplit("/", 1)[-1]
    for pattern, price in PER_MINUTE:
        if fnmatch.fnmatch(name, pattern):
            return price
    return None


def transcript_cost(model: str, seconds: float, usage: dict | None) -> float | None:
    """What a transcription cost: from the tokens the model reported when it did (a general model), else from the
    per-minute price of the audio sent."""
    if usage and usage.get("in"):
        exact = text_cost(model, usage["in"], usage["out"])
        if exact is not None:
            return exact
    return cost(seconds / 60, "gemini", model)


def meeting_minutes(duration_sec: float | None, has_system: bool, mixed: bool = False) -> float:
    """Minutes of audio a meeting would send: each recorded track counts, unless they're mixed into one. (An upper
    bound: a silent track isn't sent.)"""
    return (duration_sec or 0) / 60 * (2 if has_system and not mixed else 1)


def cost(minutes: float, engine: str, model: str | None) -> float | None:
    price = per_minute(engine, model)
    return None if price is None else round(minutes * price, 4)
