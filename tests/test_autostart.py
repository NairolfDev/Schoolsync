"""Tests fuer autostart.py. Alles mit gemocktem subprocess/Dateisystem.

Ehrlichkeit: Diese Tests (ausser dem COM-Test am Ende, der den Parser der Aufgabenplanung selbst benutzt und nichts
registriert) beweisen NICHT, dass Windows die erzeugte Task-XML akzeptiert; und keiner beweist, dass ein Standardbenutzer
sie registrieren darf. Das ist nur durch einen realen Hin-und-Rueck-Test (schtasks /Create, /Query, /Delete auf dem
Zielsystem) pruefbar. Getestet wird hier: XML wohlgeformt und fachlich passend, Ablauf/Fallback/Idempotenz der Logik,
Aufrufdisziplin (kein shell=True, Zeitlimits).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from schulsync import autostart
from schulsync.autostart import AutostartStatus, build_task_xml
from schulsync.config import Paths

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
USER = "SCHUELER-PC\\max.muster"
TRICKY_COMMAND = r"C:\Users\Jörg & Söhne\SchulSync <1>\.venv\Scripts\pythonw.exe"
TRICKY_WORKDIR = r"C:\Users\Jörg & Söhne\AppData\Local\SchulSync"


def parse(xml_text: str) -> ET.Element:
    return ET.fromstring(xml_text.encode("utf-16"))


def text(root: ET.Element, path: str) -> str | None:
    el = root.find(path, NS)
    return None if el is None else el.text


def sample_xml(**over) -> str:
    kw = dict(
        user_sid_or_name=USER,
        command=r"C:\Prog\.venv\Scripts\pythonw.exe",
        arguments="-m schulsync run --auto",
        working_dir=r"C:\Daten\SchulSync",
    )
    kw.update(over)
    return build_task_xml(**kw)


# --------------------------------------------------------------------------- XML


def test_xml_is_well_formed_and_declares_utf16():
    xml = sample_xml()
    assert xml.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    root = parse(xml)
    assert root.tag == "{%s}Task" % NS["t"]
    assert root.get("version") == "1.2"


def test_xml_logon_trigger_for_user_with_delay():
    root = parse(sample_xml())
    trig = root.find("t:Triggers/t:LogonTrigger", NS)
    assert trig is not None
    assert text(root, "t:Triggers/t:LogonTrigger/t:UserId") == USER
    assert text(root, "t:Triggers/t:LogonTrigger/t:Delay") == "PT1M"
    assert text(root, "t:Triggers/t:LogonTrigger/t:Enabled") == "true"
    assert len(root.findall("t:Triggers/*", NS)) == 3  # Anmeldung plus 06:00 und 18:00
    cals = root.findall("t:Triggers/t:CalendarTrigger", NS)
    assert [c.find("t:StartBoundary", NS).text[-8:] for c in cals] == ["06:00:00", "18:00:00"]
    assert all(c.find("t:ScheduleByDay/t:DaysInterval", NS).text == "1" for c in cals)


def test_xml_principal_is_interactive_least_privilege_and_never_admin():
    xml = sample_xml()
    root = parse(xml)
    assert text(root, "t:Principals/t:Principal/t:UserId") == USER
    assert text(root, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert text(root, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert root.find("t:Principals/t:Principal/t:GroupId", NS) is None
    assert "HighestAvailable" not in xml
    assert "<Password" not in xml


def test_xml_settings():
    root = parse(sample_xml())
    s = "t:Settings/t:"
    assert text(root, s + "MultipleInstancesPolicy") == "IgnoreNew"
    assert text(root, s + "DisallowStartIfOnBatteries") == "false"
    assert text(root, s + "StopIfGoingOnBatteries") == "false"
    assert text(root, s + "StartWhenAvailable") == "true"
    assert text(root, s + "RunOnlyIfNetworkAvailable") == "false"  # das Programm wartet selbst auf das Netz
    assert text(root, s + "ExecutionTimeLimit") == "PT1H"
    assert text(root, s + "Hidden") == "false"
    assert text(root, s + "Enabled") == "true"
    assert text(root, s + "WakeToRun") == "false"


def test_xml_action_fields():
    root = parse(sample_xml())
    assert text(root, "t:Actions/t:Exec/t:Command") == r"C:\Prog\.venv\Scripts\pythonw.exe"
    assert text(root, "t:Actions/t:Exec/t:Arguments") == "-m schulsync run --auto"
    assert text(root, "t:Actions/t:Exec/t:WorkingDirectory") == r"C:\Daten\SchulSync"
    assert root.find("t:Actions", NS).get("Context") == "Author"
    assert root.find("t:Principals/t:Principal", NS).get("id") == "Author"


def test_xml_escapes_ampersand_umlaut_angle_brackets_and_quotes():
    user = 'PC\\Ben "B" <x> & Co'
    xml = sample_xml(
        user_sid_or_name=user,
        command=TRICKY_COMMAND,
        arguments='-m schulsync run --auto --note "a & b"',
        working_dir=TRICKY_WORKDIR,
    )
    root = parse(xml)  # wohlgeformt trotz Sonderzeichen
    assert text(root, "t:Actions/t:Exec/t:Command") == TRICKY_COMMAND
    assert text(root, "t:Actions/t:Exec/t:WorkingDirectory") == TRICKY_WORKDIR
    assert text(root, "t:Actions/t:Exec/t:Arguments") == '-m schulsync run --auto --note "a & b"'
    assert text(root, "t:Triggers/t:LogonTrigger/t:UserId") == user
    assert "& Söhne" not in xml and "&amp; Söhne" in xml  # rohes & kommt nicht vor


def test_xml_paths_with_spaces_are_not_wrapped_in_quotes():
    # Befund: von Windows selbst exportierte Aufgaben (z. B. unter C:\Program Files) tragen den Pfad ohne Anfuehrungszeichen.
    root = parse(sample_xml(command=r"C:\Program Files\Python313\pythonw.exe", working_dir=r"C:\Mein Ordner\SchulSync"))
    assert text(root, "t:Actions/t:Exec/t:Command") == r"C:\Program Files\Python313\pythonw.exe"
    assert text(root, "t:Actions/t:Exec/t:WorkingDirectory") == r"C:\Mein Ordner\SchulSync"


def test_xml_custom_delay_and_time_limit_and_optional_fields_omitted():
    root = parse(sample_xml(delay="PT90S", time_limit="PT30M", arguments="", working_dir=""))
    assert text(root, "t:Triggers/t:LogonTrigger/t:Delay") == "PT90S"
    assert text(root, "t:Settings/t:ExecutionTimeLimit") == "PT30M"
    assert root.find("t:Actions/t:Exec/t:Arguments", NS) is None
    assert root.find("t:Actions/t:Exec/t:WorkingDirectory", NS) is None


@pytest.mark.parametrize("bad", ["1 minute", "", "PT", "P", "PT1M<x>", "1M", "PTM"])
def test_xml_rejects_malformed_durations(bad):
    with pytest.raises(ValueError):
        sample_xml(delay=bad)
    with pytest.raises(ValueError):
        sample_xml(time_limit=bad)


@pytest.mark.parametrize("good", ["PT1M", "PT30S", "PT1H30M", "P1D", "P1DT2H"])
def test_xml_accepts_iso_durations(good):
    assert text(parse(sample_xml(delay=good)), "t:Triggers/t:LogonTrigger/t:Delay") == good


def test_xml_has_german_description():
    desc = text(parse(sample_xml()), "t:RegistrationInfo/t:Description")
    assert desc and "SchulSync" in desc and "Anmeldung" in desc
    assert text(parse(sample_xml()), "t:RegistrationInfo/t:Author") == USER


# --------------------------------------------------------------------------- default_command


def test_default_command_prefers_pythonw_next_to_interpreter(tmp_path, monkeypatch):
    d = tmp_path / "Mein Ordner" / "Scripts"
    d.mkdir(parents=True)
    (d / "python.exe").write_bytes(b"")
    (d / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(d / "python.exe"))
    command, arguments = autostart.default_command()
    assert command == str(d / "pythonw.exe")  # Pfad mit Leerzeichen bleibt unveraendert (kein Quoting im XML/COM)
    assert arguments == "-m schulsync run --auto"


def test_default_command_falls_back_to_python_exe_without_pythonw(tmp_path, monkeypatch):
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    command, arguments = autostart.default_command()
    assert command == str(tmp_path / "python.exe")
    assert arguments == "-m schulsync run --auto"


def test_default_command_when_interpreter_already_is_pythonw(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert autostart.default_command()[0] == str(tmp_path / "pythonw.exe")


# --------------------------------------------------------------------------- Fake-System


class FakeSystem:
    """Ersatz fuer subprocess.run: emuliert schtasks.exe und powershell.exe, prueft die Aufrufdisziplin."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.task_present = False
        self.create_result: tuple[int, bytes] | Exception = (0, b"ERFOLGREICH: Aufgabe wurde erstellt.")
        self.query_after_create_fails = False
        self.delete_rc = 0
        self.shortcut_rc = 0
        self.shortcut_creates_file = True
        self.xml_bytes: bytes | None = None
        self.xml_path: Path | None = None
        self.shortcut_env: dict[str, str] = {}

    def __call__(self, args, **kw):
        args = [str(a) for a in args]
        self.calls.append((args, kw))
        # Aufrufdisziplin gilt fuer JEDEN Aufruf
        assert not kw.get("shell"), "shell=True ist verboten"
        assert isinstance(args, list)
        assert isinstance(kw.get("timeout"), (int, float)) and kw["timeout"] > 0, "Zeitlimit fehlt"
        exe = Path(args[0]).name.lower()
        if exe == "schtasks.exe":
            return self._schtasks(args)
        if exe == "powershell.exe":
            return self._powershell(args, kw)
        raise AssertionError(f"unerwarteter Aufruf: {args}")

    def _done(self, args, rc, out=b""):
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr=b"")

    def _schtasks(self, args):
        op = args[1].lower()
        assert args[args.index("/TN") + 1] == "SchulSync"
        if op == "/create":
            self.xml_path = Path(args[args.index("/XML") + 1])
            self.xml_bytes = self.xml_path.read_bytes()
            if isinstance(self.create_result, Exception):
                raise self.create_result
            rc, out = self.create_result
            if rc == 0:
                self.task_present = True
            return self._done(args, rc, out)
        if op == "/query":
            if self.task_present and not self.query_after_create_fails:
                return self._done(args, 0, b"SchulSync  Bereit")
            return self._done(args, 1, b"FEHLER: Das System kann die angegebene Datei nicht finden.")
        if op == "/delete":
            if self.delete_rc == 0:
                self.task_present = False
            return self._done(args, self.delete_rc, b"ERFOLGREICH" if self.delete_rc == 0 else b"FEHLER: Zugriff verweigert.")
        raise AssertionError(args)

    def _powershell(self, args, kw):
        assert "-NoProfile" in args and "-NonInteractive" in args
        env = kw.get("env") or {}
        self.shortcut_env = {k: v for k, v in env.items() if k.startswith("SCHULSYNC_")}
        if self.shortcut_rc == 0 and self.shortcut_creates_file:
            Path(env["SCHULSYNC_LNK"]).write_bytes(b"lnk")
        return self._done(args, self.shortcut_rc, b"" if self.shortcut_rc == 0 else b"Fehler beim Speichern")

    def ops(self, exe: str = "schtasks.exe") -> list[str]:
        return [a[1].lower() for a, _ in self.calls if Path(a[0]).name.lower() == exe]


