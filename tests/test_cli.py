"""cli.py: every command through injected I/O. No network, no browser, no real secret store, no real runner.

`runner.run` / `runner.restore` are replaced by fakes (the engine has its own tests); Google and the schools are
FakeGoogle / FakeAdapter / small fake service objects.
"""

from __future__ import annotations

import io as io_module
import json
import shutil
import sys
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from google.oauth2.credentials import Credentials

from schulsync import cli, google_auth, runner
from schulsync.autostart import AutostartStatus
from schulsync.cli import CliEnv, main
from schulsync.config import Config, Paths, load_config, set_config_value
from schulsync.errors import ConfigError, ErrorInfo, GoogleAuthError, NetworkError
from schulsync.lock import LockHeldError
from schulsync.logging_setup import shutdown_logging
from schulsync.models import (
    Category,
    CategoryOutcome,
    CategoryStatus,
    EventState,
    ItemState,
    OverallStatus,
    ProbeReport,
    ProbeStep,
    RunMode,
    Source,
    TaskState,
)
from schulsync.redact import clear_registered_secrets, register_secret
from schulsync.secrets_store import MemorySecretStore
from schulsync.state import StateStore
from tests.support import samples
from tests.support.fake_adapter import FakeAdapter
from tests.support.fake_google import FakeGoogle

PW_EDU = "hunter2-dummy-edu"
PW_UNTIS = "hunter2-dummy-untis"
CLIENT_SECRET = "GOCSPX-dummy-client-secret"
REFRESH = "1//dummy-refresh-token"
ACCOUNT = "schueler.beispiel@gmail.com"


# --------------------------------------------------------------------------- harness


class UnexpectedQuestion(BaseException):
    """BaseException on purpose: the command's own `except Exception` must not swallow a test failure."""


class Io:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.answers: deque[str] = deque()
        self.secret_answers: deque[str] = deque()
        self.prompts: list[str] = []

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise UnexpectedQuestion(prompt)
        return self.answers.popleft()

    def ask_secret(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.secret_answers:
            raise UnexpectedQuestion(prompt)
        return self.secret_answers.popleft()

    @property
    def out(self) -> str:
        return "\n".join(self.lines)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeAutostart:
    def __init__(self, installed: bool = False, ok: bool = True):
        self.installed, self.ok = installed, ok
        self.calls: list[tuple] = []

    def install(self, paths):
        self.calls.append(("install", paths))
        self.installed = True
        return AutostartStatus("task", "Autostart eingerichtet (Fake).", self.ok)

    def remove(self):
        self.calls.append(("remove",))
        self.installed = False
        return AutostartStatus(None, "Autostart entfernt (Fake).", self.ok)

    def status(self):
        self.calls.append(("status",))
        if self.installed:
            return AutostartStatus("task", "Autostart ist eingerichtet (Fake).", True)
        return AutostartStatus(None, "Autostart ist nicht eingerichtet (Fake).", False)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    clear_registered_secrets()
    monkeypatch.delenv("SCHULSYNC_HOME", raising=False)
    yield
    shutdown_logging()
    clear_registered_secrets()


@pytest.fixture
def io() -> Io:
    return Io()


@pytest.fixture
def clock(now) -> Clock:
    return Clock(now)


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
def opened() -> list[Path]:
    return []


@pytest.fixture
def autostart_fake() -> FakeAutostart:
    return FakeAutostart()


@pytest.fixture
def env(paths, io, clock, store, opened, autostart_fake) -> CliEnv:
    def refuse(*a, **k):
        raise UnexpectedQuestion("unerwarteter Aufruf")

    return CliEnv(
        say=io.say,
        ask=io.ask,
        ask_secret=io.ask_secret,
        clock=clock,
        paths=paths,
        secrets=store,
        build_deps=refuse,
        make_adapter=refuse,
        google_services=refuse,
        authorize=refuse,
        open_report=lambda p: opened.append(p) or True,
        autostart=autostart_fake,
    )


@pytest.fixture
def config_file(paths) -> Path:
    cli._write_default_config(paths.config)
    return paths.config


def make_deps(paths: Paths, config: Config, **kw) -> runner.Deps:
    kw.setdefault("adapter_factories", {})
    kw.setdefault("gateway_factory", lambda: FakeGoogle())
    return runner.Deps(paths=paths, config=config, **kw)


def outcome(code=0, text="", silent=False, path=None) -> runner.RunOutcome:
    return runner.RunOutcome(exit_code=code, report=None, report_path=path, console_text=text, silent=silent)


def all_logs(paths: Paths) -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(paths.logs.glob("*.log")))


# --------------------------------------------------------------------------- parser, help, version, --home


def test_version_and_help_are_german(env, io):
    assert main(["--version"], env=env) == 0
    assert "SchulSync 0.1.0" in io.out
    assert main(["--help"], env=env) == 0
    for word in ("Aufruf:", "Befehle:", "Optionen:", "setup", "run", "preview", "check", "status", "restore", "report", "autostart", "--home"):
        assert word in io.out
    assert "usage:" not in io.out and "options:" not in io.out


def test_subcommand_help_and_usage_errors(env, io):
    assert main(["run", "--help"], env=env) == 0
    assert "--auto" in io.out and "--force" in io.out and "Tagesbegrenzung" in io.out
    io.lines.clear()
    assert main(["gibtsnicht"], env=env) == 2
    assert "Fehler im Aufruf" in io.out and "Ungültige Auswahl" in io.out
    io.lines.clear()
    assert main(["autostart"], env=env) == 2
    assert "Es fehlt" in io.out
    io.lines.clear()
    assert main(["status", "--quatsch"], env=env) == 2
    assert "Unbekannte Argumente" in io.out


def test_no_command_prints_help_and_returns_2(env, io):
    assert main([], env=env) == 2
    assert "Befehle:" in io.out


def test_home_option_selects_the_data_directory(env, io, tmp_path):
    env.paths = None
    home = tmp_path / "Mein Ordner"
    assert main(["--home", str(home), "status"], env=env) == 0
    assert (home / "logs").is_dir() and str(home) in io.out


def test_home_option_is_accepted_behind_the_subcommand_and_beats_the_environment(env, tmp_path, monkeypatch):
    # the autostart appends `--home <dir>` to `run --auto`
    env.paths = None
    seen = []
    monkeypatch.setenv("SCHULSYNC_HOME", str(tmp_path / "ausUmgebung"))
    monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: seen.append((deps.paths.home, mode)) or outcome(0, silent=True))
    home = tmp_path / "Arg Ordner"
    Paths(home).ensure()
    cli._write_default_config(Paths(home).config)
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run", "--auto", "--home", str(home)], env=env) == 0
    assert seen == [(home, RunMode.AUTO)]


