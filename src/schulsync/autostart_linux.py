"""Two daily systemd-user runs, with a separate one-shot retry and Google check."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .autostart import AutostartStatus
from .config import Config, Paths, load_config
from . import retry_state

MAIN = "schulsync.timer"
RETRY = "schulsync-retry.timer"
GOOGLE = "schulsync-google-login.timer"


def _unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user"


def _systemctl(*args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30, check=False)
        return done.returncode, (done.stderr or done.stdout).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, type(exc).__name__


def _quote(value: str) -> str:
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("Ungültiges Zeichen im systemd-Pfad")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def _service(paths: Paths, command: str) -> str:
    executable = os.environ.get("APPIMAGE") if getattr(sys, "frozen", False) else None
    program = [_quote(str(Path(executable or sys.executable).resolve()))]
    if not getattr(sys, "frozen", False):
        program += ["-m", "schulsync"]
    program += command.split() + ["--home", _quote(str(paths.home.resolve()))]
    environment = "Environment=APPIMAGE_EXTRACT_AND_RUN=1\n" if executable else ""
    return "[Unit]\nDescription=SchulSync\n\n[Service]\nType=oneshot\n" + environment + "ExecStart=" + " ".join(program) + "\n"


def _write(name: str, contents: str) -> None:
    directory = _unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(contents, encoding="utf-8")
    os.replace(temp, path)


def _check(*args: str) -> None:
    rc, output = _systemctl(*args)
    if rc:
        raise OSError("systemd --user: " + output[:300])


def install(paths: Paths) -> AutostartStatus:
    try:
        cfg = load_config(paths.config) if paths.config.exists() else Config()
        paths.ensure()
        times = cfg.schedule.run_times
        _write("schulsync.service", _service(paths, "run --auto"))
        _write(MAIN, "[Unit]\nDescription=SchulSync tägliche Synchronisierung\n\n[Timer]\n" +
               "".join(f"OnCalendar=*-*-* {t}:00\n" for t in times) +
               "Persistent=true\nAccuracySec=1s\n\n[Install]\nWantedBy=timers.target\n")
        _write("schulsync-google-login.service", _service(paths, "google-auth-check --auto"))
        _write(GOOGLE, "[Unit]\nDescription=SchulSync Google-Login prüfen\n\n[Timer]\nOnCalendar=hourly\n"
               "Persistent=true\nAccuracySec=1s\n\n[Install]\nWantedBy=timers.target\n")
        _check("daemon-reload")
        _check("enable", "--now", MAIN, GOOGLE)
        _check("restart", MAIN, GOOGLE)
        state = retry_state.read(paths)
        due = state.get("next_retry")
        if not due and state.get("in_progress"):
            due = (datetime.fromisoformat(state["in_progress"]).astimezone(timezone.utc) + timedelta(hours=1)).isoformat()
        schedule_retry(paths, datetime.fromisoformat(due) if due else None)
        return AutostartStatus("task", f"systemd-Benutzertimer eingerichtet: täglich um {times[0]} und {times[1]} Uhr.", True)
    except (OSError, ValueError, TypeError) as exc:
        return AutostartStatus(None, f"systemd-Benutzertimer konnten nicht eingerichtet werden: {exc}", False)


def schedule_retry(paths: Paths, due: datetime | None) -> None:
    directory = _unit_dir()
    timer = directory / RETRY
    if due is None:
        if not timer.exists():
            return
        _check("disable", "--now", RETRY)
        timer.unlink(missing_ok=True)
        (directory / "schulsync-retry.service").unlink(missing_ok=True)
        _check("daemon-reload")
        return
    if _systemctl("is-enabled", MAIN)[0] != 0:
        return
    if due.tzinfo is None:
        raise ValueError("Retry-Zeitpunkt muss eine Zeitzone haben")
    utc = due.astimezone(timezone.utc)
    if utc.microsecond:
        utc = utc.replace(microsecond=0) + timedelta(seconds=1)
    _write("schulsync-retry.service", _service(paths, "run --auto"))
    _write(RETRY, "[Unit]\nDescription=SchulSync Wiederholung\n\n[Timer]\n" +
           f"OnCalendar={utc:%Y-%m-%d %H:%M:%S} UTC\nPersistent=true\nAccuracySec=1s\n\n" +
           "[Install]\nWantedBy=timers.target\n")
    _check("daemon-reload")
    _check("enable", "--now", RETRY)
    _check("restart", RETRY)


def remove() -> AutostartStatus:
    directory = _unit_dir()
    timers = [name for name in (MAIN, GOOGLE, RETRY) if (directory / name).exists()]
    try:
        if timers:
            _check("disable", "--now", *timers)
        for name in (MAIN, GOOGLE, RETRY, "schulsync.service", "schulsync-google-login.service", "schulsync-retry.service"):
            (directory / name).unlink(missing_ok=True)
        if timers:
            _check("daemon-reload")
        return AutostartStatus(None, "systemd-Benutzertimer entfernt." if timers else "Es war kein Autostart eingerichtet. Nichts zu entfernen.", True)
    except OSError as exc:
        return AutostartStatus(None, f"systemd-Benutzertimer konnten nicht entfernt werden: {exc}", False)


def status() -> AutostartStatus:
    rc, output = _systemctl("is-enabled", MAIN)
    if rc == 0:
        return AutostartStatus("task", "Autostart ist über systemd-Benutzertimer eingerichtet.", True)
    if (_unit_dir() / MAIN).exists():
        return AutostartStatus(None, f"systemd-Benutzertimer ist nicht aktiv: {output}", False)
    return AutostartStatus(None, "Autostart ist nicht eingerichtet. Einrichten mit: schulsync autostart install", False)
