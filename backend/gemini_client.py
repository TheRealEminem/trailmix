"""Where Gemini calls go and how they're signed, in one place.

With your own key they go to Google, signed with the key. With Trailmix Cloud they go to Trailmix's server, which
runs Gemini on its own key, signed with this Mac's device token (tokens start with "tmc_"). The credential is passed
around as one string either way, so the modules that talk to Gemini (transcribe_gemini, gemini_batch, live_gemini,
llm_engine) build their addresses and headers from it here and never need to know which it is.
"""
import os

GOOGLE = "https://generativelanguage.googleapis.com"
CLOUD_TOKEN_PREFIX = "tmc_"
LIVE_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


class NotConfigured(Exception):
    pass


def credential(cfg: dict) -> str:
    """The Google key, or "" when there's nothing to call Gemini with."""
    return cfg.get("gemini_api_key") or ""


def ready(cfg: dict) -> bool:
    """Something to call Gemini with is set (lockdown is checked separately, where it applies)."""
    return bool(credential(cfg))


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


def live_url(cred: str) -> str:
    """The WebSocket address for live captions."""
    return base(cred).replace("https://", "wss://", 1).replace("http://", "ws://", 1) + LIVE_PATH
