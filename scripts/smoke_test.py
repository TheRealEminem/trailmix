#!/usr/bin/env python3
"""The release gate's checks that are the same on every computer, run against a Trailmix engine that's already
up. Each platform's own smoke test (scripts/smoke-test.sh on a Mac) installs, updates and starts the app its own
way, and calls these for the rest. Standard library only, so any Python 3.9+ runs it.

  smoke_test.py check-engine URL UI_DIR     it's this app's engine (it serves UI_DIR), and its speech models are in
  smoke_test.py transcribe URL WAV WORD...  WAV imported as a meeting is transcribed, with each WORD in it
  smoke_test.py meetings URL                how many meetings it has

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


def main(argv: list[str]) -> int:
    commands = {"check-engine": (check_engine, 2), "transcribe": (lambda u, w, *ws: transcribe(u, w, list(ws)), 3),
                "meetings": (meetings, 1)}
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
