"""On-disk lifecycle of meeting audio.

While recording:  data/audio/<id>/mic.pcm and system.pcm  (raw 16 kHz mono int16, append-only)
After stopping:   each track is compressed to Opus (mic.ogg / system.ogg) and the .pcm deleted
After retention:  the whole folder is deleted (TRAILMIX_AUDIO_RETENTION_DAYS; -1 = keep forever)
"""
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import database as db

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2
RETENTION_DAYS = int(os.getenv("TRAILMIX_AUDIO_RETENTION_DAYS", "30"))
OPUS_BITRATE = os.getenv("TRAILMIX_OPUS_BITRATE", "48k")

TRACKS = {0: "mic", 1: "system"}  # channel id used on the wire -> track name


def track_dir(meeting: dict) -> Path | None:
    return Path(meeting["audio_dir"]) if meeting.get("audio_dir") else None


def pcm_path(folder: Path, track: str) -> Path:
    return folder / f"{track}.pcm"


def ogg_path(folder: Path, track: str) -> Path:
    return folder / f"{track}.ogg"


def duration_of_pcm(folder: Path) -> float:
    sizes = [p.stat().st_size for p in folder.glob("*.pcm")]
    return max(sizes, default=0) / BYTES_PER_SEC


def compress_pending(meeting: dict) -> None:
    """Opus-encode any leftover .pcm tracks, deleting each .pcm only after its .ogg is verified."""
    folder = track_dir(meeting)
    if not folder or not folder.exists():
        return
    for pcm in folder.glob("*.pcm"):
        out = pcm.with_suffix(".ogg")
        if pcm.stat().st_size == 0:
            pcm.unlink()
            continue
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
             "-i", str(pcm), "-c:a", "libopus", "-b:a", OPUS_BITRATE, str(out)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)  # keep the .pcm so nothing is lost; the job reports the error
            raise RuntimeError(f"Could not compress {pcm.name}: {proc.stderr.strip()[:300]}")
        pcm.unlink()


def load_track(folder: Path, track: str) -> np.ndarray | None:
    """Decodes a track to float32 mono 16 kHz in memory (~230 MB per hour). None if absent."""
    ogg, pcm = ogg_path(folder, track), pcm_path(folder, track)
    if pcm.exists():  # not yet compressed
        return np.fromfile(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if not ogg.exists():
        return None
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(ogg), "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Could not decode {ogg.name}: {proc.stderr.decode()[:300]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def audio_bytes(meeting: dict) -> int:
    folder = track_dir(meeting)
    if meeting.get("audio_deleted") or not folder or not folder.exists():
        return 0
    return sum(p.stat().st_size for p in folder.iterdir() if p.is_file())


def delete_audio(meeting: dict) -> None:
    folder = track_dir(meeting)
    if folder and folder.exists():
        shutil.rmtree(folder)
    legacy = meeting.get("audio_path")
    if legacy:
        Path(legacy).unlink(missing_ok=True)
    db.update_meeting(meeting["id"], audio_deleted=1)


def expires_at(meeting: dict) -> str | None:
    if RETENTION_DAYS <= 0 or meeting.get("audio_deleted") or meeting.get("keep_audio"):
        return None
    created = datetime.fromisoformat(meeting["created_at"].replace("Z", "+00:00"))
    return (created + timedelta(days=RETENTION_DAYS)).isoformat()


def sweep_expired() -> int:
    """Deletes audio older than the retention window. Returns how many meetings were cleaned."""
    if RETENTION_DAYS < 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    cleaned = 0
    for m in db.meetings_with_audio():
        if m["status"] not in ("done", "error"):
            continue
        created = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
        if created < cutoff:
            delete_audio(db.get_meeting(m["id"]))
            cleaned += 1
    return cleaned


def playback_path(meeting: dict) -> Path | None:
    """One file with both sides of the conversation, for the in-app player. Built once, then cached.
    None if there's no finished audio (still recording, not yet compressed, or deleted)."""
    folder = track_dir(meeting)
    if meeting.get("audio_deleted") or not folder or not folder.exists() or any(folder.glob("*.pcm")):
        return None
    tracks = [ogg_path(folder, t) for t in TRACKS.values() if ogg_path(folder, t).exists()]
    if not tracks:
        return None
    if len(tracks) == 1:
        return tracks[0]
    mix = folder / "mix.ogg"
    if not mix.exists():
        tmp = folder / "mix.tmp.ogg"
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(tracks[0]), "-i", str(tracks[1]),
             "-filter_complex", "amix=inputs=2:duration=longest:normalize=0", "-c:a", "libopus", "-b:a", OPUS_BITRATE,
             str(tmp)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Could not prepare audio for playback: {proc.stderr.strip()[:300]}")
        tmp.replace(mix)
    return mix
