"""platform_words.py: what the server calls the computer it runs on, and where those words show up."""
import pytest

import database as db
import feedback
import platform_words
import preflight
import settings


@pytest.mark.parametrize("plat, os, this, recorder", [
    ("darwin", "macos", "this Mac", "menu bar recorder"),
    ("win32", "windows", "this PC", "tray recorder"),
    ("cygwin", "windows", "this PC", "tray recorder"),
    ("linux", "linux", "this computer", "tray recorder"),
    ("freebsd14", "unknown", "this computer", "tray recorder"),
])
def test_words_for_each_platform(plat, os, this, recorder):
    w = platform_words.words(plat)
    assert platform_words.os_id(plat) == os and w["os"] == os
    assert w["this_computer"] == platform_words.this_computer(plat) == this
    assert w["recorder_name"] == recorder
    assert platform_words.cap(this) == this[0].upper() + this[1:]


def test_mac_wording_is_unchanged_on_a_mac(monkeypatch):
    monkeypatch.setattr(platform_words.sys, "platform", "darwin")
    w = platform_words.words()
    assert (w["os_name"], w["computer"], w["your_computer"]) == ("macOS", "Mac", "your Mac")
    assert preflight.transcribe_models("local", {})[0]["label"] == "Whisper, on this Mac"


def test_server_strings_follow_the_os(monkeypatch):
    monkeypatch.setattr(platform_words.sys, "platform", "win32")
    assert preflight.transcribe_models("local", {})[0]["label"] == "Whisper, on this PC"
    monkeypatch.setattr(platform_words.sys, "platform", "linux")
    assert preflight.transcribe_models("local", {})[0]["label"] == "Whisper, on this computer"


def test_diagnostics_labels_are_not_mistaken_for_speakers():
    # "PC: Intel Core i7, 16 GB memory…" in a report's diagnostics is the app's own line, like "Mac: Apple M4…",
    # not someone talking.
    line = "Intel Core i7, 16 GB memory, 200 GB free"
    for label in ("macOS", "Mac", "Windows", "PC", "Linux", "Computer"):
        assert label in platform_words.ALL_LABELS
        assert feedback.scrub(f"{label}: {line}", [])[0] == f"{label}: {line}"
    assert feedback.scrub(f"Dana: {line}", [])[0] == "[meeting content]"  # while a person saying it is hidden


def test_diagnostics_name_the_os(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "data" / "audio")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "t.db")
    db.init_db()
    monkeypatch.setattr(platform_words.sys, "platform", "win32")
    diag = feedback.diagnostics({**settings.DEFAULTS, "summary_provider": "gemini"}, None)
    assert "Windows" in diag and "PC" in diag and "macOS" not in diag and "Mac" not in diag