@pytest.fixture
def system(tmp_path, monkeypatch):
    fake = FakeSystem()
    monkeypatch.setattr(autostart.subprocess, "run", fake)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(autostart, "current_user", lambda: USER)
    monkeypatch.setattr(autostart, "default_command", lambda: (TRICKY_COMMAND, "-m schulsync run --auto"))
    monkeypatch.setattr(autostart, "_oem_codepage", lambda: 850)
    return fake


@pytest.fixture
def lnk(tmp_path) -> Path:
    return tmp_path / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SchulSync.lnk"


def make_lnk(lnk: Path) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    lnk.write_bytes(b"lnk")


# --------------------------------------------------------------------------- install: Aufgabenplanung


def test_install_via_task_scheduler(system, paths, lnk):
    result = autostart.install(paths)
    assert isinstance(result, AutostartStatus)
    assert result.method == "task" and result.ok
    assert "Aufgabenplanung" in result.details
    assert system.ops() == ["/create", "/query"]  # anlegen, dann verifizieren
    create_args = system.calls[0][0]
    assert create_args[create_args.index("/TN") + 1] == "SchulSync"
    assert "/F" in create_args and "/XML" in create_args
    assert "/RU" not in [a.upper() for a in create_args]  # keine fremden Konten, kein Passwort
    assert "/RL" not in [a.upper() for a in create_args]
    assert not lnk.exists()


