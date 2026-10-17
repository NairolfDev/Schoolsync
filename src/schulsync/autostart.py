"""Autostart nach der Windows-Anmeldung: Aufgabenplanung (LogonTrigger) oder Startup-Verknüpfung.

Ziel (ANFORDERUNGEN §1): Das Programm startet nach der Anmeldung des Benutzers, mit kurzer Verzögerung, im Benutzerkontext
(interaktive Sitzung, damit ein Browser aufgehen kann) und OHNE erhöhte Rechte. Bevorzugt Aufgabenplanung, sonst eine
Verknüpfung im Startup-Ordner des Benutzers.

Belegte Grundlagen (Microsoft Learn, am 2026-10-04 gelesen):
- LogonTrigger/UserId/Delay (ISO-8601-Dauer, z. B. PT5M): win32/taskschd/taskschedulerschema-logontrigger-triggergroup-element
  und ...-delay-logontriggertype-element. Ohne UserId würde der Trigger für JEDEN Benutzer gelten (LogonTrigger-Objekt,
  Remarks); wir setzen UserId deshalb immer.
- Principal: LogonType=InteractiveToken ("User must already be logged on. The task will be run only in an existing
  interactive session"), RunLevel=LeastPrivilege (taskschedulerschema-logontype-simpletype / -runlevel-principaltype-element).
- Standardbenutzer: "Security Contexts for Tasks" (win32/taskschd/security-contexts-for-running-tasks), Abschnitt "Security
  for Registering Tasks": Ein Nicht-Administrator braucht kein Passwort, wenn er die Aufgabe unter dem eigenen Konto mit
  S4U- oder InteractiveToken-Logon registriert. Aus einem Prozess mit niedrigen Rechten kann RunLevel LUA (LeastPrivilege)
  registriert werden, nicht aber HIGHEST, Administratorkonto, SYSTEM oder eine Gruppe. -> Eigene Aufgabe, eigenes Konto,
  InteractiveToken, LeastPrivilege ist der dokumentiert erlaubte Weg.
- Reale Gegenprobe (Export auf dem Zielrechner, nur lesend): Die von OneDrive unter dem Benutzerkonto angelegte Aufgabe
  "OneDrive Startup Task-<SID>" hat genau diese Form (LogonTrigger mit UserId und Delay, LogonType InteractiveToken, kein
  RunLevel = LeastPrivilege). Exportierte Aufgaben tragen `encoding="UTF-16"` und nennen den Programmpfad (auch mit
  Leerzeichen, z. B. unter C:\\Program Files) OHNE Anführungszeichen im <Command>-Element.
- schtasks /Create /XML <Datei> /TN <Name> /F: Parameterbeschreibung in windows-commands/schtasks-create ("/xml ... Creates a
  task specified in the XML file", "/f ... suppress warnings if the specified task already exists"). Dass die Datei UTF-16
  (mit BOM) sein muss, steht dort NICHT; wir folgen dem Format, das Windows selbst exportiert (UTF-16, Deklaration
  `encoding="UTF-16"`). Das ist die Konvention, keine zitierte Pflicht.

NICHT belegt / nur auf dem Zielsystem prüfbar (siehe README/Ergebnisbericht): dass genau diese XML auf dem Zielrechner
akzeptiert wird, dass ein Standardbenutzer sie dort tatsächlich registrieren darf (Gruppenrichtlinien, verwaltete
Schulgeräte), und die genauen Fehlermeldungen. Vorhersage: bei verbotener Registrierung meldet schtasks "ERROR: Access is
denied." bzw. auf deutschem Windows "FEHLER: Zugriff verweigert." (HRESULT 0x80070005); bei kaputter XML "ERROR: The task
XML is malformed / contains a value which is incorrectly formatted or out of range". Weil die Meldungssprache von der
Windows-Anzeigesprache abhängt, entscheidet NUR der Exit-Code über Erfolg; der Text dient der Erklärung (Zugriff-verweigert-
Erkennung per Stichwort "denied"/"verweigert"/0x80070005, sonst allgemeine Meldung mit der Originalzeile).

Ausweichlösung: <APPDATA>\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\SchulSync.lnk (angelegt per PowerShell/
WScript.Shell). Eine Verknüpfung kann den Start NICHT verzögern. Das ist unkritisch: Der Lauf wartet selbst bis zu
`retry.network_wait_seconds` auf das Netz (DESIGN §7.3). Bewusst kein .cmd-Wrapper (Codepage-/Quoting-Fallen bei Umlauten).
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import ENV_HOME, RUN_TIMES, Paths

TASK_NAME = "SchulSync"
SHORTCUT_NAME = "SchulSync.lnk"
RUN_ARGUMENTS = "-m schulsync run --auto"

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
# ISO-8601-Dauer (xs:duration, nur positive): mindestens eine Komponente.
_DURATION = re.compile(r"P(?=[0-9]|T[0-9])([0-9]+Y)?([0-9]+M)?([0-9]+D)?(T(?=[0-9])([0-9]+H)?([0-9]+M)?([0-9]+S)?)?")
_SCHTASKS_TIMEOUT = 30.0
_POWERSHELL_TIMEOUT = 60.0
_SHORTCUT_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SCHULSYNC_LNK);"
    "$s.TargetPath=$env:SCHULSYNC_TARGET;"
    "$s.Arguments=$env:SCHULSYNC_ARGS;"
    "$s.WorkingDirectory=$env:SCHULSYNC_WORKDIR;"
    "$s.Description='SchulSync: Abgleich nach der Anmeldung';"
    "$s.WindowStyle=7;"  # 7 = minimiert (pythonw zeigt ohnehin kein Fenster)
    "$s.Save()"
)


@dataclass(frozen=True)
class AutostartStatus:
    """Ergebnis von install/remove/status. `details` ist deutscher Klartext für die Konsole."""

    method: Literal["task", "shortcut"] | None
    details: str
    ok: bool


# --------------------------------------------------------------------------- Task-XML


def _check_duration(name: str, value: str) -> str:
    if not _DURATION.fullmatch(value):
        raise ValueError(f"{name} muss eine ISO-8601-Dauer sein (z. B. PT1M), nicht {value!r}")
    return value


def build_task_xml(
    *,
    user_sid_or_name: str,
    command: str,
    arguments: str,
    working_dir: str,
    delay: str = "PT1M",
    time_limit: str = "PT1H",
) -> str:
    """Task-Scheduler-XML (Schema 1.2) für einen Anmeldetrigger des Benutzers. Alle Werte werden XML-maskiert.

    `user_sid_or_name`: `DOMAIN\\name` (wie von `current_user()`) oder eine SID. Pfade ohne Anführungszeichen (siehe Modul-
    Docstring). Leere `arguments`/`working_dir` lassen das jeweilige Element weg.
    """
    if not user_sid_or_name.strip():
        # Ein leeres UserId-Element ließe den Anmeldetrigger für JEDEN Benutzer gelten (Modul-Docstring).
        raise ValueError("user_sid_or_name darf nicht leer sein")
    if not command.strip():
        raise ValueError("command darf nicht leer sein")  # leeres <Command/>: Aufgabe lässt sich anlegen, startet aber nie
    _check_duration("delay", delay)
    _check_duration("time_limit", time_limit)
    ET.register_namespace("", _NS)

    def sub(parent: ET.Element, name: str, value: str | None = None, **attrs: str) -> ET.Element:
        el = ET.SubElement(parent, f"{{{_NS}}}{name}", attrs)
        if value is not None:
            el.text = value
        return el

    root = ET.Element(f"{{{_NS}}}Task", {"version": "1.2"})
    info = sub(root, "RegistrationInfo")
    sub(info, "Author", user_sid_or_name)
    sub(info, "Description", "SchulSync: gleicht Eduvidual und WebUntis nach der Windows-Anmeldung und täglich um 6 und 18 Uhr mit Google Kalender und Aufgaben ab.")

    triggers = sub(root, "Triggers")
    logon = sub(triggers, "LogonTrigger")
    sub(logon, "Enabled", "true")
    sub(logon, "UserId", user_sid_or_name)
    sub(logon, "Delay", delay)
    for hhmm in RUN_TIMES:  # zusätzliche feste Läufe; verpasste holt StartWhenAvailable nach dem Einschalten nach
        cal = sub(triggers, "CalendarTrigger")
        sub(cal, "StartBoundary", f"2026-01-01T{hhmm}:00")  # ohne Zeitzone = Ortszeit, folgt Sommer-/Winterzeit
        sub(cal, "Enabled", "true")
        sub(sub(cal, "ScheduleByDay"), "DaysInterval", "1")

    principal = sub(sub(root, "Principals"), "Principal", id="Author")
    sub(principal, "UserId", user_sid_or_name)
    sub(principal, "LogonType", "InteractiveToken")
    sub(principal, "RunLevel", "LeastPrivilege")

    settings = sub(root, "Settings")
    sub(settings, "MultipleInstancesPolicy", "IgnoreNew")
    sub(settings, "DisallowStartIfOnBatteries", "false")
    sub(settings, "StopIfGoingOnBatteries", "false")
    sub(settings, "AllowHardTerminate", "true")
    sub(settings, "StartWhenAvailable", "true")
    sub(settings, "RunOnlyIfNetworkAvailable", "false")  # das Programm wartet selbst auf das Netz
    sub(settings, "AllowStartOnDemand", "true")
    sub(settings, "Enabled", "true")
    sub(settings, "Hidden", "false")  # in der Aufgabenplanung sichtbar lassen (Transparenz, einfaches Entfernen)
    sub(settings, "WakeToRun", "false")
    sub(settings, "ExecutionTimeLimit", time_limit)

    exec_ = sub(sub(root, "Actions", Context="Author"), "Exec")
    sub(exec_, "Command", command)
    if arguments:
        sub(exec_, "Arguments", arguments)
    if working_dir:
        sub(exec_, "WorkingDirectory", working_dir)

    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# --------------------------------------------------------------------------- Umgebung


def default_command() -> tuple[str, str]:
    """(Programm, Argumente) für den Autostart: pythonw.exe neben dem aktuellen Interpreter (kein Konsolenfenster).

    Der Pfad wird unverändert zurückgegeben (kein Quoting): Task-XML-<Command> und COM-`TargetPath` erwarten den reinen Pfad.
    Ohne pythonw.exe wird der aktuelle Interpreter genommen. Hinweis: unter pythonw ist `sys.stdout` None.
    """
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if exe.name.lower() == "pythonw.exe" or pythonw.exists():
        exe = pythonw
    return str(exe), RUN_ARGUMENTS


def _sam_compatible_name() -> str:
    """`DOMAIN\\name` des angemeldeten Benutzers per GetUserNameExW(NameSamCompatible=2); Unicode, keine Codepage-Probleme."""
    import ctypes

    size = ctypes.c_ulong(512)
    buf = ctypes.create_unicode_buffer(size.value)
    if not ctypes.windll.secur32.GetUserNameExW(2, buf, ctypes.byref(size)):  # type: ignore[attr-defined]
        raise OSError("GetUserNameExW fehlgeschlagen")
    return buf.value


def current_user() -> str:
    """Der aktuelle Benutzer als `DOMAIN\\name` (nie hart codiert). Ausweichen auf USERDOMAIN/USERNAME."""
    try:
        name = _sam_compatible_name()
        if name:
            return name
    except (OSError, AttributeError):
        pass
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def _oem_codepage() -> int:
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetOEMCP())  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return 850


def _decode(data: bytes) -> str:
    """Konsolenausgabe sicher dekodieren: UTF-8, dann OEM-Codepage (meist cp850), dann cp1252, zuletzt mit Ersatzzeichen."""
    for enc in ("utf-8", f"cp{_oem_codepage()}", "cp1252"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("cp1252", errors="replace")


def shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / SHORTCUT_NAME


def _system_exe(*parts: str) -> str:
    """Absoluter Pfad unter %SystemRoot%\\System32 (kein PATH-Suchen); sonst der blanke Name."""
    p = Path(os.environ.get("SystemRoot") or r"C:\Windows", "System32", *parts)
    return str(p) if p.exists() else parts[-1]


# --------------------------------------------------------------------------- Prozessaufrufe


@dataclass(frozen=True)
class _Result:
    returncode: int | None  # None = Aufruf nicht möglich (Zeitüberschreitung/Programm fehlt); `text` erklärt warum
    text: str
    timed_out: bool = False


def _run(args: list[str], *, timeout: float, env: dict[str, str] | None = None) -> _Result:
    """Externes Programm ohne Shell, mit Zeitlimit, ohne Konsolenfenster, ohne Eingabe. Wirft nie."""
    try:
        cp = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return _Result(None, f"Zeitüberschreitung nach {timeout:.0f} s", timed_out=True)
    except OSError as exc:
        return _Result(None, str(exc))
    out = (cp.stdout or b"") + (cp.stderr or b"")
    return _Result(cp.returncode, _decode(out).strip())


def _one_line(text: str, limit: int = 300) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _schtasks(*args: str) -> _Result:
    return _run([_system_exe("schtasks.exe"), *args], timeout=_SCHTASKS_TIMEOUT)


def _query_task() -> _Result:
    return _schtasks("/Query", "/TN", TASK_NAME)


# Not-found signals of schtasks. Measured on an en-US Windows 11 (not elevated): /Query of a task that does not exist ends
# with exit code 1 and "ERROR: The system cannot find the file specified."; an existing task gives 0. The German wording
# (ERROR_FILE_NOT_FOUND, "Das System kann die angegebene Datei nicht finden.") and the HRESULT 0x80070002 are the same
# condition in another language / form. NOT measured here: other languages beyond these. Anything else stays "unknown".
_NOT_FOUND_SIGNALS = ("cannot find the file", "kann die angegebene datei nicht finden", "0x80070002")


def _is_not_found(r: _Result) -> bool:
    low = r.text.lower()
    return any(signal in low for signal in _NOT_FOUND_SIGNALS)


def _task_state(r: _Result) -> Literal["present", "absent", "unknown"]:
    """'present' = exit 0. 'absent' only for exit 1 PLUS a not-found signal in the text: "access denied", a localised
    message we do not know, a timeout or a missing schtasks.exe say nothing about the task and must never be read as
    "not there" (that would claim 'nothing to remove' while the task stays and starts at every logon)."""
    if r.returncode == 0:
        return "present"
    if r.returncode == 1 and _is_not_found(r):
        return "absent"
    return "unknown"


def _first_line(r: _Result) -> str:
    """What the user needs to copy into a bug report: the first line of the raw answer, with the exit code if there is one."""
    line = _one_line(next((ln for ln in r.text.splitlines() if ln.strip()), ""))
    code = f"Exit-Code {r.returncode}" if r.returncode is not None else "nicht ausgeführt"
    return f"{code}: {line}" if line else code


def _is_access_denied(text: str) -> bool:
    low = text.lower()
    return "denied" in low or "verweigert" in low or "0x80070005" in low


def _task_failure_reason(r: _Result) -> str:
    if r.timed_out:
        return "Die Aufgabenplanung hat nicht rechtzeitig geantwortet (Zeitüberschreitung)."
    if r.returncode is None:
        return f"Die Aufgabenplanung (schtasks.exe) konnte nicht gestartet werden: {_one_line(r.text)}"
    if _is_access_denied(r.text):
        return "Die Aufgabenplanung hat das Anlegen ohne Administratorrechte abgelehnt (Zugriff verweigert)."
    detail = f": {_one_line(r.text)}" if r.text else ""
    return f"Die Aufgabenplanung hat das Anlegen abgelehnt (Exit-Code {r.returncode}){detail}"


# --------------------------------------------------------------------------- Aufgabenplanung


def _try_task(xml: str) -> str | None:
    """Legt die Aufgabe an und prüft sie. None = Erfolg, sonst deutscher Grund für den Fehlschlag."""
    try:
        fd, name = tempfile.mkstemp(prefix="schulsync-task-", suffix=".xml")
    except OSError as exc:  # z. B. %TEMP% voll/nicht beschreibbar: Ausweichlösung statt Abbruch
        return f"Die temporäre Aufgabendatei konnte nicht angelegt werden: {_one_line(str(exc))}"
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(xml.encode("utf-16"))  # UTF-16 mit BOM, passend zur Deklaration
        created = _schtasks("/Create", "/TN", TASK_NAME, "/XML", name, "/F")
    except OSError as exc:
        return f"Die temporäre Aufgabendatei konnte nicht geschrieben werden: {_one_line(str(exc))}"
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    if created.returncode != 0:
        reason = _task_failure_reason(created)
        # /Create kann trotz Zeitüberschreitung/Fehler eine Aufgabe hinterlassen haben (oder eine ältere besteht noch):
        # sie muss weg, sonst starten Aufgabe UND Ausweich-Verknüpfung beim Anmelden.
        if _query_task().returncode == 0 and _schtasks("/Delete", "/TN", TASK_NAME, "/F").returncode != 0:
            reason += f" Achtung: Die Aufgabe „{TASK_NAME}“ besteht noch und konnte nicht gelöscht werden; sie startet zusätzlich zur Verknüpfung."
        return reason
    if _query_task().returncode != 0:
        _schtasks("/Delete", "/TN", TASK_NAME, "/F")  # Halbfertiges aufräumen, sonst Doppelstart neben der Verknüpfung
        return "Die Aufgabe war nach dem Anlegen nicht auffindbar."
    return None


# --------------------------------------------------------------------------- Startup-Verknüpfung


def _create_shortcut(command: str, arguments: str, working_dir: str) -> str | None:
    """Legt die .lnk an. None = Erfolg, sonst deutscher Fehlertext. Werte laufen über Umgebungsvariablen, nicht über den Skripttext."""
    lnk = shortcut_path()
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Der Startup-Ordner ist nicht beschreibbar: {_one_line(str(exc))}"
    env = dict(os.environ)
    env.update(SCHULSYNC_LNK=str(lnk), SCHULSYNC_TARGET=command, SCHULSYNC_ARGS=arguments, SCHULSYNC_WORKDIR=working_dir)
    r = _run(
        [_system_exe("WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", _SHORTCUT_SCRIPT],
        timeout=_POWERSHELL_TIMEOUT,
        env=env,
    )
    if r.returncode != 0:
        return f"PowerShell konnte die Verknüpfung nicht anlegen: {_one_line(r.text) or 'ohne Meldung'}"
    if not lnk.exists():
        return "PowerShell meldete Erfolg, aber die Verknüpfung wurde nicht angelegt."
    return None


def _remove_shortcut() -> str | None:
    """None = entfernt oder nicht vorhanden, sonst Fehlertext."""
    try:
        shortcut_path().unlink(missing_ok=True)
    except OSError as exc:
        return _one_line(str(exc))
    return None


# --------------------------------------------------------------------------- Öffentliche Schnittstelle


def _is_default_home(paths: Paths) -> bool:
    """True only for the built-in default folder (LOCALAPPDATA, then SchulSync). NOT `Paths.default()`: that one honours the variable
    SCHULSYNC_HOME of the installing shell, which the Task Scheduler does not inherit, so a task installed from such a
    shell would otherwise silently use another (empty) data folder."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return os.path.normcase(os.path.abspath(paths.home)) == os.path.normcase(os.path.abspath(Path(base) / "SchulSync"))


