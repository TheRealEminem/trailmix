"""Where Gemini calls go and how they're signed, in one place.

With your own key they go to Google, signed with the key. With Trailmix Cloud they go to Trailmix's server, which
runs Gemini on its own key, signed with this Mac's device token (tokens start with "tmc_"). The credential is passed
around as one string either way, so the modules that talk to Gemini (transcribe_gemini, gemini_batch,
llm_engine) build their addresses and headers from it here and never need to know which it is.
"""
import os

GOOGLE = "https://generativelanguage.googleapis.com"
CLOUD_TOKEN_PREFIX = "tmc_"


class NotConfigured(Exception):
    pass


# What the app asks Trailmix Cloud for. The server picks the model for each, by plan and level, so models and prices
# can change without an app update.
ROLES = ("transcribe", "transcribe_chunk", "notes", "quick")


def credential(cfg: dict, provider: str = "gemini") -> str:
    """The Google key for "gemini", this Mac's Trailmix Cloud token for "trailmix"; "" when it isn't set."""
    return (cfg.get("cloud_token") if provider == "trailmix" else cfg.get("gemini_api_key")) or ""


def ready(cfg: dict, provider: str = "gemini") -> bool:
    """Something to call Gemini with is set (lockdown is checked separately, where it applies)."""
    return bool(credential(cfg, provider))


def is_cloud(cred: str) -> bool:
    return cred.startswith(CLOUD_TOKEN_PREFIX)


def cloud_url() -> str:
    url = os.getenv("TRAILMIX_CLOUD_URL", "").rstrip("/")
    if not url:
        raise NotConfigured("Trailmix Cloud's address isn't set (TRAILMIX_CLOUD_URL)")
    return url


def base(cred: str) -> str:
    """The address the Gemini API paths (/v1beta/..., /upload/v1beta/...) are appended to."""
    return cloud_url() if is_cloud(cred) else GOOGLE


def headers(cred: str) -> dict:
    return {"Authorization": f"Bearer {cred}"} if is_cloud(cred) else {"x-goog-api-key": cred}


def meeting_headers(cred: str, meeting_id: int | None, track: str | None = None) -> dict:
    """Which meeting (and which side: "mic" or "call") some audio is from, so Trailmix Cloud counts a meeting's hours
    once, not once per track or per pass. Only Trailmix Cloud is told; Google never is."""
    if not is_cloud(cred) or meeting_id is None:
        return {}
    return {"X-Trailmix-Meeting": str(meeting_id), **({"X-Trailmix-Track": track} if track else {})}