def test_environment_variable_is_used_without_home_option(env, io, tmp_path, monkeypatch):
    env.paths = None
    monkeypatch.setenv("SCHULSYNC_HOME", str(tmp_path / "ausUmgebung"))
    assert main(["status"], env=env) == 0
    assert str(tmp_path / "ausUmgebung") in io.out


# --------------------------------------------------------------------------- pythonw: no console at all


def test_no_console_never_crashes(paths, clock, store, opened, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdin", None)
    env = CliEnv(paths=paths, secrets=store, clock=clock, open_report=lambda p: opened.append(p) or True, autostart=FakeAutostart())
    assert main(["--help"], env=env) == 0
    assert main(["status"], env=env) == 0
    assert main(["bogus"], env=env) == 2
    # automatic run with a missing config: failure report + browser, in -v mode too (stderr is None)
    assert main(["run", "--auto", "-v"], env=env) == 2
    assert len(opened) == 1 and opened[0].is_file()
    # an interactive command without a console is a clean configuration error
    assert main(["setup"], env=env) == 2


def test_default_say_survives_unencodable_characters(monkeypatch):
    class Narrow:
        encoding = "ascii"

        def __init__(self):
            self.parts = []

        def write(self, text):
            text.encode("ascii")
            self.parts.append(text)

        def flush(self):
            pass

    stream = Narrow()
    monkeypatch.setattr(sys, "stdout", stream)
    cli._default_say("Prüfung läuft")
    assert "".join(stream.parts).startswith("Pr")


# --------------------------------------------------------------------------- run


def test_run_manual_passes_mode_force_and_prints(env, io, config_file, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: calls.append((deps, mode, force)) or outcome(1, "Teilweise fehlgeschlagen."))
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run", "--force"], env=env) == 1
    assert len(calls) == 1 and calls[0][1] == RunMode.MANUAL and calls[0][2] is True
    assert io.out == "Teilweise fehlgeschlagen."
    calls.clear()
    assert main(["run"], env=env) == 1
    assert calls[0][2] is False


def test_run_auto_success_prints_and_opens_nothing(env, io, config_file, monkeypatch, opened):
    monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: outcome(0, "Alles erledigt.", silent=False))
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run", "--auto"], env=env) == 0
    assert io.lines == [] and opened == []
    assert list(env.paths.reports.glob("*.html")) == []  # the CLI wrote no failure report


def test_run_auto_with_problems_prints_the_runner_text(env, io, config_file, monkeypatch):
    monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: outcome(1, "Eduvidual fehlgeschlagen."))
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run", "--auto"], env=env) == 1
    assert "Eduvidual fehlgeschlagen." in io.out


def test_run_auto_lock_held_exits_quietly(env, io, config_file, monkeypatch, opened):
    def locked(deps, mode, *, force=False):
        raise LockHeldError("SchulSync läuft bereits.")

    monkeypatch.setattr(runner, "run", locked)
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run", "--auto"], env=env) == 0
    assert io.lines == [] and opened == []


def test_run_manual_errors_are_german_without_report_or_browser(env, io, config_file, monkeypatch, opened):
    monkeypatch.setattr(runner, "run", lambda *a, **k: (_ for _ in ()).throw(ConfigError("Kaputt.", hint="Bitte reparieren.")))
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run"], env=env) == 2
    assert "Fehler: Kaputt." in io.out and "Hinweis: Bitte reparieren." in io.out
    assert opened == [] and list(env.paths.reports.glob("*.html")) == []


def test_run_manual_unexpected_exception_is_reported_without_details(env, io, config_file, monkeypatch, paths):
    register_secret(PW_EDU)
    monkeypatch.setattr(runner, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError(f"boom {PW_EDU}")))
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run"], env=env) == 1
    assert "Unerwarteter Fehler (ValueError)" in io.out and "Logdatei" in io.out
    assert PW_EDU not in io.out and PW_EDU not in all_logs(paths)
    assert "Traceback" in all_logs(paths)  # the details are in the log, redacted


def test_keyboard_interrupt_is_a_clean_exit(env, io, config_file, monkeypatch):
    monkeypatch.setattr(runner, "run", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run"], env=env) == 1
    assert "Abgebrochen" in io.out


# --------------------------------------------------------------------------- run --auto: never fails silently


def _break_config(paths: Paths) -> None:
    paths.config.write_text("das ist [kein toml", encoding="utf-8")


def _crash(monkeypatch, env, exc):
    def boom(deps, mode, *, force=False):
        raise exc

    monkeypatch.setattr(runner, "run", boom)
    env.build_deps = lambda p, c, s: make_deps(p, c)


FAILURES = {
    "config_missing": (2, "Konfigurationsdatei fehlt", lambda paths, env, mp: None),
    "config_broken": (2, "kein gültiges TOML", lambda paths, env, mp: _break_config(paths)),
    "crash": (1, "Unerwarteter Fehler (RuntimeError)", lambda paths, env, mp: (cli._write_default_config(paths.config), _crash(mp, env, RuntimeError("kaputt")))),
    "secrets_broken": (
        2,
        "Zugangsdaten ist beschädigt",
        lambda paths, env, mp: (
            cli._write_default_config(paths.config),
            setattr(env, "build_deps", lambda p, c, s: (_ for _ in ()).throw(ConfigError("Die Datei mit den gespeicherten Zugangsdaten ist beschädigt.", hint="Bitte 'schulsync setup' erneut ausführen."))),
        ),
    ),
}


@pytest.mark.parametrize("name", list(FAILURES))
def test_run_auto_failure_writes_report_logs_and_opens_browser_once_per_day(name, env, io, paths, monkeypatch, opened, clock):
    code, text, prepare = FAILURES[name]
    prepare(paths, env, monkeypatch)
    assert main(["run", "--auto"], env=env) == code
    report_file = paths.reports / "bericht-2026-10-14.html"
    assert opened == [report_file] and report_file.is_file()
    html_text = report_file.read_text(encoding="utf-8")
    assert "Fehlgeschlagen" in html_text and text.split()[0] in html_text
    assert "Fehler (" in all_logs(paths) or "Unerwarteter Fehler" in all_logs(paths)
    # same error again the same day: report refreshed, no second tab
    report_file.unlink()
    assert main(["run", "--auto"], env=env) == code
    assert report_file.is_file() and len(opened) == 1
    # next day: opened again
    clock.now = clock.now + timedelta(days=1)
    assert main(["run", "--auto"], env=env) == code
    assert len(opened) == 2 and opened[1].name == "bericht-2026-10-15.html"


def test_run_auto_a_different_error_the_same_day_opens_again(env, paths, monkeypatch, opened):
    assert main(["run", "--auto"], env=env) == 2  # config missing
    cli._write_default_config(paths.config)
    _crash(monkeypatch, env, RuntimeError("kaputt"))
    assert main(["run", "--auto"], env=env) == 1
    assert len(opened) == 2


def test_run_auto_failure_report_is_not_opened_when_open_on_is_never_but_still_written(env, paths, monkeypatch, opened, config_file):
    set_config_value(paths.config, "report", "open_on", "never")
    _crash(monkeypatch, env, RuntimeError("kaputt"))
    assert main(["run", "--auto"], env=env) == 1
    assert opened == [] and (paths.reports / "bericht-2026-10-14.html").is_file()


def test_run_auto_failed_browser_open_is_retried_next_time(env, paths):
    attempts = []
    env.open_report = lambda p: attempts.append(p) or False
    assert main(["run", "--auto"], env=env) == 2
    assert main(["run", "--auto"], env=env) == 2
    assert len(attempts) == 2 and (paths.reports / "bericht-2026-10-14.html").is_file()


def test_run_auto_failure_never_leaks_secrets(env, paths, monkeypatch, config_file):
    register_secret(PW_EDU)
    _crash(monkeypatch, env, RuntimeError(f"Anmeldung mit {PW_EDU} gescheitert"))
    assert main(["run", "--auto"], env=env) == 1
    assert PW_EDU not in (paths.reports / "bericht-2026-10-14.html").read_text(encoding="utf-8")
    assert PW_EDU not in all_logs(paths)


def test_run_auto_report_file_failure_does_not_crash(env, io, paths, monkeypatch, opened):
    monkeypatch.setattr(cli, "write_report", lambda *a, **k: (_ for _ in ()).throw(OSError("Datenträger voll")))
    assert main(["run", "--auto"], env=env) == 2
    assert opened == [] and "Konfigurationsdatei fehlt" in io.out


# --------------------------------------------------------------------------- preview / restore / report / autostart


def test_preview_runs_in_preview_mode_and_opens_the_preview_report(env, io, paths, config_file, monkeypatch):
    shown = []
    preview_file = paths.reports / "vorschau.html"
    calls = []
    monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: calls.append((mode, force)) or outcome(0, "2 Änderungen geplant.", path=preview_file))
    env.build_deps = lambda p, c, s: make_deps(p, c, open_report=lambda path: shown.append(path) or True)
    assert main(["preview"], env=env) == 0
    assert calls == [(RunMode.PREVIEW, False)]
    assert shown == [preview_file]
    assert "2 Änderungen geplant." in io.out and "nichts bei Google geschrieben" in io.out


