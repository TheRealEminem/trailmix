"""Trailmix Cloud: the paid way to use Trailmix's AI without a Mac that runs it or a key of your own.

The app signs in like a TV does: it asks the server for a code, opens the browser at a page where you sign in (or
start a trial), and asks every few seconds until the server hands over a token for this Mac. The token goes in
settings.cloud_token; from then on Gemini calls made with it go to Trailmix Cloud instead of Google (gemini_client).

The account (plan, level, hours) is cached in settings.cloud_account, refreshed when asked and whenever a reply says
the level changed. The level is what decides features: "pro" has live captions and catch-up, "basic" doesn't. A Pro
plan drops to the Basic level for the rest of the month once its live hours are used, and the server says so.
"""
import hashlib
import logging
import os
import socket
import subprocess
import sys
import time
import uuid

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
    if r.status_code >= 400 and r.status_code not in (404, 410):
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

# The code on screen is also in the sign-in page's address, so anyone who saw it could ask for the token first. The
# server also hands out a secret device code for asking; it stays here, never in the UI.
_device_codes: dict[str, str] = {}


def device_id() -> str:
    """The same for this Mac every time, and not the Mac's own serial: a hash of its hardware UUID (or, failing that, of
    its network address). The server keeps only a hash of this, to give each Mac one free trial."""
    raw = ""
    if sys.platform == "win32":
        try:  # the id Windows gave this install (not the machine's serial)
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                                 timeout=5).stdout
            raw = next((line.split('"')[-2] for line in out.splitlines() if "IOPlatformUUID" in line), "")
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return hashlib.sha256(f"trailmix-device:{raw or uuid.getnode()}".encode()).hexdigest()


def link() -> dict:
    """Starts signing in: {code, link_url, expires_in, interval}. The UI opens link_url and polls with the code."""
    if settings.locked():
        raise CloudError(f"{settings.LOCKDOWN}, so Trailmix Cloud can't be used")
    got = _call("POST", "/v1/link", json={"device_name": socket.gethostname().removesuffix(".local"),
                                         "device_id": device_id()}).json()
    secret = got.pop("device_code", None)
    if secret:
        _device_codes[got["code"]] = secret
    return got


def poll(code: str) -> dict:
    """{status: pending|expired|done}. Once done, the token is saved and the account fetched."""
    secret = _device_codes.get(code)
    r = _call("POST", "/v1/link/poll", json={"device_code": secret} if secret else {"code": code})
    if r.status_code == 202:
        return {"status": "pending"}
    if r.status_code in (404, 410):  # ran out, already used, or not known (the server restarted, say)
        _device_codes.pop(code, None)
        return {"status": "expired"}
    _device_codes.pop(code, None)
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