def test_install_writes_utf16_xml_with_bom_and_removes_temp_file(system, paths):
    autostart.install(paths)
    assert system.xml_bytes[:2] in (b"\xff\xfe", b"\xfe\xff")
    root = ET.fromstring(system.xml_bytes)
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == USER
    assert root.find("t:Actions/t:Exec/t:Command", NS).text == TRICKY_COMMAND
    assert root.find("t:Actions/t:Exec/t:WorkingDirectory", NS).text == str(paths.home)
    assert system.xml_path is not None and not system.xml_path.exists()  # Temp-Datei aufgeraeumt


def test_install_removes_stale_fallback_shortcut_when_task_succeeds(system, paths, lnk):
    make_lnk(lnk)
    result = autostart.install(paths)
    assert result.method == "task"
    assert not lnk.exists()  # sonst wuerde das Programm doppelt gestartet


# --------------------------------------------------------------------------- install: Ausweichloesung


def test_install_falls_back_to_shortcut_on_access_denied(system, paths, lnk):
    system.create_result = (1, "FEHLER: Zugriff verweigert.".encode("cp850"))
    result = autostart.install(paths)
    assert result.method == "shortcut" and result.ok
    assert lnk.exists()
    assert "Zugriff verweigert" in result.details
    assert "Administrator" in result.details
    assert "Verzögerung" in result.details  # Hinweis: Verknuepfung kann nicht verzoegern
    assert "Startup" in result.details
    assert not system.task_present


