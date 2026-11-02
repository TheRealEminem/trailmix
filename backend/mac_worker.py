"""The transcription worker under its old name, from when it only ran on a Mac: `uvicorn mac_worker:app` and
`python -m mac_worker` still work. It's transcription_worker.py now, and runs on a Mac, a PC or Linux."""
import sys

from transcription_worker import ALIASES, app, main  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