def test_preview_tells_when_the_browser_cannot_be_opened(env, io, paths, config_file, monkeypatch):
    preview_file = paths.reports / "vorschau.html"
    monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: outcome(0, "ok", path=preview_file))
    env.build_deps = lambda p, c, s: make_deps(p, c, open_report=lambda path: False)
    assert main(["preview"], env=env) == 0
    assert "nicht im Browser geöffnet" in io.out and str(preview_file) in io.out


@pytest.mark.parametrize("argv, expected", [(["restore"], None), (["restore", "webuntis:homework:hw:1", "eduvidual:assignment:assign:2"], ["webuntis:homework:hw:1", "eduvidual:assignment:assign:2"])])
def test_restore_passes_uids_through(argv, expected, env, io, config_file, monkeypatch):
    seen = []
    monkeypatch.setattr(runner, "restore", lambda deps, uids: seen.append(uids) or outcome(0, "Wiederhergestellt."))
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(argv, env=env) == 0
    assert seen == [expected] and "Wiederhergestellt." in io.out


def test_restore_exit_code_comes_from_the_runner(env, io, config_file, monkeypatch):
    monkeypatch.setattr(runner, "restore", lambda deps, uids: outcome(1, "Unbekannte Kennung."))
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["restore", "x:y:z"], env=env) == 1


def test_restore_without_config_is_a_config_error(env, io):
    assert main(["restore"], env=env) == 2
    assert "Konfigurationsdatei fehlt" in io.out and "schulsync setup" in io.out


def test_report_without_any_report(env, io, opened):
    assert main(["report"], env=env) == 1
    assert "noch keinen Bericht" in io.out and opened == []


def test_report_opens_the_latest_one(env, io, paths, opened):
    (paths.reports / "latest.html").write_text("<html><title>x</title></html>", encoding="utf-8")
    assert main(["report"], env=env) == 0
    assert opened == [paths.reports / "latest.html"]


def test_report_open_failure_names_the_file(env, io, paths):
    (paths.reports / "latest.html").write_text("<html></html>", encoding="utf-8")
    env.open_report = lambda p: False
    assert main(["report"], env=env) == 1
    assert str(paths.reports / "latest.html") in io.out


@pytest.mark.parametrize("action, expected_call, ok, code", [
    ("install", "install", True, 0),
    ("remove", "remove", True, 0),
    ("status", "status", True, 0),
    ("install", "install", False, 1),
    ("status", "status", False, 1),
])
def test_autostart_commands(action, expected_call, ok, code, env, io, autostart_fake):
    autostart_fake.ok = ok
    autostart_fake.installed = action == "status" and ok
    assert main(["autostart", action], env=env) == code
    assert autostart_fake.calls[0][0] == expected_call
    assert "(Fake)" in io.out


def test_autostart_install_gets_the_paths(env, paths, autostart_fake):
    main(["autostart", "install"], env=env)
    assert autostart_fake.calls == [("install", paths)]


# --------------------------------------------------------------------------- status


def test_status_on_an_empty_home_works_without_config_network_or_database(env, io, paths):
    assert main(["status"], env=env) == 0
    assert "Noch keine Daten" in io.out
    assert "fehlt oder ist fehlerhaft" in io.out
    assert "Autostart ist nicht eingerichtet (Fake)" in io.out
    assert "Letzter Bericht: noch keiner" in io.out
    assert not paths.db.exists()  # status never creates a database


