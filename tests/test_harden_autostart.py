"""H7: `schtasks /Query` results are classified. 'absent' needs exit 1 AND a recognisable not-found signal; every other
failure is 'unknown' (status says so with the raw first line; remove() still tries the delete and reports what really
happened). subprocess is mocked; the facts of the real command were measured on an en-US Windows 11 (not elevated): a
non-existent task gives exit 1 with 'ERROR: The system cannot find the file specified.', an existing task gives 0."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from schulsync import autostart

NOT_FOUND_EN = b"ERROR: The system cannot find the file specified."
NOT_FOUND_DE = "FEHLER: Das System kann die angegebene Datei nicht finden.".encode("cp850")
NOT_FOUND_HRESULT = b"ERROR: The task could not be opened (0x80070002)."
DENIED_EN = b"ERROR: Access is denied."
DENIED_DE = "FEHLER: Zugriff verweigert.".encode("cp850")


class Sys:
    """schtasks stand-in whose /Query and /Delete answers are scripted: (returncode, stdout bytes) or an exception."""

    def __init__(self):
        self.query: tuple[int, bytes] | Exception = (1, NOT_FOUND_EN)
        self.delete: tuple[int, bytes] | Exception = (1, NOT_FOUND_EN)
        self.calls: list[str] = []

    def __call__(self, args, **kw):
        args = [str(a) for a in args]
        assert not kw.get("shell") and kw.get("timeout")
        op = args[1].lower()
        self.calls.append(op)
        result = {"/query": self.query, "/delete": self.delete}[op]
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(args, result[0], stdout=result[1], stderr=b"")


@pytest.fixture
def system(tmp_path, monkeypatch):
    s = Sys()
    monkeypatch.setattr(autostart.subprocess, "run", s)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(autostart, "_oem_codepage", lambda: 850)
    return s


@pytest.fixture
def lnk(tmp_path) -> Path:
    return tmp_path / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SchulSync.lnk"


# --------------------------------------------------------------------------- classification


@pytest.mark.parametrize("text", [NOT_FOUND_EN, NOT_FOUND_DE, NOT_FOUND_HRESULT])
def test_exit_1_with_a_not_found_signal_is_conclusively_absent(system, text):
    system.query = (1, text)
    assert autostart._task_state(autostart._query_task()) == "absent"


@pytest.mark.parametrize(
    "answer",
    [
        (1, DENIED_EN),  # access denied: the task may well exist
        (1, DENIED_DE),
        (1, b""),  # exit 1 without any message: nothing recognisable
        (1, "FEHLER: Irgendetwas anderes ging schief.".encode("cp850")),
        (2, NOT_FOUND_EN),  # the signal alone is not enough: the exit code must be 1
        (5, NOT_FOUND_DE),
        (-1, b""),
    ],
)
def test_every_other_failure_is_unknown(system, answer):
    system.query = answer
    assert autostart._task_state(autostart._query_task()) == "unknown"


def test_a_scheduler_that_cannot_be_started_is_unknown(system):
    system.query = FileNotFoundError(2, "schtasks.exe fehlt")
    assert autostart._task_state(autostart._query_task()) == "unknown"


def test_exit_0_is_present(system):
    system.query = (0, b"SchulSync  Bereit")
    assert autostart._task_state(autostart._query_task()) == "present"


def test_a_timeout_is_unknown(system):
    system.query = subprocess.TimeoutExpired("schtasks", 30)
    assert autostart._task_state(autostart._query_task()) == "unknown"


# --------------------------------------------------------------------------- status


def test_status_says_unknown_with_the_raw_first_line_when_access_is_denied(system, lnk):
    system.query = (1, DENIED_EN + b"\r\nzweite Zeile")
    st = autostart.status()
    assert st.method is None and st.ok is False
    assert "unbekannt" in st.details and "Access is denied" in st.details and "zweite Zeile" not in st.details
    assert "nicht eingerichtet" not in st.details


def test_status_says_unknown_for_a_localised_denial_too(system, lnk):
    system.query = (1, DENIED_DE)
    st = autostart.status()
    assert "unbekannt" in st.details and "Zugriff verweigert" in st.details and "Exit-Code 1" in st.details


def test_status_not_installed_needs_the_not_found_signal(system, lnk):
    system.query = (1, NOT_FOUND_DE)
    st = autostart.status()
    assert st.method is None and not st.ok and "nicht eingerichtet" in st.details and "unbekannt" not in st.details


def test_status_with_a_shortcut_but_an_unreadable_scheduler_warns_about_a_possible_task(system, lnk):
    lnk.parent.mkdir(parents=True)
    lnk.write_bytes(b"lnk")
    system.query = (1, DENIED_EN)
    st = autostart.status()
    assert st.method == "shortcut" and st.ok
    assert "Access is denied" in st.details and "Aufgabe" in st.details  # a task may exist next to the shortcut


# --------------------------------------------------------------------------- remove


def test_remove_does_not_claim_nothing_to_remove_when_the_query_was_denied_but_the_delete_works(system, lnk):
    system.query = (1, DENIED_EN)
    system.delete = (0, b"SUCCESS: The scheduled task was successfully deleted.")
    result = autostart.remove()
    assert result.ok and "Nichts zu entfernen" not in result.details
    assert "Aufgabe" in result.details and system.calls == ["/query", "/delete"]


def test_remove_reports_what_really_happened_when_both_query_and_delete_are_denied(system, lnk):
    system.query = (1, DENIED_EN)
    system.delete = (1, DENIED_EN)
    result = autostart.remove()
    assert not result.ok
    assert "Nichts zu entfernen" not in result.details and "Access is denied" in result.details
    assert "konnte nicht entfernt werden" in result.details


def test_remove_treats_a_delete_that_finds_no_task_as_nothing_to_remove(system, lnk):
    system.query = (1, DENIED_EN)  # unknown ...
    system.delete = (1, NOT_FOUND_DE)  # ... but the delete itself proves there is none
    result = autostart.remove()
    assert result.ok and "Nichts zu entfernen" in result.details


def test_remove_with_a_conclusive_absent_does_not_call_delete(system, lnk):
    system.query = (1, NOT_FOUND_EN)
    result = autostart.remove()
    assert result.ok and "Nichts zu entfernen" in result.details and system.calls == ["/query"]


def test_remove_with_an_unstartable_scheduler_still_removes_the_shortcut_and_reports_the_error(system, lnk):
    lnk.parent.mkdir(parents=True)
    lnk.write_bytes(b"lnk")
    system.query = subprocess.TimeoutExpired("schtasks", 30)
    system.delete = subprocess.TimeoutExpired("schtasks", 30)
    result = autostart.remove()
    assert not result.ok and not lnk.exists() and "Verknüpfung" in result.details and "Aufgabe" in result.details


def test_remove_present_task_is_deleted_as_before(system, lnk):
    system.query = (0, b"SchulSync Bereit")
    system.delete = (0, b"ERFOLGREICH")
    result = autostart.remove()
    assert result.ok and "Aufgabenplanung" in result.details and system.calls == ["/query", "/delete"]
