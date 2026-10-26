"""The server log: logs/server.log in the data folder, with timestamps, rotated at 5 MB (three old ones kept).

Everything the engine says goes there: its own loggers (trailmix.*), uvicorn's, and whatever bypasses logging
(a crash's traceback, a model download's progress from a child process). The launchers (Trailmix.app,
./trailmix, launchd) already point the process's output at that same file; when they do, the output is moved
along at each rotation, so nothing keeps writing into an old copy. Run from a terminal or in Docker, the log
also goes to stderr as before.

Request lines: a successful GET and the menu bar recorder's check-in are polling (several a second while
Trailmix is open, which used to fill the log with nothing else) and are left out; anything that changes
something, or fails, is kept.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import database as db

MAX_BYTES = 5_000_000
KEEP = 3
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def path() -> Path:
    return db.DATA_DIR / "logs" / "server.log"


def _same_file(fd: int, other: int) -> bool:
    try:
        return os.path.sameopenfile(fd, other)
    except OSError:  # fd closed or not a file
        return False


class LogFile(RotatingFileHandler):
    """The rotating log. When stdout/stderr were sent to this file by the launcher, they're pointed at the new
    file after each rotation, so tracebacks and child processes' output stay in the current log."""

    def __init__(self, file: Path, max_bytes: int = MAX_BYTES, keep: int = KEEP):
        super().__init__(file, maxBytes=max_bytes, backupCount=keep, encoding="utf-8")
        self.follows_output = _same_file(2, self.stream.fileno())
        if self.follows_output:
            self._point_output_here()

    def _point_output_here(self) -> None:
        for fd in (1, 2):
            os.dup2(self.stream.fileno(), fd)

    def doRollover(self) -> None:
        super().doRollover()
        if self.follows_output and self.stream:
            self._point_output_here()


class NotPolling(logging.Filter):
    """Drops uvicorn's access lines for polling: successful GETs, and the menu bar recorder's check-in."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _, method, request_path, _, status = record.args  # type: ignore[misc]
        except (TypeError, ValueError):
            return True
        if int(status) >= 400:
            return True
        return not (method == "GET" or request_path == "/api/recorder/check-in")


_configured = False


def configure() -> None:
    """Sets up logging for the server process. Idempotent."""
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(FORMAT, DATE_FORMAT)
    handlers: list[logging.Handler] = []
    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(LogFile(target))
    except OSError as e:
        print(f"Can't write the server log at {target}: {e}", file=sys.stderr)
    if not any(getattr(h, "follows_output", False) for h in handlers):  # a terminal, Docker, or no file
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    # uvicorn gives its loggers handlers of their own (stderr, no timestamps); route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").addFilter(NotPolling())
    # httpx announces every outgoing request (Ollama is asked for its models on each health check); only its
    # problems are worth a line.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
