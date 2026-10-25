"""Trailmix Cloud: the paid way to use Trailmix's AI without a Mac that runs it or a key of your own.

The app signs in like a TV does: it asks the server for a code, opens the browser at a page where you sign in (or
start a trial), and asks every few seconds until the server hands over a token for this Mac. The token goes in
settings.cloud_token; from then on Gemini calls made with it go to Trailmix Cloud instead of Google (gemini_client).

The account (plan, level, hours) is cached in settings.cloud_account, refreshed when asked and whenever a reply says
the level changed. The level is what decides features: "pro" has live captions and catch-up, "basic" doesn't. A Pro
plan drops to the Basic level for the rest of the month once its live hours are used, and the server says so.
"""
import logging
import os
import socket
import time

import httpx

import gemini_client
import settings

log = logging.getLogger("trailmix.cloud")

LEVEL_HEADER = "x-trailmix-level"
NO_ACCOUNT = {"plan": "none", "status": "lapsed", "level": "basic"}


class CloudError(Exception):
    pass


def _call(method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
    try:
        url = gemini_client.cloud_url() + path
    except gemini_client.NotConfigured as e:
        raise CloudError(str(e)) from e
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.request(method, url, headers=headers, timeout=20, **kw)
    except httpx.HTTPError as e:
        raise CloudError(f"Couldn't reach Trailmix Cloud: {e or type(e).__name__}") from e
    if r.status_code == 401:
        raise CloudError("This Mac is signed out of Trailmix Cloud; sign in again")
    if r.status_code >= 400 and r.status_code != 410:
        raise CloudError(error_text(r))
    return r


def error_text(r: httpx.Response) -> str:
    """The server's own sentence for a refusal (it writes them for people), else the status."""
    try:
        err = r.json().get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    except ValueError:
        pass
    return f"Trailmix Cloud returned {r.status_code}"


# ── Signing in ──────────────────────────────────────────────────────────

def link() -> dict:
    """Starts signing in: {code, link_url, expires_in, interval}. The UI opens link_url and polls with the code."""
    if settings.locked():
        raise CloudError(f"{settings.LOCKDOWN}, so Trailmix Cloud can't be used")
    return _call("POST", "/v1/link", json={"device_name": socket.gethostname().removesuffix(".local")}).json()


def poll(code: str) -> dict:
    """{status: pending|expired|done}. Once done, the token is saved and the account fetched."""
    r = _call("POST", "/v1/link/poll", json={"code": code})
    if r.status_code == 202:
        return {"status": "pending"}
    if r.status_code == 410:
        return {"status": "expired"}
    token = r.json().get("token", "")
    if not gemini_client.is_cloud(token):
        raise CloudError("Trailmix Cloud sent something that isn't a sign-in token")
    settings.update({"cloud_token": token}, internal=True)
    return {"status": "done", "account": account(refresh=True)}


def sign_out() -> None:
    """Forgets this Mac's token (and tells the server to revoke it, if it can be reached). Settings that used Trailmix
    Cloud are left as they are: they then say "sign in" where they'd otherwise work."""
    token = settings.get_all()["cloud_token"]
    if token:
        try:
            _call("POST", "/v1/signout", token)
        except CloudError as e:
            log.info("couldn't tell Trailmix Cloud about signing out: %s", e)
    settings.update({"cloud_token": "", "cloud_account": {}}, internal=True)


# ── The account ─────────────────────────────────────────────────────────

def account(refresh: bool = False, cfg: dict | None = None) -> dict:
    """The plan as last seen ({} when signed out). `refresh` asks the server first."""
    cfg = cfg or settings.get_all()
    token = cfg["cloud_token"]
    if not token:
        return {}
    if refresh:
        try:
            got = _call("GET", "/v1/account", token).json()
        except CloudError as e:
            log.info("couldn't refresh the Trailmix Cloud account: %s", e)
            return cfg["cloud_account"] or {}
        got["checked_at"] = time.time()
        settings.update({"cloud_account": got}, internal=True)
        return got
    return cfg["cloud_account"] or {}


def offered(cfg: dict) -> bool:
    """Whether to list Trailmix Cloud as a choice: this copy knows where it is, or it's in use already."""
    return bool(os.getenv("TRAILMIX_CLOUD_URL")) or "trailmix" in (
        cfg.get("transcribe_engine"), cfg.get("summary_provider"), cfg.get("summary_fallback"))


def level(cfg: dict) -> str:
    """"pro" or "basic": which features Trailmix Cloud gives right now (Basic when unknown)."""
    acct = cfg.get("cloud_account") or {}
    if not cfg.get("cloud_token") or acct.get("status") == "lapsed":
        return "basic"
    return "pro" if acct.get("level") == "pro" else "basic"


def note_level(value: str | None) -> None:
    """A reply said which level the account is at: keep the cache in step (no request needed)."""
    if value not in ("pro", "basic"):
        return
    cfg = settings.get_all()
    acct = cfg["cloud_account"] or {}
    if cfg["cloud_token"] and acct.get("level") != value:
        settings.update({"cloud_account": {**acct, "level": value}}, internal=True)


# ── What the level switches off ─────────────────────────────────────────

PRO_ONLY = "Live captions, catch-up and notes during the meeting are part of Trailmix Cloud Pro"
USED_UP = "This month's live hours are used. Meetings still get their transcript and notes right after"


def features_off(cfg: dict) -> dict[str, str]:
    """{feature: why it's off} for what Trailmix Cloud doesn't give at its current level. Features are "live_captions",
    "catch_up" and "live_notes"; only the ones that would run on Trailmix Cloud are listed."""
    uses = {"live_captions": cfg.get("transcribe_engine") == "trailmix",
            "catch_up": cfg.get("summary_provider") == "trailmix",
            "live_notes": cfg.get("summary_provider") == "trailmix"}
    if not any(uses.values()) or level(cfg) == "pro":
        return {}
    acct = cfg.get("cloud_account") or {}
    why = USED_UP if acct.get("plan") in ("pro", "max", "trial") and acct.get("status") != "lapsed" else PRO_ONLY
    return {feature: why for feature, used in uses.items() if used}