def _arguments_for(arguments: str, paths: Paths) -> str:
    """Hängt `--home <Ordner>` an, wenn `paths` nicht dem Standard entspricht.

    Der Autostart startet ohne Umgebungsvariable SCHULSYNC_HOME; ohne diese Option würde er den Standardordner nutzen. Das
    Argument wird für die Windows-Kommandozeile quotiert (Leerzeichen im Pfad); `--home` akzeptiert die CLI auch hinter dem
    Unterbefehl.
    """
    if _is_default_home(paths):
        return arguments
    # absolute: the task starts in another working directory, a relative folder would point somewhere else
    return f"{arguments} --home {subprocess.list2cmdline([os.path.abspath(paths.home)])}"


def _home_note(paths: Paths) -> str:
    """Hinweis auf den benutzerdefinierten Datenordner, den der Autostart per `--home` erhält (leer beim Standardordner)."""
    if _is_default_home(paths):
        return ""
    return f" Der Autostart verwendet den Datenordner {paths.home} (Option --home)."


def install(paths: Paths) -> AutostartStatus:
    """Richtet den Autostart ein: erst Aufgabenplanung, bei jedem Fehlschlag die Startup-Verknüpfung."""
    paths.ensure()
    note = _home_note(paths)  # Arbeitsverzeichnis der Aufgabe muss existieren
    command, arguments = default_command()
    arguments = _arguments_for(arguments, paths)
    xml = build_task_xml(
        user_sid_or_name=current_user(), command=command, arguments=arguments, working_dir=os.path.abspath(paths.home)
    )
    reason = _try_task(xml)
    if reason is None:
        stale = _remove_shortcut()  # alte Ausweichlösung entfernen, sonst startet das Programm doppelt
        warning = (
            f" Achtung: Die alte Verknüpfung im Startup-Ordner ({shortcut_path()}) konnte nicht gelöscht werden ({stale}); "
            "SchulSync startet dadurch doppelt. Bitte lösche die Verknüpfung von Hand."
            if stale
            else ""
        )
        return AutostartStatus(
            "task",
            f"Autostart über die Windows-Aufgabenplanung eingerichtet (Aufgabe „{TASK_NAME}“). "
            f"SchulSync startet etwa eine Minute nach Ihrer Anmeldung, im Benutzerkonto und ohne Administratorrechte.{warning}{note}",
            True,
        )

    problem = _create_shortcut(command, arguments, os.path.abspath(paths.home))
    if problem is None:
        return AutostartStatus(
            "shortcut",
            f"Autostart über eine Verknüpfung im Startup-Ordner eingerichtet ({shortcut_path()}). {reason} "
            "Hinweis: Eine Startverzögerung ist mit einer Verknüpfung nicht möglich (Verzögerung = 0); SchulSync wartet selbst auf das Netzwerk. "
            "Entfernen: 'schulsync autostart remove'." + note,
            True,
        )
    return AutostartStatus(
        None,
        f"Autostart konnte nicht eingerichtet werden. {reason} {problem} "
        f"Manuell: Win+R, 'shell:startup' eingeben und dort eine Verknüpfung auf \"{command}\" mit den Argumenten "
        f"{arguments} anlegen.{note}",
        False,
    )