def test_install_shortcut_receives_values_via_environment_not_script_text(system, paths, lnk):
    system.create_result = (1, b"ERROR: Access is denied.")
    autostart.install(paths)
    env = system.shortcut_env
    assert env["SCHULSYNC_LNK"] == str(lnk)
    assert env["SCHULSYNC_TARGET"] == TRICKY_COMMAND
    assert env["SCHULSYNC_ARGS"] == f"-m schulsync run --auto --home {paths.home}"  # `paths` ist nicht der Standardordner
    assert env["SCHULSYNC_WORKDIR"] == str(paths.home)
    ps_args = [a for a, _ in system.calls if Path(a[0]).name.lower() == "powershell.exe"][0]
    assert TRICKY_COMMAND not in " ".join(ps_args)  # keine Werte im Skripttext (keine Quoting-/Injection-Probleme)
    assert "-ExecutionPolicy" not in ps_args


@pytest.mark.parametrize(
    "create_result, expect",
    [
        ((1, b"ERROR: Access is denied."), "Zugriff verweigert"),
        ((1, b"FEHLER: Zugriff verweigert."), "Zugriff verweigert"),
        ((1, b"Fehler 0x80070005"), "Zugriff verweigert"),
        ((1, "FEHLER: Die Aufgaben-XML enthält einen ungültigen Wert.".encode("cp850")), "ungültigen Wert"),
        ((5, b""), "abgelehnt"),
        (subprocess.TimeoutExpired("schtasks", 30), "Zeitüberschreitung"),
        (FileNotFoundError(2, "Das System kann die angegebene Datei nicht finden"), "nicht gestartet"),
    ],
)
def test_install_falls_back_on_any_task_failure(system, paths, lnk, create_result, expect):
    system.create_result = create_result
    result = autostart.install(paths)
    assert result.method == "shortcut" and result.ok and lnk.exists()
    assert expect in result.details


def test_install_falls_back_when_task_not_found_after_create(system, paths, lnk):
    system.query_after_create_fails = True
    result = autostart.install(paths)
    assert result.method == "shortcut" and lnk.exists()
    assert "/delete" in system.ops()  # halbfertige Aufgabe wird aufgeraeumt (kein Doppelstart)
    assert "nicht auffindbar" in result.details


def test_install_reports_failure_with_manual_steps_when_both_routes_fail(system, paths, lnk):
    system.create_result = (1, b"ERROR: Access is denied.")
    system.shortcut_rc = 1
    result = autostart.install(paths)
    assert result.method is None and not result.ok
    assert not lnk.exists()
    assert "shell:startup" in result.details  # dokumentierter manueller Weg
    assert "-m schulsync run --auto" in result.details
    assert "Zugriff verweigert" in result.details


def test_install_fails_when_powershell_reports_success_but_no_file(system, paths, lnk):
    system.create_result = (1, b"ERROR: Access is denied.")
    system.shortcut_creates_file = False
    result = autostart.install(paths)
    assert result.method is None and not result.ok


