"""Process-level integration: the REAL entry point (`python -m schulsync`-equivalent) in a child process.

What is real: argument parsing, data-directory handling (SCHULSYNC_HOME), logging to files, config loading, the real
`factories.build_deps`, the real runner/engine/state/report, exit codes, and `pythonw.exe` (no stdout/stderr/stdin).
What is stubbed in the child (see LAUNCHER): the browser (`webbrowser.open` / `os.startfile` only record the path, so no
browser ever opens) and the Windows Task Scheduler query behind `status` (nothing outside the temporary directory is read
or written). LOCALAPPDATA/APPDATA point to empty temporary directories, so any access to the real user profile would show
up as a file there. No network is used: every command here ends before any host is contacted.

Nothing is exercised against Eduvidual, WebUntis or Google: those need real accounts (see docs/ACCEPTANCE_NOTES.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from schulsync import __version__

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO / "config.example.toml"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")

LAUNCHER = '''
import os, sys, webbrowser
from pathlib import Path

LOG = Path(os.environ["SCHULSYNC_TEST_OPENED"])


def _record(target, *args, **kwargs):
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(str(target) + "\\n")
    return True


webbrowser.open = webbrowser.open_new = webbrowser.open_new_tab = _record
if hasattr(os, "startfile"):
    os.startfile = _record

from schulsync import autostart

# the real scheduler is never queried; this is its measured answer for a task that does not exist (H7: exit 1 alone is
# not conclusive any more, the not-found message is part of the signal)
autostart._run = lambda args, **kw: autostart._Result(1, "ERROR: The system cannot find the file specified.")

from schulsync.cli import main

sys.exit(main(sys.argv[1:]))
'''


class Sandbox:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.localappdata = tmp_path / "localappdata"
        self.appdata = tmp_path / "appdata"
        for d in (self.localappdata, self.appdata):
            d.mkdir()
        self.opened_log = tmp_path / "opened.txt"
        self.launcher = tmp_path / "launcher.py"
        self.launcher.write_text(LAUNCHER, encoding="utf-8")

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k.upper() not in {"SCHULSYNC_HOME", "LOCALAPPDATA", "APPDATA"}}
        env.update(
            SCHULSYNC_HOME=str(self.home),
            SCHULSYNC_TEST_OPENED=str(self.opened_log),
            LOCALAPPDATA=str(self.localappdata),
            APPDATA=str(self.appdata),
            PYTHONPATH=str(REPO / "src"),
            PYTHONIOENCODING="utf-8",
        )
        return env

    def run(self, *args: str, exe: str | Path | None = None, input_text: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(exe or sys.executable), str(self.launcher), *args],
            env=self.env(),
            cwd=str(self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=input_text,
            timeout=120,
            check=False,
        )

    def install_config(self) -> Path:
        self.home.mkdir(exist_ok=True)
        target = self.home / "config.toml"
        shutil.copyfile(EXAMPLE_CONFIG, target)
        return target

    def opened(self) -> list[str]:
        return self.opened_log.read_text(encoding="utf-8").splitlines() if self.opened_log.exists() else []

    def reports(self) -> list[Path]:
        return sorted((self.home / "reports").glob("bericht-*.html")) if (self.home / "reports").exists() else []

    def log_text(self) -> str:
        logs = sorted((self.home / "logs").glob("*.log")) if (self.home / "logs").exists() else []
        return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)

    def assert_profile_untouched(self) -> None:
        assert list(self.localappdata.rglob("*")) == [], "the program wrote below LOCALAPPDATA although SCHULSYNC_HOME was set"
        assert list(self.appdata.rglob("*")) == [], "the program wrote below APPDATA"


@pytest.fixture
def box(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


# --------------------------------------------------------------------------- commands without configuration


@pytest.mark.parametrize(
    ("args", "code", "expected"),
    [
        (["--help"], 0, ["Aufruf:", "Befehle", "setup", "preview"]),
        (["--version"], 0, [f"SchulSync {__version__}"]),
        ([], 2, ["Aufruf:", "Befehle"]),
        (["bogus"], 2, ["Fehler im Aufruf"]),
        (["run", "--bogus"], 2, ["Unbekannte Argumente"]),
        (["status"], 0, ["Noch keine Daten", "Konfiguration: fehlt", "Autostart ist nicht eingerichtet", "Letzter Bericht: noch keiner"]),
        (["check"], 2, ["Konfigurationsdatei fehlt", "schulsync setup"]),
        (["preview"], 2, ["Fehler: Konfigurationsdatei fehlt", "schulsync setup"]),
        (["restore"], 2, ["Fehler: Konfigurationsdatei fehlt"]),
        (["restore", "eduvidual:assignment:assign:1"], 2, ["Fehler: Konfigurationsdatei fehlt"]),
        (["report"], 1, ["noch keinen Bericht"]),
        (["run"], 2, ["Fehler: Konfigurationsdatei fehlt"]),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, list) else None,
)
def test_commands_without_configuration_give_clear_german_messages_and_exit_codes(box, args, code, expected):
    proc = box.run(*args)
    text = proc.stdout + proc.stderr
    assert proc.returncode == code, text
    for part in expected:
        assert part in text, text
    assert "Traceback" not in text
    assert box.opened() == []  # no browser page for a plain command
    assert not (box.home / "state.sqlite3").exists()  # status/check/... never create a database behind the user's back
    box.assert_profile_untouched()


# --------------------------------------------------------------------------- run --auto: a failure is never silent


@pytest.mark.parametrize("exe", [sys.executable, pytest.param(PYTHONW, id="pythonw", marks=pytest.mark.skipif(not PYTHONW.exists(), reason="no pythonw.exe"))])
def test_run_auto_without_config_writes_a_failed_report_and_opens_it_once_per_day(box, exe):
    first = box.run("run", "--auto", exe=exe)
    assert first.returncode == 2, first.stdout + first.stderr
    assert first.stdout == "" and first.stderr == ""  # automatic start: nothing on a console (there is none under pythonw)
    reports = box.reports()
    assert len(reports) == 1
    html_text = reports[0].read_text(encoding="utf-8")
    assert "Fehlgeschlagen" in html_text and "Konfigurationsdatei fehlt" in html_text and "schulsync setup" in html_text
    low = html_text.lower()
    assert "<script" not in low and "<link" not in low and ' src="http' not in low  # self-contained, no external resources
    assert box.opened() == [reports[0].as_uri()]  # the browser opener was asked exactly once (the stub recorded the file:// URI)
    assert "Konfigurationsdatei fehlt" in box.log_text() and "Traceback" not in box.log_text()

    second = box.run("run", "--auto", exe=exe)  # same day, same error: report refreshed, no second tab
    assert second.returncode == 2
    assert box.reports() == reports and box.opened() == [reports[0].as_uri()]
    box.assert_profile_untouched()


@pytest.mark.skipif(not PYTHONW.exists(), reason="no pythonw.exe")
@pytest.mark.parametrize("args", [["-v", "run", "--auto"], ["run", "--auto", "-v"], ["status", "-v"], ["check", "-v"], ["preview", "-v"]])
def test_pythonw_without_console_never_crashes_even_with_verbose_logging(box, args):
    """sys.stdout/stderr/stdin are None under pythonw.exe: neither the console logging nor the output may raise."""
    proc = box.run(*args, exe=PYTHONW)
    expected = 0 if "status" in args else 2  # status works without a config, run/check/preview report the missing one
    assert proc.returncode == expected, (args, proc.returncode)
    assert "gestartet (SchulSync" in box.log_text()  # it ran and logged to the file although no console exists
    assert "Traceback" not in box.log_text()
    box.assert_profile_untouched()


def test_run_auto_with_a_broken_config_reports_it_too(box):
    box.home.mkdir()
    (box.home / "config.toml").write_text("das ist [kein toml", encoding="utf-8")
    proc = box.run("run", "--auto")
    assert proc.returncode == 2 and proc.stdout == ""
    assert len(box.reports()) == 1 and "kein gültiges TOML" in box.reports()[0].read_text(encoding="utf-8")
    assert box.opened() == [box.reports()[0].as_uri()]


# --------------------------------------------------------------------------- a valid config but no credentials yet (fresh install)


def test_fresh_install_with_config_but_without_credentials(box):
    box.install_config()

    status = box.run("status")
    assert status.returncode == 0 and "Konfiguration: gültig." in status.stdout and "Noch keine Daten" in status.stdout

    check = box.run("check")  # local part only: nothing is stored, Google/sources cannot even be asked
    assert check.returncode == 1, check.stdout + check.stderr
    assert "Nicht gespeichert: google_token" in check.stdout and "Nicht gespeichert: eduvidual_password" in check.stdout
    assert "Kein Benutzername für Eduvidual" in check.stdout and "schulsync setup" in check.stdout
    assert "Traceback" not in check.stdout + check.stderr

    run = box.run("run")  # manual: no network wait, both sources fail at the credential step, nothing is written anywhere
    assert run.returncode == 1, run.stdout + run.stderr
    assert "Fehlgeschlagen" in run.stdout and "kein Benutzername" in run.stdout and "schulsync setup --eduvidual" in run.stdout
    (report,) = box.reports()
    assert "schulsync setup --webuntis" in report.read_text(encoding="utf-8")
    assert box.opened() == [report.as_uri()]

    preview = box.run("preview")  # never writes or books anything; its own report file, plus the opener stub
    assert preview.returncode == 1, preview.stdout + preview.stderr
    assert "Es wurde nichts bei Google geschrieben und kein Tageserfolg verbucht." in preview.stdout
    assert (box.home / "reports" / "vorschau.html").is_file() and box.reports() == [report]
    assert box.opened()[-1].endswith("vorschau.html")

    restore = box.run("restore")
    assert restore.returncode == 0 and "keine bewusst gelöschten Einträge" in restore.stdout

    shown = box.run("report")
    assert shown.returncode == 0 and "geöffnet" in shown.stdout and box.opened()[-1].endswith("/latest.html")
    assert (box.home / "reports" / "latest.html").read_text(encoding="utf-8") == report.read_text(encoding="utf-8")  # the newest run, not the preview

    after = box.run("status")
    assert after.returncode == 0 and "Letzter Lauf:" in after.stdout and "fehlgeschlagen" in after.stdout
    assert "Ausstehende Google-Schreibvorgänge: 0" in after.stdout
    assert "Anmeldung abgelehnt" not in after.stdout  # missing credentials are not a rejected login: no auth block
    box.assert_profile_untouched()

