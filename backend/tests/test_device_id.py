import hashlib
import sys

import cloud

ID = "4c1b2f0e9a8d47b6b3e5c2d1f0a9b8c7"


def test_linux_uses_the_machine_id_hashed_as_elsewhere(monkeypatch, tmp_path):
    machine_id = tmp_path / "machine-id"
    machine_id.write_text(ID.upper() + "\n")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cloud, "MACHINE_ID_FILES", (str(machine_id),))
    assert cloud.linux_machine_id() == ID
    assert cloud.device_id() == hashlib.sha256(f"trailmix-device:{ID}".encode()).hexdigest()
    assert cloud.device_id() == cloud.device_id()


def test_the_dbus_copy_is_used_when_systemd_has_none(monkeypatch, tmp_path):
    dbus = tmp_path / "dbus-machine-id"
    dbus.write_text(ID)
    monkeypatch.setattr(cloud, "MACHINE_ID_FILES", (str(tmp_path / "missing"), str(dbus)))
    assert cloud.linux_machine_id() == ID


def test_an_unset_machine_id_falls_back_to_the_network_address(monkeypatch, tmp_path):
    files = []
    for n, text in enumerate(["", "uninitialized\n", "0" * 32, "not hex at all, but 32 chars ok", ID + "ff"]):
        f = tmp_path / f"id{n}"
        f.write_text(text)
        files.append(str(f))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cloud, "MACHINE_ID_FILES", tuple(files))
    monkeypatch.setattr(cloud.uuid, "getnode", lambda: 0x0242AC110002)
    assert cloud.linux_machine_id() == ""
    assert cloud.device_id() == hashlib.sha256(f"trailmix-device:{0x0242AC110002}".encode()).hexdigest()