def test_status_shows_runs_sources_blocks_pending_and_deleted_entries(env, io, paths, config_file, now, autostart_fake):
    autostart_fake.installed = True
    db = StateStore(paths.db)
    db.record_run(now, now + timedelta(minutes=2), now.date(), RunMode.AUTO, OverallStatus.PARTIAL)
    db.record_fetch(now.date(), Source.EDUVIDUAL, now, True)
    db.record_writes_done(now.date(), Source.EDUVIDUAL, now)
    db.record_fetch(now.date(), Source.WEBUNTIS, now, True)  # fetched, writes still open
    db.set_auth_blocked(Source.WEBUNTIS, now, "auth")
    deleted = ItemState(
        item=samples.assignment(title="Laborprotokoll 2"), item_hash="h", cancelled=False, first_seen_at=now, last_seen_at=now,
        event_id="ss1", event_state=EventState.USER_DELETED, event_applied_hash="x", dirty=False, task_state=TaskState.USER_DELETED,
    )
    pending = ItemState(item=samples.homework(), item_hash="h2", cancelled=False, first_seen_at=now, last_seen_at=now)
    db.upsert(deleted, now)
    db.upsert(pending, now)
    db.close()
    (paths.reports / "latest.html").write_text("<html></html>", encoding="utf-8")
    assert main(["status"], env=env) == 0
    out = io.out
    assert "Letzter Lauf: 14.10.2026 08:02 Uhr (automatisch), Ergebnis: teilweise fehlgeschlagen." in out
    assert "Eduvidual: letzter vollständiger Abruf 14.10.2026 08:00 Uhr; heute erledigt." in out
    assert "WebUntis:" in out and "Google-Schreibvorgänge stehen noch aus" in out
    assert "Anmeldung abgelehnt seit 14.10.2026 08:00 Uhr" in out and "schulsync setup --webuntis" in out
    assert "Ausstehende Google-Schreibvorgänge: 1" in out
    assert "eduvidual:assignment:assign:40213 - Laborprotokoll 2 (Termin und Aufgabe von dir gelöscht)" in out
    assert "schulsync restore" in out
    assert "Autostart ist eingerichtet (Fake)" in out
    assert "latest.html" in out


def test_every_command_writes_to_the_log_file(env, paths, io):
    assert main(["status"], env=env) == 0
    assert main(["report"], env=env) == 1
    log_text = all_logs(paths)
    assert "Befehl 'status' gestartet" in log_text and "Befehl 'report' beendet mit Exit-Code 1" in log_text


def test_verbose_adds_log_lines_to_the_console_stream(env, paths, monkeypatch):
    stream = io_module.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    assert main(["-v", "status"], env=env) == 0
    assert "Befehl 'status' gestartet" in stream.getvalue()


def test_status_survives_a_failing_autostart_query(env, io):
    env.autostart = type("Broken", (), {"status": staticmethod(lambda: (_ for _ in ()).throw(OSError("schtasks")))})()
    assert main(["status"], env=env) == 0
    assert "konnte nicht ermittelt werden" in io.out


def test_status_with_a_corrupt_database_is_a_german_error(env, io, paths):
    paths.db.write_bytes(b"das ist keine datenbank" * 50)
    assert main(["status"], env=env) == 2
    assert "Zustandsdatenbank" in io.out and "Hinweis:" in io.out


# --------------------------------------------------------------------------- check


def probe_ok(source: Source, count: int = 3) -> ProbeReport:
    cats = {
        Source.EDUVIDUAL: {Category.ASSIGNMENTS: count},
        Source.WEBUNTIS: {Category.HOMEWORK: count, Category.EXAMS: count, Category.CANCELLATIONS: count},
    }[source]
    return ProbeReport(
        source,
        [ProbeStep("Erreichbarkeit", True, "antwortet"), ProbeStep("Anmeldung", True, "Anmeldung erfolgreich.")],
        {c: CategoryOutcome(c, CategoryStatus.OK, item_count=n) for c, n in cats.items()},
    )


def check_env(env, paths, store, *, adapters, gateway=None, config_changes=None):
    cfg = load_config(paths.config)
    if config_changes:
        cfg = config_changes(cfg)
    store.set(google_auth.SECRET_TOKEN, json.dumps({"refresh_token": REFRESH}))
    store.set("eduvidual_password", PW_EDU)
    store.set("webuntis_password", PW_UNTIS)
    deps = make_deps(
        paths, cfg,
        adapter_factories={s: (lambda a=a: a) for s, a in adapters.items()},
        gateway_factory=(lambda: gateway) if gateway is not None else (lambda: FakeGoogle()),
    )
    env.build_deps = lambda p, c, s: deps
    return deps


def with_usernames(paths):
    set_config_value(paths.config, "eduvidual", "username", "schueler.edu")
    set_config_value(paths.config, "webuntis", "username", "schueler.untis")


def test_check_all_ok(env, io, paths, config_file, store):
    with_usernames(paths)
    google = FakeGoogle()
    check_env(env, paths, store, gateway=google, adapters={
        Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)),
        Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS, 2)),
    })
    assert main(["check"], env=env) == 0
    out = io.out
    assert "Konfiguration gültig" in out and "Alles in Ordnung" in out
    assert "Gespeichert: google_token" in out and "Gespeichert: eduvidual_password" in out and "Gespeichert: webuntis_password" in out
    assert "Kalender 'Schule' ist erreichbar" in out and "nicht der Hauptkalender" in out and "Aufgabenliste 'Schule'" in out
    assert "Aufgaben und Abgaben: OK (3 Einträge im Prüffenster)" in out
    assert "Hausaufgaben: OK (2" in out and "Prüfungen und Schularbeiten: OK (2" in out and "Unterrichtsentfälle: OK (2" in out
    assert "[OK] Anmeldung: Anmeldung erfolgreich." in out
    assert PW_EDU not in out and PW_UNTIS not in out and REFRESH not in out
    assert google.write_log == [] and google.calls["check_targets"] == 1  # nothing is written


def test_check_mixed_results_exit_1_with_per_category_coverage(env, io, paths, config_file, store):
    with_usernames(paths)
    broken = ProbeReport(
        Source.WEBUNTIS,
        [
            ProbeStep("Erreichbarkeit", True, "WebUntis antwortet."),
            ProbeStep("Anmeldung", True, "Angemeldet."),
            ProbeStep("Hausaufgaben", True, "2 gelesen"),
            ProbeStep("Prüfungen", False, "Struktur unerwartet"),
        ],
        {
            Category.HOMEWORK: CategoryOutcome(Category.HOMEWORK, CategoryStatus.OK, item_count=2),
            Category.EXAMS: CategoryOutcome(Category.EXAMS, CategoryStatus.FAILED, ErrorInfo("schema", "Antwort hat sich geändert.")),
            Category.CANCELLATIONS: CategoryOutcome(Category.CANCELLATIONS, CategoryStatus.INCOMPLETE, detail="Seite 2 fehlt"),
        },
    )
    check_env(env, paths, store, adapters={
        Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)),
        Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=broken),
    })
    assert main(["check"], env=env) == 1
    out = io.out
    assert "Aufgaben und Abgaben: OK (3" in out
    assert "Hausaufgaben: OK (2" in out
    assert "Prüfungen und Schularbeiten: FEHLGESCHLAGEN" in out and "Antwort hat sich geändert." in out
    assert "Unterrichtsentfälle: UNVOLLSTÄNDIG" in out and "Seite 2 fehlt" in out
    assert "[FEHLER] Prüfungen: Struktur unerwartet" in out
    assert "Es gibt Probleme" in out and "Nächste Schritte" in out


def test_check_google_failure_does_not_hide_the_sources(env, io, paths, config_file, store):
    with_usernames(paths)
    check_env(env, paths, store, gateway=FakeGoogle(calendar_is_primary=True), adapters={
        Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)),
        Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS)),
    })
    assert main(["check"], env=env) == 1
    assert "[FEHLER]" in io.out and "Hauptkalender" in io.out
    assert "Aufgaben und Abgaben: OK" in io.out  # sources were still probed
    assert "schulsync setup --google" in io.out


