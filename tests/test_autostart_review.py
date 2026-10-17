"""Adversarial review tests for autostart.py (each one demonstrates a real defect; all are expected to FAIL today)."""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from schulsync import autostart
from schulsync.autostart import build_task_xml
from schulsync.config import Paths

USER = "PC\\max"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class Sys:
    """Minimal schtasks/powershell stand-in."""

    def __init__(self):
        self.task_present = False
        self.query_raises: Exception | None = None
        self.xml_bytes: bytes | None = None

    def __call__(self, args, **kw):
        args = [str(a) for a in args]
        exe = Path(args[0]).name.lower()
        if exe == "powershell.exe":
            Path(kw["env"]["SCHULSYNC_LNK"]).write_bytes(b"lnk")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        op = args[1].lower()
        if op == "/create":
            self.xml_bytes = Path(args[args.index("/XML") + 1]).read_bytes()
            self.task_present = True
            return subprocess.CompletedProcess(args, 0, b"OK", b"")
        if op == "/query":
            if self.query_raises:
                raise self.query_raises
            return subprocess.CompletedProcess(args, 0 if self.task_present else 1, b"", b"")
        if op == "/delete":
            self.task_present = False
            return subprocess.CompletedProcess(args, 0, b"", b"")
        raise AssertionError(args)


@pytest.fixture
def system(tmp_path, monkeypatch):
    s = Sys()
    monkeypatch.setattr(autostart.subprocess, "run", s)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(autostart, "current_user", lambda: USER)
    monkeypatch.setattr(autostart, "default_command", lambda: (r"C:\P\pythonw.exe", "-m schulsync run --auto"))
    monkeypatch.setattr(autostart, "_oem_codepage", lambda: 850)
    return s


def test_status_must_not_claim_not_installed_when_schtasks_cannot_be_queried(system):
    # Query timed out: we do NOT know whether the task exists. status() maps returncode None to "not installed"
    # (remove() reports the same situation as an error, status() as a plain negative).
    system.query_raises = subprocess.TimeoutExpired("schtasks", 30)
    st = autostart.status()
    assert "nicht eingerichtet" not in st.details, st.details


def test_build_task_xml_rejects_empty_command():
    # Same reasoning as for the empty user: an empty <Command/> yields a task that registers but can never run.
    with pytest.raises(ValueError):
        build_task_xml(user_sid_or_name=USER, command="", arguments="-m schulsync run --auto", working_dir=r"C:\D")


def test_install_falls_back_when_temp_file_cannot_be_created(system, paths, monkeypatch):
    # install() docstring: "bei jedem Fehlschlag die Startup-Verknüpfung". An unwritable %TEMP% raises instead.
    def boom(*a, **k):
        raise PermissionError(13, "TEMP nicht beschreibbar")

    monkeypatch.setattr(autostart.tempfile, "mkstemp", boom)
    result = autostart.install(paths)
    assert result.method == "shortcut" and result.ok


def test_install_with_custom_home_must_carry_or_flag_the_data_directory(system, tmp_path, monkeypatch):
    # Paths.default() honours SCHULSYNC_HOME, but the logon task starts `pythonw -m schulsync run --auto` without it.
    # The task would therefore run against %LOCALAPPDATA%\SchulSync, not against the home the user installed for.
    monkeypatch.delenv("SCHULSYNC_HOME", raising=False)
    custom = Paths(tmp_path / "Eigenes Datenverzeichnis")
    result = autostart.install(custom)
    root = ET.fromstring(system.xml_bytes)
    args = root.find("t:Actions/t:Exec/t:Arguments", NS).text
    cmd = root.find("t:Actions/t:Exec/t:Command", NS).text
    assert (str(custom.home) in args) or (str(custom.home) in cmd) or ("SCHULSYNC_HOME" in result.details), (
        "autostart silently ignores the custom data directory"
    )