def test_install_shortcut_creates_missing_startup_folder(system, paths, lnk):
    system.create_result = (1, b"ERROR: Access is denied.")
    assert not lnk.parent.exists()
    assert autostart.install(paths).ok and lnk.exists()


# --------------------------------------------------------------------------- remove


def test_remove_is_idempotent_when_nothing_exists(system, lnk):
    for _ in range(2):
        result = autostart.remove()
        assert result.ok and result.method is None
        assert "Nichts zu entfernen" in result.details
    assert "/delete" not in system.ops()


def test_remove_deletes_task(system):
    system.task_present = True
    result = autostart.remove()
    assert result.ok and "Aufgabenplanung" in result.details
    d = [a for a, _ in system.calls if len(a) > 1 and a[1].lower() == "/delete"][0]
    assert d[d.index("/TN") + 1] == "SchulSync" and "/F" in d
    assert not system.task_present
    assert autostart.remove().ok  # zweites Mal: nichts mehr da, kein Fehler


def test_remove_deletes_shortcut(system, lnk):
    make_lnk(lnk)
    result = autostart.remove()
    assert result.ok and "Verknüpfung" in result.details
    assert not lnk.exists()
    assert autostart.remove().ok


def test_remove_deletes_both_artifacts(system, lnk):
    system.task_present = True
    make_lnk(lnk)
    result = autostart.remove()
    assert result.ok and not system.task_present and not lnk.exists()
    assert "Aufgabenplanung" in result.details and "Verknüpfung" in result.details


def test_remove_reports_failure_but_still_removes_shortcut(system, lnk):
    system.task_present = True
    system.delete_rc = 1
    make_lnk(lnk)
    result = autostart.remove()
    assert not result.ok
    assert not lnk.exists()
    assert "Zugriff verweigert" in result.details


def test_remove_does_not_raise_when_schtasks_cannot_run(system, lnk, monkeypatch):
    def boom(args, **kw):
        raise FileNotFoundError(2, "nicht gefunden")

    monkeypatch.setattr(autostart.subprocess, "run", boom)
    make_lnk(lnk)
    result = autostart.remove()  # darf nie werfen
    assert not result.ok and not lnk.exists()


def test_remove_handles_unlink_error(system, lnk, monkeypatch):
    make_lnk(lnk)
    real_unlink = Path.unlink

    def deny(self, *a, **k):
        if self.name == "SchulSync.lnk":
            raise PermissionError(13, "Zugriff verweigert")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny)
    result = autostart.remove()
    assert not result.ok and "Verknüpfung" in result.details


# --------------------------------------------------------------------------- status


def test_status_task(system):
    system.task_present = True
    st = autostart.status()
    assert st.method == "task" and st.ok
    assert "Aufgabenplanung" in st.details and "SchulSync" in st.details
    assert system.ops() == ["/query"]  # nur lesend


def test_status_shortcut(system, lnk):
    make_lnk(lnk)
    st = autostart.status()
    assert st.method == "shortcut" and st.ok
    assert "Verknüpfung" in st.details and "Verzögerung" in st.details


def test_status_not_installed(system):
    st = autostart.status()
    assert st.method is None and not st.ok
    assert "nicht eingerichtet" in st.details
    assert "autostart install" in st.details


def test_status_prefers_task_and_mentions_leftover_shortcut(system, lnk):
    system.task_present = True
    make_lnk(lnk)
    st = autostart.status()
    assert st.method == "task" and st.ok
    assert "Verknüpfung" in st.details  # Hinweis auf Doppelung


def test_status_never_raises_when_schtasks_cannot_run(system, lnk, monkeypatch):
    def boom(args, **kw):
        raise subprocess.TimeoutExpired("schtasks", 30)

    monkeypatch.setattr(autostart.subprocess, "run", boom)
    make_lnk(lnk)
    st = autostart.status()
    assert st.method == "shortcut" and st.ok


# --------------------------------------------------------------------------- Hilfsfunktionen


def test_decode_console_output_cp850_umlauts(monkeypatch):
    monkeypatch.setattr(autostart, "_oem_codepage", lambda: 850)
    assert autostart._decode("Ungültig ä ö ü ß".encode("cp850")) == "Ungültig ä ö ü ß"
    assert autostart._decode("Größe".encode("utf-8")) == "Größe"
    assert autostart._decode(b"") == ""
    assert autostart._decode(b"plain ascii") == "plain ascii"