def remove() -> AutostartStatus:
    """Entfernt Aufgabe UND Verknüpfung, soweit vorhanden. Idempotent: Fehlendes ist kein Fehler."""
    removed: list[str] = []
    errors: list[str] = []

    query = _query_task()
    state = _task_state(query)
    if state != "absent":  # present, or unknown: then the delete is the only way to learn what is really there
        deleted = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
        if deleted.returncode == 0:
            removed.append(f"Aufgabe „{TASK_NAME}“ aus der Aufgabenplanung")
        elif state == "unknown" and _task_state(deleted) == "absent":
            pass  # the delete itself proves that there is no task
        elif state == "present":
            errors.append(f"Die Aufgabe konnte nicht gelöscht werden: {_one_line(deleted.text) or 'ohne Meldung'}")
        else:
            errors.append(
                f"Die Aufgabe „{TASK_NAME}“ konnte nicht entfernt werden; ob sie besteht, ist unbekannt. "
                f"Abfrage: {_first_line(query)}. Löschen: {_first_line(deleted)}."
            )

    lnk = shortcut_path()
    existed = lnk.exists()
    problem = _remove_shortcut()
    if problem:
        errors.append(f"Die Verknüpfung im Startup-Ordner konnte nicht gelöscht werden: {problem}")
    elif existed:
        removed.append("Verknüpfung im Startup-Ordner")

    parts = []
    if removed:
        parts.append("Entfernt: " + " und ".join(removed) + ".")
    if errors:
        parts.append("Fehler: " + " ".join(errors))
    if not parts:
        parts.append("Es war kein Autostart eingerichtet. Nichts zu entfernen.")
    return AutostartStatus(None, " ".join(parts), not errors)


