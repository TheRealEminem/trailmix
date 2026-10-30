#!/usr/bin/env python3
"""The release gate's checks that are the same on every computer, run against a Trailmix engine that's already
up. Each platform's own smoke test (scripts/smoke-test.sh on a Mac) installs, updates and starts the app its own
way, and calls these for the rest. Standard library only, so any Python 3.9+ runs it.

  smoke_test.py check-engine URL UI_DIR     it's this app's engine (it serves UI_DIR), and its speech models are in
  smoke_test.py transcribe URL WAV WORD...  WAV imported as a meeting is transcribed, with each WORD in it
  smoke_test.py meetings URL                how many meetings it has
  smoke_test.py read-photo URL PHOTO WORD...  PHOTO attached to a meeting is read here (text recognition), and
                                            search finds each WORD in it

Speech for the tests is in scripts/fixtures (me.wav, them.wav: 16 kHz mono, made once with macOS's `say`).
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Failed(Exception):
    pass


def send(url: str, body: dict, method: str = "POST", timeout: float = 60) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise Failed(f"{url} answered {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e


def get(url: str, timeout: float = 10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body[:1] in (b"{", b"[") else body.decode("utf-8", "replace")


def post(url: str, data: bytes, timeout: float = 60) -> dict:
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise Failed(f"{url} answered {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e


def check_engine(url: str, ui_dir: str, models_timeout: float = 300) -> None:
    served = re.search(r"assets/index-[A-Za-z0-9_-]*\.js", get(f"{url}/"))
    if not served or not (Path(ui_dir) / served.group(0)).is_file():
        raise Failed(f"the engine at {url} isn't this app's (it serves {served and served.group(0)})")
    deadline = time.time() + models_timeout
    while True:
        missing = [m["repo"] for m in get(f"{url}/api/models")["models"] if not m["installed"]]
        if not missing:
            break
        if time.time() > deadline:
            raise Failed(f"the speech models didn't download: {', '.join(missing)}")
        time.sleep(1)
    print("this app's engine, with its speech models")


def transcribe(url: str, wav: str, words: list[str], timeout: float = 300) -> None:
    path = Path(wav)
    made = post(f"{url}/api/import/file?name={path.name}&modified={int(time.time() * 1000)}", path.read_bytes())
    if made.get("kind") != "recording" or not made.get("id"):
        raise Failed(f"importing {path.name} didn't make a meeting: {made}")
    deadline = time.time() + timeout
    confirmed = False
    while True:
        m = get(f"{url}/api/meetings/{made['id']}")
        if m.get("transcript"):
            break
        if m.get("status") == "ready_transcribe" and not confirmed:  # an import waits for you to say go, as here
            post(f"{url}/api/meetings/{made['id']}/confirm", b"")
            confirmed = True
        if m.get("status") in ("error", "waiting_confirm"):
            raise Failed(f"the imported meeting stopped at {m['status']}: {m.get('error') or m.get('wait_reason')}")
        if time.time() > deadline:
            raise Failed(f"no transcript after {timeout:.0f}s (status {m.get('status')})")
        time.sleep(1)
    text = m["transcript"].lower()
    missing = [w for w in words if w.lower() not in text]
    if missing:
        raise Failed(f"the transcript doesn't have {', '.join(missing)}: {m['transcript'][:300]}")
    print(f"transcribed {path.name}: {m['transcript'][:120]}")


def meetings(url: str) -> None:
    found = get(f"{url}/api/meetings")
    print(len(found if isinstance(found, list) else found["meetings"]))


def read_photo(url: str, photo: str, words: list[str], timeout: float = 600) -> None:
    """Attaches PHOTO to the first meeting, searchable, with Settings → Attachments on "this computer": its text is
    recognised here (the model downloads the first time) and search finds it. A digest needs a local AI model,
    which isn't here, so the attachment may end with "No digest yet"; its text is searchable either way."""
    path = Path(photo)
    send(f"{url}/api/settings", {"attachments_engine": "local"}, "PUT")
    found = get(f"{url}/api/meetings")
    mid = (found if isinstance(found, list) else found["meetings"])[0]["id"]
    if any(h["id"] == mid for h in get(f"{url}/api/search?q={words[0]}")):
        raise Failed(f"meeting {mid} already matches {words[0]!r}, so the photo's text couldn't be told apart")
    name = f"whiteboard{path.suffix}"
    added = post(f"{url}/api/meetings/{mid}/attachments?name={name}&searchable=true", path.read_bytes())
    if "pending" in added:  # a HEIC photo is always offered shrinking: keep it as it is, so it's read as a HEIC
        added = {"attachment": send(f"{url}/api/meetings/{mid}/attachments/pending/{added['pending']['id']}",
                                    {"name": name, "shrink": False, "searchable": True})}
    att_id = added["attachment"]["id"]
    deadline = time.time() + timeout
    while True:
        att = next(a for a in get(f"{url}/api/meetings/{mid}")["attachments"] if a["id"] == att_id)
        if att["status"] != "reading":
            break
        if time.time() > deadline:
            raise Failed(f"{path.name} was still being read after {timeout:.0f}s")
        time.sleep(1)
    if not att["has_text"]:
        raise Failed(f"no text was read from {path.name}: {att['status']}, {att.get('error')}")
    hits = [h for h in get(f"{url}/api/search?q={words[0]}") if h["id"] == mid]
    snippet = " ".join(re.sub(r"[^\w$,.:%]+", " ", hits[0]["snippet"]).split()) if hits else ""
    missing = [w for w in words if w.lower() not in snippet.lower()]
    if missing:
        raise Failed(f"the photo's text doesn't have {', '.join(missing)}: {snippet[:200]!r} ({att.get('error')})")
    print(f"read {path.name} here: {snippet[:120]}")


def main(argv: list[str]) -> int:
    commands = {"check-engine": (check_engine, 2), "transcribe": (lambda u, w, *ws: transcribe(u, w, list(ws)), 3),
                "meetings": (meetings, 1), "read-photo": (lambda u, p, *ws: read_photo(u, p, list(ws)), 3)}
    if len(argv) < 2 or argv[1] not in commands or len(argv) - 2 < commands[argv[1]][1]:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        commands[argv[1]][0](*argv[2:])
    except (Failed, OSError, ValueError, KeyError) as e:
        print(f"SMOKE TEST FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