def test_check_gateway_auth_error_is_reported(env, io, paths, config_file, store):
    with_usernames(paths)
    deps = check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)),
                                                   Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS))})
    deps.gateway_factory = lambda: (_ for _ in ()).throw(GoogleAuthError("Die Google-Anmeldung ist abgelaufen.", hint="Bitte 'schulsync setup' erneut ausführen."))
    assert main(["check"], env=env) == 1
    assert "Die Google-Anmeldung ist abgelaufen." in io.out


def test_check_a_crashing_probe_does_not_stop_the_other_probes(env, io, paths, config_file, store):
    with_usernames(paths)
    boom = FakeAdapter(Source.EDUVIDUAL)
    boom.check = lambda: (_ for _ in ()).throw(RuntimeError(f"kaputt {PW_EDU}"))
    register_secret(PW_EDU)
    check_env(env, paths, store, adapters={Source.EDUVIDUAL: boom, Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS))})
    assert main(["check"], env=env) == 1
    assert "Unerwarteter Fehler (RuntimeError)" in io.out and "Hausaufgaben: OK" in io.out
    assert PW_EDU not in io.out + all_logs(paths)


def test_check_missing_password_and_secret_names_only(env, io, paths, config_file, store):
    with_usernames(paths)
    deps = check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL))})
    store.delete("webuntis_password")
    deps.adapter_factories[Source.WEBUNTIS] = lambda: (_ for _ in ()).throw(ConfigError("Für WebUntis ist kein Passwort gespeichert.", hint="Bitte 'schulsync setup --webuntis' ausführen."))
    assert main(["check"], env=env) == 1
    assert "Nicht gespeichert: webuntis_password" in io.out
    assert "Für WebUntis ist kein Passwort gespeichert." in io.out
    assert PW_EDU not in io.out


def test_check_missing_username_is_flagged(env, io, paths, config_file, store):
    check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)),
                                          Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS))})
    assert main(["check"], env=env) == 1
    assert "Kein Benutzername für Eduvidual" in io.out


def test_check_disabled_source_is_not_probed(env, io, paths, config_file, store):
    with_usernames(paths)
    set_config_value(paths.config, "webuntis", "enabled", False)
    check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL))})
    assert main(["check"], env=env) == 0
    assert "abgeschaltet" in io.out and "webuntis_password" not in io.out


def test_check_with_broken_config_is_a_config_error(env, io, paths):
    _break_config(paths)
    assert main(["check"], env=env) == 2
    assert "[FEHLER] Konfiguration" in io.out


def test_check_unblocks_a_source_whose_login_works_again(env, io, paths, config_file, store, now):
    with_usernames(paths)
    db = StateStore(paths.db)
    db.set_auth_blocked(Source.EDUVIDUAL, now, "auth")
    db.set_auth_blocked(Source.WEBUNTIS, now, "auth")
    db.close()
    bad_login = ProbeReport(Source.WEBUNTIS, [ProbeStep("Erreichbarkeit", True, "ok"), ProbeStep("Anmeldung", False, "Anmeldung abgelehnt.")])
    check_env(env, paths, store, adapters={
        Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)),
        Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=bad_login),
    })
    assert main(["check"], env=env) == 1
    assert "Sperre automatischer Läufe wurde aufgehoben" in io.out
    db = StateStore(paths.db)
    try:
        assert not db.is_auth_blocked(Source.EDUVIDUAL)
        assert db.is_auth_blocked(Source.WEBUNTIS)  # login still rejected: stays blocked
    finally:
        db.close()


def test_check_does_not_create_a_database(env, io, paths, config_file, store):
    with_usernames(paths)
    check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)),
                                          Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS))})
    assert main(["check"], env=env) == 0
    assert not paths.db.exists()


# --------------------------------------------------------------------------- setup: fakes


class Req:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=0):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class CalService:
    def __init__(self, entries, primary_id=ACCOUNT):
        self.entries, self.primary_id = entries, primary_id

    def calendarList(self):
        return self

    def list(self, **kw):
        return Req({"items": self.entries})

    def get(self, calendarId):
        assert calendarId == "primary"
        return Req({"id": self.primary_id, "summary": self.primary_id, "primary": True, "accessRole": "owner"})


class TaskService:
    def __init__(self, lists=()):
        self.lists, self.inserted = list(lists), []

    def tasklists(self):
        return self

    def list(self, **kw):
        return Req({"items": self.lists})

    def insert(self, body):
        self.inserted.append(body)
        self.lists.append({"id": "L-neu", "title": body["title"]})  # like Google: the next listing contains it
        return Req({"id": "L-neu", "title": body["title"]})


def cal(id_, summary="Schule", role="owner", **kw):
    return {"id": id_, "summary": summary, "accessRole": role, **kw}


def creds() -> Credentials:
    return Credentials(
        token="ya29.dummy-access", refresh_token=REFRESH, token_uri="https://oauth2.googleapis.com/token",
        client_id="id.apps.googleusercontent.com", client_secret=CLIENT_SECRET, scopes=list(google_auth.SCOPES),
    )


@dataclass
class SetupWorld:
    env: CliEnv
    io: Io
    paths: Paths
    store: MemorySecretStore
    client_file: Path
    authorized: list
    adapters: dict
    checked: list
    cal_service: CalService
    task_service: TaskService


@pytest.fixture
def world(env, io, paths, store, tmp_path, autostart_fake) -> SetupWorld:
    client_file = tmp_path / "client_secret_123.json"
    client_file.write_text(json.dumps({"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": CLIENT_SECRET}}), encoding="utf-8")
    authorized, checked = [], []
    cal_service = CalService([cal("p@x", "Privat"), cal("cal-1", "Schule")])
    task_service = TaskService([{"id": "L-alt", "title": "Anderes"}])
    adapters = {
        Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)),
        Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS)),
    }

    def make_adapter(source, config, secrets):
        checked.append((source, getattr(config, source.value).username, secrets.get(f"{source.value}_password")))
        return adapters[source]

    env.authorize = lambda client_json: authorized.append(client_json) or creds()
    env.google_services = lambda c, cfg: (cal_service, task_service)
    env.make_adapter = make_adapter
    return SetupWorld(env, io, paths, store, client_file, authorized, adapters, checked, cal_service, task_service)


def happy_answers(w: SetupWorld, *, autostart: str = "j") -> None:
    w.io.answers.extend([f'"{w.client_file}"', "schueler.edu", "", "schueler.untis", "", autostart])
    w.io.secret_answers.extend([PW_EDU, PW_UNTIS])


