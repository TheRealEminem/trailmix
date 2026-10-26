"""The server log (logfile.py): what's left out of it, and that output follows it through a rotation."""
import logging
import os
import subprocess
import sys
from pathlib import Path

import logfile


def _access(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:1", method, path, "1.1", status), None)


def test_polling_is_left_out_but_changes_and_failures_are_kept():
    keep = logfile.NotPolling().filter
    assert not keep(_access("GET", "/api/live", 200))
    assert not keep(_access("GET", "/api/meetings/3?x=1", 304))
    assert not keep(_access("POST", "/api/recorder/check-in", 200))
    assert keep(_access("POST", "/api/meetings/3/summarize", 202))
    assert keep(_access("DELETE", "/api/meetings/3", 204))
    assert keep(_access("GET", "/api/meetings/999", 404))
    assert keep(_access("GET", "/api/health", 500))
    # Not an access line at all (another logger, or a different message): kept.
    assert keep(logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Started server process [%d]", (1,), None))


def test_rotation_keeps_the_newest_and_a_few_old_logs(tmp_path: Path):
    handler = logfile.LogFile(tmp_path / "server.log", max_bytes=200, keep=2)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("test.logfile.rotation")
    log.propagate = False
    log.addHandler(handler)
    for i in range(40):
        log.warning("line %02d %s", i, "x" * 40)
    handler.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["server.log", "server.log.1", "server.log.2"]
    assert "line 39" in (tmp_path / "server.log").read_text()


SCRIPT = """
import logging, os, sys
sys.path.insert(0, {backend!r})
import logfile
h = logfile.LogFile({path!r}, max_bytes=120, keep=2)
h.setFormatter(logging.Formatter("%(message)s"))
assert h.follows_output, "stderr should have been recognised as the log file"
log = logging.getLogger("t"); log.propagate = False; log.addHandler(h)
print("printed before", file=sys.stderr, flush=True)
for i in range(6):
    log.warning("logged %d %s", i, "y" * 40)   # rotates at least once
print("printed after", file=sys.stderr, flush=True)
h.close()
"""


def test_output_sent_to_the_log_by_the_launcher_follows_a_rotation(tmp_path: Path):
    """The launcher points stdout/stderr at server.log; after a rotation they must land in the new file, not the
    renamed old one."""
    path = tmp_path / "server.log"
    with open(path, "ab") as out:  # as the launchers do: >> server.log 2>&1
        subprocess.run([sys.executable, "-c", SCRIPT.format(backend=str(Path(logfile.__file__).parent), path=str(path))],
                       stdout=out, stderr=out, check=True, env={**os.environ, "TRAILMIX_DATA_DIR": str(tmp_path)})
    current = path.read_text()
    assert "printed after" in current and "logged 5" in current
    assert "printed before" not in current  # that went into the log as it was then, since rotated
    assert any("printed before" in p.read_text() for p in tmp_path.glob("server.log.*"))
