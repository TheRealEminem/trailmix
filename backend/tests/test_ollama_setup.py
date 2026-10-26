import sys

import ollama_setup


def test_a_windows_download_must_be_validly_signed_by_ollama():
    assert ollama_setup.signature_problem("Valid", "CN=Ollama, O=Ollama, L=Palo Alto, S=California, C=US") is None
    assert "signature check" in ollama_setup.signature_problem("NotSigned", "")
    assert "signature check" in ollama_setup.signature_problem("HashMismatch", "CN=Ollama")
    assert "isn't signed by Ollama" in ollama_setup.signature_problem("Valid", "CN=Someone Else, O=Elsewhere")
    assert "signature check" in ollama_setup.signature_problem("", "")


def test_the_windows_app_is_looked_for_where_its_installer_puts_it(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(ollama_setup.shutil, "which", lambda name: None)
    assert ollama_setup.app_path() is None
    folder = tmp_path / "Programs" / "Ollama"
    folder.mkdir(parents=True)
    (folder / "ollama app.exe").write_bytes(b"")
    assert ollama_setup.app_path() == folder / "ollama app.exe"
    assert ollama_setup.status({"ollama_url": "http://127.0.0.1:9"})["can_install"] is True


def test_the_mac_app_is_looked_for_in_applications(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ollama_setup, "INSTALL_DIRS", [tmp_path])
    assert ollama_setup.app_path() is None
    (tmp_path / "Ollama.app").mkdir()
    assert ollama_setup.app_path() == tmp_path / "Ollama.app"
