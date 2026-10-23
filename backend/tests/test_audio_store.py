import os
import subprocess
import time

import pytest

import audio_store as store
import database as db


@pytest.fixture
def meeting(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    mid = db.create_meeting("Sync", title_auto=False)
    folder = db.AUDIO_DIR / str(mid)
    folder.mkdir(parents=True)
    for name, hz in (("mic", 300), ("system", 900)):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration=2",
                        "-c:a", "libopus", str(folder / f"{name}.ogg")], check=True)
    db.update_meeting(mid, audio_dir=str(folder), audio_deleted=0)
    return db.get_meeting(mid)


def age(path, days):
    then = time.time() - days * 86400
    os.utime(path, (then, then))


def test_a_playback_mix_nobody_played_lately_is_deleted_and_rebuilt_when_played_again(meeting):
    folder = store.track_dir(meeting)
    mix = store.playback_path(meeting)
    assert mix == folder / "mix.ogg" and mix.exists()
    age(mix, store.PLAYBACK_COPY_DAYS + 1)
    assert store.prune_playback_copies() > 0 and not mix.exists()
    assert (folder / "mic.ogg").exists() and (folder / "system.ogg").exists()  # the recording itself is untouched
    assert store.playback_path(meeting) == mix and mix.exists()                 # rebuilt on demand


def test_a_recently_played_mix_is_kept_and_playing_it_again_extends_that(meeting):
    mix = store.playback_path(meeting)
    age(mix, store.PLAYBACK_COPY_DAYS - 1)
    assert store.prune_playback_copies() == 0 and mix.exists()
    store.playback_path(meeting)  # played again
    assert time.time() - mix.stat().st_mtime < 60
