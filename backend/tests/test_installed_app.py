from pathlib import Path

import main

BACKEND = Path(main.__file__).resolve().parent


def test_the_code_folder_is_not_the_installed_app(monkeypatch):
    monkeypatch.setattr(main.sys, "executable", "/home/dana/Code/Trailmix/.venv/bin/python3")
    assert not main._installed_app()


def test_the_mac_app(monkeypatch):
    monkeypatch.setattr(main.sys, "executable", "/Applications/Trailmix.app/Contents/Resources/python/bin/python3")
    assert main._installed_app()


def test_the_windows_and_linux_apps(monkeypatch):
    # Their Python sits in a python folder beside the backend one (the "DEV" badge showed on every install: #14)
    for python in ("python/bin/python3", "python/python.exe"):
        monkeypatch.setattr(main.sys, "executable", str(BACKEND.parent / python))
        assert main._installed_app()