def status() -> AutostartStatus:
    """Nur lesend: ob (und wie) der Autostart eingerichtet ist."""
    query = _query_task()
    state = _task_state(query)
    lnk = shortcut_path()
    shortcut = lnk.exists()
    if state == "present":
        extra = (
            f" Zusätzlich liegt eine alte Verknüpfung im Startup-Ordner ({lnk}); 'schulsync autostart install' räumt sie auf."
            if shortcut
            else ""
        )
        return AutostartStatus(
            "task",
            f"Autostart ist über die Windows-Aufgabenplanung eingerichtet (Aufgabe „{TASK_NAME}“, Start etwa eine Minute nach der Anmeldung).{extra}",
            True,
        )
    unknown_note = (
        f" Die Aufgabenplanung konnte nicht eindeutig abgefragt werden ({_first_line(query)}); eine Aufgabe kann zusätzlich bestehen."
        if state == "unknown"
        else ""
    )
    if shortcut:
        return AutostartStatus(
            "shortcut",
            f"Autostart ist über eine Verknüpfung im Startup-Ordner eingerichtet ({lnk}), ohne Startverzögerung "
            f"(eine Verzögerung ist mit einer Verknüpfung nicht möglich).{unknown_note}",
            True,
        )
    if state == "unknown":  # nicht "nicht eingerichtet" behaupten (sonst Doppel-Einrichtung, und 'remove' bliebe wirkungslos)
        return AutostartStatus(
            None,
            f"Der Autostart-Status ist unbekannt: Die Aufgabenplanung konnte nicht eindeutig abgefragt werden ({_first_line(query)}). "
            "Es liegt keine Verknüpfung im Startup-Ordner, eine Aufgabe kann aber bestehen. "
            "'schulsync autostart remove' versucht trotzdem, sie zu löschen.",
            False,
        )
    return AutostartStatus(
        None, "Autostart ist nicht eingerichtet. Einrichten mit: schulsync autostart install", False
    )
