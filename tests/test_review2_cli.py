"""Adversarial review of cli.py / factories.py / autostart.py / secrets_store.py (area "cli").

Every test here describes behaviour the requirements demand; each one fails against the current code.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from schulsync import autostart, cli, runner
from schulsync.autostart import AutostartStatus
from schulsync.cli import CliEnv, main
from schulsync.config import Config, Paths, set_config_value
from schulsync.logging_setup import shutdown_logging
from schulsync.redact import clear_registered_secrets, register_secret
from schulsync.secrets_store import MemorySecretStore
from tests.support.fake_google import FakeGoogle

SECRET = "hunter2-dummy-edu"


class Io:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.answers: deque[str] = deque()
        self.secret_answers: deque[str] = deque()

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    def ask(self, prompt: str) -> str:
        return self.answers.popleft()

    def ask_secret(self, prompt: str) -> str:
        return self.secret_answers.popleft()

    @property
    def out(self) -> str:
        return "\n".join(self.lines)


class FakeAutostart:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def install(self, paths):
        self.calls.append(("install", paths))
        return AutostartStatus("task", "Autostart eingerichtet (Fake).", True)

    def remove(self):
        self.calls.append(("remove",))
        return AutostartStatus(None, "entfernt", True)

    def status(self):
        return AutostartStatus(None, "nicht eingerichtet", False)


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
def opened() -> list[Path]:
    return []


@pytest.fixture
def env(paths, io, now, opened) -> CliEnv:
    def refuse(*a, **k):
        raise AssertionError("unerwarteter Aufruf")

    return CliEnv(
        say=io.say, ask=io.ask, ask_secret=io.ask_secret, clock=lambda: now, paths=paths,
        secrets=MemorySecretStore(), build_deps=refuse, make_adapter=refuse, google_services=refuse,
        authorize=refuse, open_report=lambda p: opened.append(p) or True, autostart=FakeAutostart(),
    )


@pytest.fixture
def config_file(paths) -> Path:
    cli._write_default_config(paths.config)
    return paths.config


def make_deps(paths: Paths, config: Config, **kw) -> runner.Deps:
    kw.setdefault("adapter_factories", {})
    kw.setdefault("gateway_factory", lambda: FakeGoogle())
    return runner.Deps(paths=paths, config=config, **kw)


# --------------------------------------------------------------------------- --home


def test_relative_home_is_made_absolute_before_it_reaches_the_autostart(env, tmp_path, monkeypatch):
    """The scheduled task starts with another working directory, so `--home daten` would point somewhere else
    (ANFORDERUNGEN 1: Autostart im Benutzerkontext; DESIGN_PHASE2: `--home` wird an `run --auto` angehängt)."""
    env.paths = None
    monkeypatch.chdir(tmp_path)
    assert main(["--home", "daten", "autostart", "install"], env=env) == 0
    (call,) = env.autostart.calls
    assert call[0] == "install"
    assert call[1].home.is_absolute()


def test_autostart_install_carries_home_when_it_comes_from_the_environment(tmp_path, monkeypatch):
    """SCHULSYNC_HOME of the installing shell is not inherited by the Task Scheduler (autostart.py's own docstring:
    'Der Autostart startet ohne Umgebungsvariable SCHULSYNC_HOME'), yet `_is_default_home` compares with
    `Paths.default()`, which honours that variable: the installed task would use the folder below LOCALAPPDATA, not the
    directory the user installed from. (Contradicts the assertion of test_autostart's
    `test_install_with_default_home_adds_no_home_option`, which pins the current behaviour.)"""
    seen: list[str] = []
    monkeypatch.setattr(autostart, "_try_task", lambda xml: seen.append(xml))  # None = "task created"
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    home = tmp_path / "Daten"
    monkeypatch.setenv("SCHULSYNC_HOME", str(home))
    autostart.install(Paths.default())
    assert "--home" in seen[0] and str(home) in seen[0]


# --------------------------------------------------------------------------- run --auto: failures without report


def test_run_auto_reports_a_failure_the_runner_could_not_report(env, io, paths, opened, config_file):
    """ANFORDERUNGEN 8: an unsuccessful run must produce an HTML report and open it. If the lock file cannot even be
    created, the runner returns exit code 2 without a report, and the CLI only `say`s the text: under pythonw
    (stdout None) the user never learns that nothing ran."""
    paths.lock.mkdir()  # a directory in place of the lock file: os.open raises PermissionError
    env.build_deps = lambda p, c, s: make_deps(p, c)
    assert main(["run", "--auto"], env=env) != 0
    assert len(opened) == 1 and opened[0].is_file()


# --------------------------------------------------------------------------- broken config value types

BAD_CONFIG = "[retry]\ndelays_seconds = [\"a\"]\n"


def test_status_survives_a_config_value_of_the_wrong_type_and_leaves_a_log(env, io, paths):
    """`status` promises to work without a usable configuration. `config_or_none` only catches ConfigError, but
    `delays_seconds = ["a"]` makes `config._build` raise a bare ValueError inside `_bootstrap`, i.e. before logging is
    set up: the command crashes with 'Technische Details stehen in der Logdatei' although no log file exists."""
    paths.config.write_text(BAD_CONFIG, encoding="utf-8")
    code = main(["status"], env=env)
    assert code == 0, io.out
    assert "fehlerhaft" in io.out


def test_check_reports_a_config_value_of_the_wrong_type_as_a_configuration_error(env, io, paths):
    paths.config.write_text(BAD_CONFIG, encoding="utf-8")
    assert main(["check"], env=env) == 2  # like every other broken configuration
    assert "Konfiguration" in io.out and "Unerwarteter Fehler" not in io.out


# --------------------------------------------------------------------------- console output is not masked


def _check_world(env, paths, detail: str):
    from schulsync import google_auth
    from schulsync.models import ProbeReport, ProbeStep, Source
    from tests.support.fake_adapter import FakeAdapter

    set_config_value(paths.config, "eduvidual", "username", "schueler.edu")
    set_config_value(paths.config, "webuntis", "enabled", False)
    env.secrets.set(google_auth.SECRET_TOKEN, json.dumps({"refresh_token": "1//dummy"}))
    env.secrets.set("eduvidual_password", SECRET)
    probe = ProbeReport(Source.EDUVIDUAL, [ProbeStep("Anmeldung", False, detail)])
    adapter = FakeAdapter(Source.EDUVIDUAL, check_report=probe)
    from schulsync.config import load_config

    deps = make_deps(paths, load_config(paths.config), adapter_factories={Source.EDUVIDUAL: lambda: adapter})
    env.build_deps = lambda p, c, s: deps


def test_check_masks_registered_secrets_in_probe_details_on_the_console(env, io, paths, config_file):
    """The runner redacts its console text (found by the acceptance tests earlier); `check` prints adapter-supplied
    step details unmasked."""
    register_secret(SECRET)
    _check_world(env, paths, f"Server meldet: ungültig {SECRET}")
    main(["check"], env=env)
    assert SECRET not in io.out


def test_failure_message_on_the_console_is_masked_like_the_log(env, io, paths, config_file):
    from schulsync.errors import ConfigError

    register_secret(SECRET)
    env.build_deps = lambda p, c, s: (_ for _ in ()).throw(ConfigError(f"Anmeldung mit {SECRET} abgelehnt"))
    assert main(["run"], env=env) == 2
    assert SECRET not in io.out
    assert SECRET not in "\n".join(f.read_text(encoding="utf-8") for f in paths.logs.glob("*.log"))  # the log is masked


# --------------------------------------------------------------------------- setup: an adapter bug must not abort the wizard


def test_setup_continues_after_an_unexpected_adapter_error(env, io, paths):
    """`check` isolates a crashing probe ('a bug in one probe must not stop the other probes'); `setup` only catches
    SchulSyncError, so a RuntimeError from the Eduvidual probe aborts before WebUntis and the autostart offer."""
    from schulsync.models import Source

    asked: list[Source] = []

    def make_adapter(source, config, secrets):
        asked.append(source)
        if source is Source.EDUVIDUAL:
            raise RuntimeError("Fehler im Adapter")
        raise AssertionError("WebUntis wird hier nicht geprüft")

    env.make_adapter = make_adapter
    # Eduvidual: user name, password, test? ja; WebUntis: user name, (no password -> incomplete); autostart? nein
    io.answers.extend(["schueler.edu", "j", "schueler.untis", "n"])
    io.secret_answers.extend([SECRET, ""])
    assert main(["setup", "--eduvidual", "--webuntis"], env=env) == 0, io.out
    assert asked == [Source.EDUVIDUAL]
    assert "Benutzername und Passwort nötig" in io.out  # the WebUntis step was reached


def test_run_auto_with_a_config_value_of_the_wrong_type_still_opens_the_failure_report(env, paths, opened):
    """ANFORDERUNGEN 8: the automatic run must never fail silently. `_auto_failure_report` calls `config_or_none()`
    (which lets the ValueError through) after the report was written but before the browser is opened, so the
    error handler itself dies ('Fehlerbehandlung fehlgeschlagen') and nothing is shown."""
    paths.config.write_text(BAD_CONFIG, encoding="utf-8")
    assert main(["run", "--auto"], env=env) in (1, 2)
    assert len(opened) == 1 and opened[0].is_file()