def test_setup_happy_path(world, paths, store, autostart_fake, now):
    w = world
    happy_answers(w)
    db = StateStore(paths.db)
    db.set_auth_blocked(Source.EDUVIDUAL, now, "auth")
    db.set_auth_blocked(Source.WEBUNTIS, now, "auth")
    db.close()
    w.adapters[Source.WEBUNTIS].check_report = ProbeReport(Source.WEBUNTIS, [ProbeStep("Anmeldung", False, "Anmeldung abgelehnt.")])

    assert main(["setup"], env=w.env) == 0

    # config.toml from the repo example, with the discovered ids and the user names
    cfg = load_config(paths.config)
    assert cfg.google.calendar_id == "cal-1" and cfg.google.tasklist_id == "L-neu"
    assert cfg.eduvidual.username == "schueler.edu" and cfg.webuntis.username == "schueler.untis"
    assert "Beispielkonfiguration" in paths.config.read_text(encoding="utf-8")  # copied from config.example.toml
    # secrets stored under the agreed names, token carries the refresh token
    assert sorted(store.names()) == ["eduvidual_password", "google_client", "google_token", "webuntis_password"]
    assert store.get("eduvidual_password") == PW_EDU and store.get("webuntis_password") == PW_UNTIS
    assert json.loads(store.get("google_client"))["installed"]["client_secret"] == CLIENT_SECRET
    assert json.loads(store.get("google_token"))["refresh_token"] == REFRESH
    assert w.authorized == [store.get("google_client")]
    assert w.task_service.inserted == [{"title": "Schule"}]
    # adapters were tested with the new values, auth block lifted only where the login worked
    assert w.checked == [(Source.EDUVIDUAL, "schueler.edu", PW_EDU), (Source.WEBUNTIS, "schueler.untis", PW_UNTIS)]
    db = StateStore(paths.db)
    try:
        assert not db.is_auth_blocked(Source.EDUVIDUAL) and db.is_auth_blocked(Source.WEBUNTIS)
    finally:
        db.close()
    assert [c[0] for c in autostart_fake.calls] == ["status", "install"]
    # 7-day explanation, next steps, and nothing secret anywhere
    out = w.io.out
    assert "'Testing'" in out and "7 Tagen" in out and "In Produktion" in out
    assert "schulsync check" in out and "nicht der Hauptkalender" in out
    # ANFORDERUNGEN 7: foreign entries stay untouched, and an old school automation that still runs is pointed out
    assert "alte Schulautomation" in out and "doppelte Einträge" in out and "fremde Einträge" in out
    haystack = out + "\n".join(w.io.prompts) + all_logs(paths)
    for secret in (PW_EDU, PW_UNTIS, CLIENT_SECRET, REFRESH, "ya29.dummy-access"):
        assert secret not in haystack
    assert all("getpass" not in p for p in w.io.prompts) and any("unsichtbar" in p for p in w.io.prompts)


def test_setup_is_idempotent_and_keeps_values_on_empty_input(world, paths, store, autostart_fake):
    w = world
    happy_answers(w)
    assert main(["setup"], env=w.env) == 0
    stored = {n: store.get(n) for n in store.names()}
    config_text = paths.config.read_text(encoding="utf-8")
    w.authorized.clear()
    w.checked.clear()
    autostart_fake.calls.clear()

    # second run: everything empty = keep; do not re-authorize, do not re-test, do not ask for autostart (installed)
    w.io.answers.extend(["", "", "", "n", "", "n"])  # client file, re-login?, user name, test?, user name, test?
    w.io.secret_answers.extend(["", ""])
    assert main(["setup"], env=w.env) == 0
    assert w.authorized == [] and w.checked == []
    assert {n: store.get(n) for n in store.names()} == stored
    assert paths.config.read_text(encoding="utf-8") == config_text
    assert w.task_service.inserted == [{"title": "Schule"}]  # no second task list
    assert [c[0] for c in autostart_fake.calls] == ["status"]
    assert any("Enter = behalten" in p for p in w.io.prompts)
    assert any("[aktuell: schueler.edu]" in p for p in w.io.prompts)


def test_setup_rerun_can_replace_a_single_value(world, paths, store):
    w = world
    happy_answers(w)
    main(["setup"], env=w.env)
    w.io.answers.extend(["", "", "", "n", "neuer.untis", "n"])
    w.io.secret_answers.extend(["", "neues-dummy-passwort"])
    assert main(["setup"], env=w.env) == 0
    assert store.get("eduvidual_password") == PW_EDU and store.get("webuntis_password") == "neues-dummy-passwort"
    assert load_config(paths.config).webuntis.username == "neuer.untis" and load_config(paths.config).eduvidual.username == "schueler.edu"
    assert "neues-dummy-passwort" not in w.io.out


def test_setup_expired_google_login_triggers_reauthorization_automatically(world, monkeypatch, paths):
    w = world
    happy_answers(w)
    assert main(["setup"], env=w.env) == 0
    w.authorized.clear()
    monkeypatch.setattr(cli.google_auth, "load_credentials", lambda *a, **k: (_ for _ in ()).throw(GoogleAuthError("Die Google-Anmeldung ist abgelaufen.", hint="x")))
    w.io.answers.extend(["", "", "n", "", "n"])
    w.io.secret_answers.extend(["", ""])
    assert main(["setup"], env=w.env) == 0
    assert len(w.authorized) == 1 and "nicht mehr gültig" in w.io.out


def test_setup_google_only_asks_nothing_else(world, paths, autostart_fake):
    w = world
    w.io.answers.append(str(w.client_file))
    assert main(["setup", "--google"], env=w.env) == 0
    cfg = load_config(paths.config)
    assert cfg.google.calendar_id == "cal-1" and cfg.eduvidual.username == ""
    assert autostart_fake.calls == []  # no autostart offer for a partial run
    # the Google step carries its own explanations even when it runs alone: the 7-day limit, the README pointer and the hint
    # on an old school automation (ANFORDERUNGEN 4 and 7)
    out = w.io.out
    assert "'Testing'" in out and "7 Tagen" in out and "In Produktion" in out and "README" in out
    assert "alte Schulautomation" in out and "doppelte Einträge" in out


def test_setup_single_source_only(world, paths, store):
    w = world
    w.io.answers.extend(["schueler.untis", "n"])
    w.io.secret_answers.append(PW_UNTIS)
    assert main(["setup", "--webuntis"], env=w.env) == 0
    assert store.names() == ["webuntis_password"] and load_config(paths.config).webuntis.username == "schueler.untis"
    assert w.authorized == [] and w.checked == []


def test_setup_source_without_password_is_incomplete_and_not_tested(world, store):
    w = world
    w.io.answers.append("schueler.edu")
    w.io.secret_answers.append("")
    assert main(["setup", "--eduvidual"], env=w.env) == 0
    assert "noch unvollständig" in w.io.out and w.checked == [] and store.names() == []