def test_decode_falls_back_to_cp1252_when_oem_cannot_decode(monkeypatch):
    monkeypatch.setattr(autostart, "_oem_codepage", lambda: 932)  # Shift-JIS: 0x81 0x20 ist ungueltig
    assert autostart._decode(b"\x81 x") == "\x81 x".encode("latin-1").decode("cp1252", errors="replace")


def test_current_user_has_domain_and_name():
    if sys.platform != "win32":
        pytest.skip("nur Windows")
    user = autostart.current_user()
    assert "\\" in user and not user.endswith("\\") and not user.startswith("\\")


def test_current_user_falls_back_to_environment(monkeypatch):
    def fail():
        raise OSError("kein Win32")

    monkeypatch.setattr(autostart, "_sam_compatible_name", fail)
    monkeypatch.setenv("USERDOMAIN", "SCHULE")
    monkeypatch.setenv("USERNAME", "lisa")
    assert autostart.current_user() == "SCHULE\\lisa"


def test_shortcut_path_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    assert autostart.shortcut_path() == (
        tmp_path / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SchulSync.lnk"
    )


def test_module_has_no_shell_true():
    src = Path(autostart.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src
    assert "os.system" not in src


# --------------------------------------------------------------------------- scripts/install.ps1

INSTALL_PS1 = Path(__file__).resolve().parent.parent / "scripts" / "install.ps1"


def test_install_script_content():
    raw = INSTALL_PS1.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8-BOM noetig: Windows PowerShell 5.1 liest BOM-lose Dateien als ANSI"
    content = raw.decode("utf-8-sig")
    for needle in ("venv", "pip", "install -e", "schulsync setup", "schulsync check", "schulsync autostart install"):
        assert needle in content
    assert "-ExecutionPolicy Bypass -File scripts\\install.ps1" in content  # dokumentierte Option
    lowered = content.lower()
    assert "set-executionpolicy" not in lowered  # keine Systemaenderung
    assert "-verb runas" not in lowered and "runas" not in lowered  # keine Erhoehung
    assert "start-process" not in lowered


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell nicht gefunden")
def test_install_script_parses_without_syntax_errors():
    # Nur der Parser, es wird NICHTS ausgefuehrt.
    cmd = (
        "$e=$null;$t=$null;"
        "[void][System.Management.Automation.Language.Parser]::ParseFile($env:SCHULSYNC_PS1,[ref]$t,[ref]$e);"
        "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}"
    )
    env = dict(os.environ, SCHULSYNC_PS1=str(INSTALL_PS1))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout.decode("cp850", errors="replace")


# --------------------------------------------------------------------------- benutzerdefinierter Datenordner (--home)


def test_install_with_default_home_adds_no_home_option(system, tmp_path, monkeypatch):
    # The default is the folder below LOCALAPPDATA. SCHULSYNC_HOME of the installing shell is NOT the default: the task
    # does not inherit it (see test_review2_cli.test_autostart_install_carries_home_when_it_comes_from_the_environment).
    monkeypatch.delenv("SCHULSYNC_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    default = Paths.default()
    result = autostart.install(default)
    root = ET.fromstring(system.xml_bytes)
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "-m schulsync run --auto"
    assert "--home" not in result.details


def test_install_carries_custom_home_quoted_into_task_arguments(system, tmp_path, monkeypatch):
    monkeypatch.delenv("SCHULSYNC_HOME", raising=False)
    custom = Paths(tmp_path / "Mein Ordner")
    result = autostart.install(custom)
    root = ET.fromstring(system.xml_bytes)
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == f'-m schulsync run --auto --home "{custom.home}"'
    assert "--home" in result.details and str(custom.home) in result.details


# --------------------------------------------------------------------------- audit: does WINDOWS accept the XML?


_COM_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
} catch {
    Write-Output ('SKIP: ' + $_.Exception.Message)
    exit 0
}
$def = $svc.NewTask(0)                      # an in-memory definition: NOTHING is registered
$def.XmlText = [IO.File]::ReadAllText($args[0])   # the parser of the Task Scheduler itself; throws for anything it rejects
$trigger = $def.Triggers.Item(1)
Write-Output ('TRIGGERS=' + $def.Triggers.Count)
Write-Output ('TRIGGER_TYPE=' + $trigger.Type)
Write-Output ('TRIGGER_USER=' + $trigger.UserId)
Write-Output ('TRIGGER_DELAY=' + $trigger.Delay)
Write-Output ('LOGON_TYPE=' + $def.Principal.LogonType)
Write-Output ('RUN_LEVEL=' + $def.Principal.RunLevel)
Write-Output ('INSTANCES=' + $def.Settings.MultipleInstances)
Write-Output ('TIME_LIMIT=' + $def.Settings.ExecutionTimeLimit)
Write-Output ('ACTION=' + $def.Actions.Item(1).Path + '|' + $def.Actions.Item(1).Arguments)
"""


def test_windows_task_scheduler_parses_the_generated_xml_and_reads_back_what_matters(tmp_path):
    """The tests above prove the XML is well formed and says the right things; they would also pass if Windows rejected it.
    Here the Task Scheduler's own parser reads it (COM `ITaskDefinition.XmlText`, an in-memory definition: nothing is
    registered, nothing on the system changes) and the values that matter come back as Windows understands them: a logon
    trigger of the user with a delay, an interactive token, NO elevation, one instance at a time, the time limit and the
    action. Skipped where there is no Task Scheduler COM service (not Windows, locked-down machine).

    Still NOT proven: that a standard user may REGISTER the task on the target PC (group policy) - `schulsync autostart install`
    on that PC is the test for that."""
    if sys.platform != "win32":
        pytest.skip("Windows only")
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("no PowerShell")
    xml = autostart.build_task_xml(
        user_sid_or_name=autostart.current_user(),
        command=r"C:\Users\Jörg Beispiel\SchulSync\.venv\Scripts\pythonw.exe",
        arguments="-m schulsync run --auto",
        working_dir=str(tmp_path),
    )
    xml_file = tmp_path / "task.xml"
    xml_file.write_bytes(xml.encode("utf-16"))  # with BOM, like the real install
    script = tmp_path / "parse.ps1"
    script.write_text(_COM_SCRIPT, encoding="utf-8-sig")
    done = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), str(xml_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = done.stdout.strip()
    if out.startswith("SKIP:"):
        pytest.skip(out)
    assert done.returncode == 0, f"Windows lehnt die Aufgaben-XML ab: {done.stderr.strip() or out}"
    got = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert got["TRIGGERS"] == "3" and got["TRIGGER_TYPE"] == "9"  # first: TASK_TRIGGER_LOGON, then two daily triggers
    assert got["TRIGGER_USER"].lower() == autostart.current_user().lower() and got["TRIGGER_DELAY"] == "PT1M"
    assert got["LOGON_TYPE"] == "3" and got["RUN_LEVEL"] == "0"  # TASK_LOGON_INTERACTIVE_TOKEN, TASK_RUNLEVEL_LUA (least privilege)
    assert got["INSTANCES"] == "2"  # TASK_INSTANCES_IGNORE_NEW
    assert got["TIME_LIMIT"] == "PT1H"
    assert got["ACTION"].endswith("pythonw.exe|-m schulsync run --auto")


def test_the_settings_that_matter_for_a_logon_task_are_pinned_not_only_the_harmless_ones():
    """`StartWhenAvailable` means nothing for a logon trigger (it is about missed schedule times), so pinning it proves
    little. These are the settings that decide whether the daily run happens and what it may do."""
    root = parse(sample_xml())
    s = "t:Settings/t:"
    assert text(root, s + "MultipleInstancesPolicy") == "IgnoreNew"  # a second logon trigger must not start a second run
    assert text(root, s + "DisallowStartIfOnBatteries") == "false" and text(root, s + "StopIfGoingOnBatteries") == "false"  # laptops
    assert text(root, s + "RunOnlyIfNetworkAvailable") == "false"  # the program waits for the network itself
    assert text(root, s + "AllowHardTerminate") == "true" and text(root, s + "ExecutionTimeLimit") == "PT1H"
    assert text(root, s + "WakeToRun") == "false" and text(root, s + "Enabled") == "true" and text(root, s + "Hidden") == "false"
    assert text(root, "t:Triggers/t:LogonTrigger/t:Delay") == "PT1M"
    # logon plus the fixed daily runs at 06:00 and 18:00; missed ones run when the PC is on again
    assert [child.tag.split("}")[1] for child in root.find("t:Triggers", NS)] == ["LogonTrigger", "CalendarTrigger", "CalendarTrigger"]
    assert text(root, s + "StartWhenAvailable") == "true"
