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


def _linux(monkeypatch, tmp_path, which=None):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ollama_setup.shutil, "which", lambda name: which)
    monkeypatch.setattr(ollama_setup, "LINUX_BINARIES", [tmp_path / "usr-local-bin-ollama"])
    monkeypatch.setattr(ollama_setup, "LINUX_SERVICE_FILES", [tmp_path / "ollama.service"])


def test_linux_shows_the_install_command_and_never_installs_itself(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    s = ollama_setup.status({"ollama_url": "http://127.0.0.1:9"})
    assert s["can_install"] is False  # installing needs root: Trailmix doesn't run it
    assert s["installed"] is False and s["reachable"] is False
    assert s["install_command"] == "curl -fsSL https://ollama.com/install.sh | sh"
    assert s["install_page"].startswith("https://ollama.com/")
    assert s["start_command"] is None


def test_linux_once_installed_says_how_to_start_it(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    (tmp_path / "usr-local-bin-ollama").write_bytes(b"")
    s = ollama_setup.status({"ollama_url": "http://127.0.0.1:9"})
    assert s["installed"] is True and s["install_command"] is None
    assert s["start_command"] == "ollama serve"  # no system service: run it yourself
    (tmp_path / "ollama.service").write_text("[Service]\n")
    assert ollama_setup.status({"ollama_url": "http://127.0.0.1:9"})["start_command"] == "sudo systemctl start ollama"


def test_linux_finds_ollama_on_the_path(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path, which="/opt/ollama/bin/ollama")
    assert str(ollama_setup.app_path()) == "/opt/ollama/bin/ollama"


def test_linux_notices_when_ollama_starts_answering(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    monkeypatch.setattr(ollama_setup.llm_engine, "_ollama_tags", lambda cfg: [])
    answering = []
    monkeypatch.setattr(ollama_setup, "_reachable", lambda cfg: bool(answering))
    assert ollama_setup.status({"ollama_url": "http://127.0.0.1:11434"})["reachable"] is False
    answering.append(True)  # you ran the command in a terminal: the page's next look (every few seconds) sees it
    assert ollama_setup.status({"ollama_url": "http://127.0.0.1:11434"})["reachable"] is True


def test_other_platforms_have_no_linux_commands(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    s = ollama_setup.status({"ollama_url": "http://127.0.0.1:9"})
    assert s["install_command"] is None and s["start_command"] is None and s["install_page"] is None