def test_setup_probe_error_is_shown_not_fatal(world):
    w = world
    w.io.answers.extend(["schueler.edu", ""])
    w.io.secret_answers.append(PW_EDU)
    w.env.make_adapter = lambda s, c, st: (_ for _ in ()).throw(NetworkError("Keine Verbindung.", hint="Internet prüfen."))
    assert main(["setup", "--eduvidual"], env=w.env) == 0
    assert "Keine Verbindung." in w.io.out


def test_setup_declined_test_keeps_the_auth_block(world, paths, now):
    w = world
    db = StateStore(paths.db)
    db.set_auth_blocked(Source.EDUVIDUAL, now, "auth")
    db.close()
    w.io.answers.extend(["schueler.edu", "n"])
    w.io.secret_answers.append(PW_EDU)
    main(["setup", "--eduvidual"], env=w.env)
    db = StateStore(paths.db)
    try:
        assert db.is_auth_blocked(Source.EDUVIDUAL)
    finally:
        db.close()


@pytest.mark.parametrize("entries, expect_in_out", [
    ([cal("p", "Privat")], "Kein Kalender mit dem Namen 'Schule'"),
    ([cal("s1"), cal("s2")], "Mehrere Kalender heißen 'Schule'"),
    ([cal(ACCOUNT, "Schule", primary=True)], "Hauptkalender"),
    ([cal("s1", role="reader")], "Keine Schreibrechte"),
])
def test_setup_aborts_clearly_when_the_calendar_is_not_unique_or_usable(entries, expect_in_out, world, paths, store, autostart_fake):
    w = world
    w.cal_service.entries = entries
    w.io.answers.append(str(w.client_file))  # any further question would raise UnexpectedQuestion
    assert main(["setup"], env=w.env) == 1
    assert "Abbruch:" in w.io.out and expect_in_out in w.io.out
    assert "schulsync setup" in w.io.out
    cfg = load_config(paths.config)
    assert cfg.google.calendar_id == "" and cfg.google.tasklist_id == ""
    assert w.task_service.inserted == []  # no task list created behind an aborted setup
    # H1 (deliberately changed): the login is kept in memory until account, calendar and task list are verified; an abort
    # persists nothing, so a login made with the wrong account (or before the calendar exists) never replaces a working one.
    assert store.names() == [] and "nichts gespeichert oder verändert" in w.io.out
    assert autostart_fake.calls == []


def test_setup_no_calendar_hint_explains_how_to_create_it(world):
    w = world
    w.cal_service.entries = []
    w.io.answers.append(str(w.client_file))
    assert main(["setup"], env=w.env) == 1
    assert "Google Kalender" in w.io.out and "anlegen" in w.io.out


def expect_account(w) -> None:
    """The expected Google account is optional (empty default); the wrong-account tests configure it."""
    w.paths.home.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "config.example.toml", w.paths.config)
    set_config_value(w.paths.config, "google", "account", ACCOUNT)


@pytest.mark.parametrize("login, stops", [(ACCOUNT, False), ("jemand.anderes@example.com", True)])
def test_setup_stops_when_the_login_is_not_the_configured_account(login, stops, world):
    expect_account(world)
    # H1 (deliberately changed from "only a warning"): a mismatch stops the setup before anything is stored; see
    # tests/test_harden_cli.py::TestSetupWrongAccount for the full set of cases.
    w = world
    w.cal_service.primary_id = login
    w.io.answers.append(str(w.client_file))
    assert main(["setup", "--google"], env=w.env) == (1 if stops else 0)
    assert ("Abbruch" in w.io.out) is stops
    if stops:
        assert login in w.io.out and ACCOUNT in w.io.out and w.store.names() == []


def test_setup_existing_tasklist_is_reused(world):
    w = world
    w.task_service.lists = [{"id": "L-vorhanden", "title": "Schule"}]
    w.io.answers.append(str(w.client_file))
    assert main(["setup", "--google"], env=w.env) == 0
    assert w.task_service.inserted == [] and load_config(w.paths.config).google.tasklist_id == "L-vorhanden"
    assert "gefunden" in w.io.out


def test_setup_without_client_file_aborts_with_instructions(world, store):
    w = world
    w.io.answers.append("")
    assert main(["setup", "--google"], env=w.env) == 1
    assert "OAuth-Client-Datei" in w.io.out and store.names() == []


def test_setup_unreadable_client_path_aborts(world, tmp_path):
    w = world
    w.io.answers.append(str(tmp_path / "gibt-es-nicht.json"))
    assert main(["setup", "--google"], env=w.env) == 1
    assert "nicht gelesen" in w.io.out


def test_setup_invalid_client_file_is_rejected_before_anything_is_stored(world, tmp_path, store):
    w = world
    bad = tmp_path / "web_client.json"
    bad.write_text(json.dumps({"web": {"client_id": "x"}}), encoding="utf-8")
    w.io.answers.append(str(bad))
    assert main(["setup", "--google"], env=w.env) == 2
    assert "OAuth-Client-Datei ist ungültig" in w.io.out and "Desktop-App" in w.io.out
    assert store.names() == [] and w.authorized == []


def test_setup_authorization_failure_is_reported(world, store):
    w = world
    w.env.authorize = lambda client_json: (_ for _ in ()).throw(GoogleAuthError("Google hat die Anmeldung nicht bestätigt.", hint="Erneut versuchen."))
    w.io.answers.append(str(w.client_file))
    assert main(["setup", "--google"], env=w.env) == 1
    assert "Google hat die Anmeldung nicht bestätigt." in w.io.out
    assert "google_token" not in store.names()


def test_setup_keeps_an_existing_config_untouched(world, paths):
    w = world
    paths.config.write_text('# meine Notiz\n[google]\ncalendar_name = "Schule"\n', encoding="utf-8")
    w.io.answers.append(str(w.client_file))
    assert main(["setup", "--google"], env=w.env) == 0
    text = paths.config.read_text(encoding="utf-8")
    assert "# meine Notiz" in text and 'calendar_id = "cal-1"' in text


def test_setup_with_a_broken_config_is_a_config_error(world, paths):
    paths.config.write_text("[google\n", encoding="utf-8")
    assert main(["setup"], env=world.env) == 2
    assert "kein gültiges TOML" in world.io.out


def test_setup_without_example_file_generates_the_defaults(world, paths, tmp_path):
    w = world
    w.env.example_config = tmp_path / "gibt-es-nicht.toml"
    w.io.answers.append(str(w.client_file))
    assert main(["setup", "--google"], env=w.env) == 0
    assert "aus den Standardwerten" in w.io.out
    cfg = load_config(paths.config)
    assert replace(cfg, google=Config().google) == Config()  # every default value round-trips through the file
    assert cfg.google.calendar_id == "cal-1"


def test_setup_uses_a_custom_example_file(world, paths, tmp_path):
    w = world
    example = tmp_path / "meine.toml"
    example.write_text('# eigene Vorlage\n[google]\naccount = "' + ACCOUNT + '"\n', encoding="utf-8")
    w.env.example_config = example
    w.io.answers.append(str(w.client_file))
    assert main(["setup", "--google"], env=w.env) == 0
    assert "# eigene Vorlage" in paths.config.read_text(encoding="utf-8")


def test_setup_autostart_declined_by_default(world, autostart_fake):
    w = world
    w.io.answers.extend([str(w.client_file), "schueler.edu", "n", "schueler.untis", "n", ""])
    w.io.secret_answers.extend([PW_EDU, PW_UNTIS])
    assert main(["setup"], env=w.env) == 0
    assert [c[0] for c in autostart_fake.calls] == ["status"] and "autostart install" in w.io.out


def test_setup_autostart_failure_text_is_shown(world, autostart_fake):
    w = world
    autostart_fake.ok = False
    w.io.answers.extend([str(w.client_file), "schueler.edu", "n", "schueler.untis", "n", "j"])
    w.io.secret_answers.extend([PW_EDU, PW_UNTIS])
    assert main(["setup"], env=w.env) == 0
    assert "Autostart eingerichtet (Fake)." in w.io.out


def test_repo_example_config_is_found_and_valid():
    example = cli._repo_example_config()
    assert example.is_file()
    assert isinstance(load_config(example), Config)


def test_default_ask_without_console_raises_a_config_error(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    with pytest.raises(cli.NoConsoleError) as ei:
        cli._default_ask("x: ")
    assert "Konsole" in ei.value.message
    with pytest.raises(cli.NoConsoleError):
        cli._default_ask_secret("x: ")


def test_setup_holds_the_process_lock_and_refuses_while_a_run_is_active(world, paths):
    w = world
    from schulsync.lock import ProcessLock

    with ProcessLock(paths.lock):
        assert main(["setup"], env=w.env) == 1
    assert "SchulSync läuft gerade, bitte kurz warten" in w.io.out  # H3: the same wait message for setup and check
    assert w.authorized == []  # nothing was asked or changed
    w.io.answers.append(str(w.client_file))
    assert main(["setup", "--google"], env=w.env) == 0  # the lock is free again after the refused attempt


def test_setup_replaces_a_secret_that_cannot_be_decrypted(world, store):
    w = world

    class Undecryptable(MemorySecretStore):
        def get(self, name):
            if name == "eduvidual_password":
                raise ConfigError("Der gespeicherte Eintrag 'eduvidual_password' kann nicht entschlüsselt werden.", hint="x")
            return super().get(name)

    w.env.secrets = broken = Undecryptable({"eduvidual_password": "alt"})
    w.io.answers.extend(["schueler.edu", "n"])
    w.io.secret_answers.append(PW_EDU)
    assert main(["setup", "--eduvidual"], env=w.env) == 0
    assert "wird bei dieser Einrichtung ersetzt" in w.io.out
    assert dict(broken._data)["eduvidual_password"] == PW_EDU


def test_run_and_preview_through_the_real_runner(env, io, paths, config_file, now, opened):
    """Wiring check with the real engine: fake school + FakeGoogle, no monkeypatching of runner.run."""
    item = samples.assignment(due=samples.at(2026, 10, 21, 23, 59))
    google = FakeGoogle()
    adapter = FakeAdapter(Source.EDUVIDUAL).set_items([item])
    set_config_value(paths.config, "webuntis", "enabled", False)
    env.build_deps = lambda p, c, s: make_deps(
        p, c, adapter_factories={Source.EDUVIDUAL: lambda: adapter}, gateway_factory=lambda: google,
        clock=lambda: now, sleep=lambda s: None, open_report=lambda path: opened.append(path) or True, network_ready=lambda: True,
    )
    assert main(["preview"], env=env) == 0
    assert google.write_log == [] and opened == [paths.reports / "vorschau.html"]
    assert main(["run", "--force"], env=env) == 0
    assert len(google.events) == 1 and len(google.open_tasks) == 1
    io.lines.clear()
    assert main(["run", "--auto"], env=env) == 0  # same day: quiet
    assert io.lines == []
    assert len(google.events) == 1


# --------------------------------------------------------------------------- audit: the real lock, and the flags together


def test_run_auto_with_the_lock_really_held_by_another_run_ends_quietly_and_runs_nothing(env, io, config_file, opened):
    """The test above stubs `runner.run` with an exception the real runner never raises. Here the REAL runner meets a REAL
    held lock (ANFORDERUNGEN 1: parallel runs are prevented by a process lock): a silent exit 0, nothing fetched, no report."""
    from schulsync.lock import ProcessLock

    adapters = {s: FakeAdapter(s) for s in Source}
    env.build_deps = lambda p, c, s: make_deps(p, c, adapter_factories={src: (lambda a=a: a) for src, a in adapters.items()})
    other = ProcessLock(env.paths.lock)
    assert other.acquire()
    try:
        assert main(["run", "--auto"], env=env) == 0
        assert io.lines == [] and opened == [] and list(env.paths.reports.glob("*.html")) == []
        assert all(a.calls == [] for a in adapters.values())
        io.lines.clear()
        assert main(["run"], env=env) == 1  # a manual start says so
        assert "läuft" in io.out
    finally:
        other.release()


def test_run_auto_force_passes_both_flags_to_the_runner(env, io, config_file, monkeypatch):
    """ACCEPTANCE_NOTES step 6 runs `run --auto --force`: the automatic mode (silent, report only for problems) WITH the day
    limit lifted. Both flags must arrive together."""
    seen = []

    def fake_run(deps, mode, *, force=False):
        seen.append((mode, force))
        return outcome(0, silent=True)

    monkeypatch.setattr(runner, "run", fake_run)
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run", "--auto", "--force"], env=env) == 0
    assert main(["run", "--auto"], env=env) == 0
    assert main(["run", "--force"], env=env) == 0
    assert main(["run"], env=env) == 0
    assert seen == [(RunMode.AUTO, True), (RunMode.AUTO, False), (RunMode.MANUAL, True), (RunMode.MANUAL, False)]
    assert io.lines == []  # the automatic runs stay silent, the manual ones print only non-empty texts


def test_the_old_automation_hint_is_part_of_the_google_step_and_of_the_readme():
    """ANFORDERUNGEN 7: "Falls eine alte Schulautomation noch läuft, auf mögliche Überschneidungen hinweisen". The wizard says it in
    the Google step (where the calendar and list are chosen) and the README says it too."""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "alte Schulautomation" in readme and "doppelte Einträge" in readme
    source = (Path(cli.__file__)).read_text(encoding="utf-8")
    assert "alte Schulautomation" in source and "doppelte Einträge" in source
