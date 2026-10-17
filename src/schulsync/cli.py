"""Command line: `python -m schulsync <command>` / console script `schulsync`.

Commands: setup, run [--auto] [--force], preview, check, status, restore [UID ...], report, autostart install|remove|status.
Global options: `--home DIR` (data directory, else $SCHULSYNC_HOME, else %LOCALAPPDATA%\\SchulSync), `-v/--verbose`
(log lines also on the console), `--version`. `--home`/`-v` are accepted behind the sub-command as well, so the autostart
can append `--home <dir>` to `run --auto`.

Exit codes (like `runner.RunOutcome`): 0 ok / nothing to do, 1 partial failure or failure, 2 configuration or usage error.

Design notes
- All console I/O goes through `CliEnv` (say / ask / ask_secret) so tests inject it; the defaults never raise when
  `sys.stdout`/`sys.stdin` is None (pythonw.exe). Interactive commands without a console end with a German message.
- Logging goes to the daily log file in every command (logging_setup); console lines only with `--verbose`.
- `run --auto` never fails silently: any error before or outside `runner.run` (missing/broken config, secret store,
  crash) becomes a minimal FAILED report that is written and opened in the browser once per day and error kind
  (marker file `auto_failure_report.json`; the file is independent of the state DB, which may be the broken part).
- Secrets are never printed or logged: new passwords are registered with `redact` right after input; the console only
  ever shows secret *names* (check) or nothing (setup).
- `check` and `setup` take the program lock (they may refresh or write the Google token and the secret store); while a run
  or the other command is active they say so and end with exit code 1. `status` and `report` stay lock-free.
- `run` starts a hard watchdog (H4): `retry.run_timeout_seconds` is only cooperative, a hung HTTP call would keep the
  process, and with it the lock, alive until reboot, and every later automatic start would end silently. After
  run_timeout_seconds + 60 s the watchdog thread logs, writes a FAILED report (code `timeout`) and ends the process with
  `os._exit(3)`; the OS releases the lock and the unfinished work is picked up by the next start.
- `setup --google` keeps everything it learns in memory until the authorized account, the calendar and the task list are
  verified, and persists token, client file and ids only then (H1): a wrong Google account can never replace a working login.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__, factories, google_auth, runner
from . import autostart as autostart_module
from .config import Config, Paths, ReportConfig, load_config, set_config_value
from .errors import ConfigError, ErrorInfo, GoogleAuthError, SchulSyncError
from . import frozen, update as updater
from .google_client import call_api
from .google_targets import TargetsUnavailableError
from .lock import LockHeldError, ProcessLock
from .logging_setup import setup_logging, shutdown_logging
from .models import (
    Category,
    CategoryStatus,
    EventState,
    Kind,
    Notice,
    OverallStatus,
    ProbeReport,
    RunMode,
    RunReport,
    Severity,
    Source,
    Target,
    TaskState,
)
from .redact import redact, register_secret
from .report import latest_report, open_report as default_open_report, prune_reports, write_report
from .secrets_store import DpapiSecretStore, MemorySecretStore, SecretStore, register_loaded_secrets
from .state import StateStore
from .timeutil import now_vienna, to_vienna, today_vienna

log = logging.getLogger("schulsync.cli")

PROG = "schulsync"
FAILURE_MARKER = "auto_failure_report.json"
WATCHDOG_GRACE_SECONDS = 60.0  # the hard limit is run_timeout_seconds (cooperative) plus this
LOCK_BUSY_TEXT = "SchulSync läuft gerade, bitte kurz warten."
LABEL = factories.SOURCE_LABEL
_CATEGORY_NAME = {
    Category.ASSIGNMENTS: "Aufgaben und Abgaben",
    Category.HOMEWORK: "Hausaufgaben",
    Category.EXAMS: "Prüfungen und Schularbeiten",
    Category.CANCELLATIONS: "Unterrichtsentfälle",
}
_CATEGORY_STATUS = {
    CategoryStatus.OK: "OK",
    CategoryStatus.FAILED: "FEHLGESCHLAGEN",
    CategoryStatus.INCOMPLETE: "UNVOLLSTÄNDIG",
    CategoryStatus.DISABLED: "ABGESCHALTET",
}
_OVERALL = {
    OverallStatus.SUCCESS: "erfolgreich",
    OverallStatus.PARTIAL: "teilweise fehlgeschlagen",
    OverallStatus.FAILED: "fehlgeschlagen",
    OverallStatus.NOTHING_TO_DO: "nichts zu tun",
}
_MODE = {RunMode.AUTO: "automatisch", RunMode.MANUAL: "manuell", RunMode.PREVIEW: "Vorschau"}


# --------------------------------------------------------------------------- console I/O (never crashes without console)


class NoConsoleError(ConfigError):
    """An interactive question was asked although no console is attached (pythonw.exe, closed stdin)."""


def _default_say(text: str = "") -> None:
    stream = sys.stdout
    if stream is None:
        return
    try:
        try:
            stream.write(text + "\n")
        except UnicodeEncodeError:
            enc = getattr(stream, "encoding", None) or "ascii"
            stream.write((text + "\n").encode(enc, "replace").decode(enc, "replace"))
        stream.flush()
    except (OSError, ValueError):  # closed or broken pipe: output is optional, the exit code is what counts
        pass


def _no_console() -> NoConsoleError:
    return NoConsoleError(
        "Für diese Eingabe wird eine Konsole benötigt, es ist aber keine vorhanden.",
        hint="Bitte in einem Eingabeaufforderungs- oder PowerShell-Fenster ausführen: python -m schulsync setup",
    )


def _default_ask(prompt: str) -> str:
    if sys.stdin is None or sys.stdout is None:
        raise _no_console()
    try:
        return input(prompt)
    except EOFError:
        raise _no_console() from None


def _default_ask_secret(prompt: str) -> str:
    if sys.stdin is None or sys.stdout is None:
        raise _no_console()
    try:
        return getpass.getpass(prompt)
    except EOFError:
        raise _no_console() from None


@dataclass
class CliEnv:
    """Everything the commands touch outside the process: injectable for tests, production defaults otherwise."""

    say: Callable[[str], None] = _default_say
    ask: Callable[[str], str] = _default_ask
    ask_secret: Callable[[str], str] = _default_ask_secret
    clock: Callable[[], datetime] = now_vienna
    paths: Paths | None = None  # None: --home, else $SCHULSYNC_HOME, else %LOCALAPPDATA%\SchulSync
    secrets: SecretStore | None = None  # None: DPAPI store in paths.secrets
    build_deps: Callable[[Paths, Config, SecretStore], runner.Deps] = factories.build_deps
    make_adapter: Callable[[Source, Config, SecretStore], Any] = factories.make_adapter
    google_services: Callable[[Any, Config], tuple[Any, Any]] = factories.build_google_services
    authorize: Callable[[str], Any] = google_auth.authorize_interactive
    open_report: Callable[[Path], bool] = default_open_report
    autostart: Any = autostart_module  # install(paths) / remove() / status()
    example_config: Path | None = None  # None: config.example.toml next to the package (repo root)
    hard_exit: Callable[[int], None] = os._exit  # what the `run` watchdog ends the process with (tests inject a recorder)
    watchdog_grace: float = WATCHDOG_GRACE_SECONDS  # seconds after retry.run_timeout_seconds before the watchdog fires


# --------------------------------------------------------------------------- session


class _Session:
    def __init__(self, args: argparse.Namespace, env: CliEnv):
        self.args = args
        self.env = env
        home = getattr(args, "home", None)
        paths = env.paths or (Paths(Path(home).expanduser()) if home else Paths.default())
        if not paths.home.is_absolute():
            # The autostart (Task Scheduler) starts in another working directory: a relative folder would point
            # somewhere else on every logon.
            paths = Paths(Path(os.path.abspath(paths.home)))
        self.paths = paths
        self.secrets: SecretStore = env.secrets if env.secrets is not None else DpapiSecretStore(self.paths.secrets)
        self.started = env.clock()
        self._config: Config | None = None

    def say(self, text: str = "") -> None:
        # single choke point of the console: whatever a server, a library or an exception text echoes is masked
        self.env.say(redact(text))

    def load_config(self, *, reload: bool = False) -> Config:
        if self._config is None or reload:
            try:
                self._config = load_config(self.paths.config)
            except ConfigError:
                raise
            except (ValueError, TypeError, OSError) as exc:
                # config.py lets a wrongly typed value (e.g. delays_seconds = ["a"]) escape as a bare ValueError and an
                # unreadable file as OSError; to every caller that is just a broken configuration (contract request filed)
                raise ConfigError(
                    f"Die Konfigurationsdatei enthält einen ungültigen Wert oder ist nicht lesbar ({type(exc).__name__}).",
                    hint="config.toml prüfen (Zahlen ohne Anführungszeichen, Texte in Anführungszeichen) oder 'schulsync setup' ausführen.",
                ) from exc
        return self._config

    def config_or_none(self) -> Config | None:
        try:
            return self.load_config()
        except ConfigError:
            return None

    def deps(self) -> runner.Deps:
        return self.env.build_deps(self.paths, self.load_config(), self.secrets)

    def yes(self, question: str, default: bool) -> bool:
        answer = self.env.ask(f"{question} [{'J/n' if default else 'j/N'}]: ").strip().lower()
        if answer in ("j", "ja", "y", "yes"):
            return True
        if answer in ("n", "nein", "no"):
            return False
        return default


def _fmt(dt: datetime | None) -> str:
    return "nie" if dt is None else to_vienna(dt).strftime("%d.%m.%Y %H:%M") + " Uhr"


@contextmanager
def _state(s: _Session, deps: runner.Deps | None = None) -> Iterator[StateStore | None]:
    """The state store if there is one (injected via deps, or an existing DB file); never creates a database."""
    injected = getattr(deps, "state", None)
    if injected is not None:
        yield injected
        return
    if not s.paths.db.exists():
        yield None
        return
    store = StateStore(s.paths.db)
    try:
        yield store
    finally:
        store.close()


# --------------------------------------------------------------------------- error handling


def _error_info(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, SchulSyncError):
        return exc.to_info()
    return ErrorInfo(
        "internal",
        f"Unerwarteter Fehler ({type(exc).__name__}).",
        hint="Technische Details stehen in der Logdatei (Ordner 'logs' im SchulSync-Datenordner).",
    )


def _fail(s: _Session, exc: BaseException) -> int:
    """Turn any exception into a German message, a log entry and an exit code. Never raises."""
    auto = bool(getattr(s.args, "auto", False))
    try:
        info = _error_info(exc)
        if isinstance(exc, SchulSyncError):
            log.error("Fehler (%s): %s %s", info.code, info.message, info.detail or "")
        else:
            log.error("Unerwarteter Fehler: %s", type(exc).__name__, exc_info=exc)
        if auto and isinstance(exc, LockHeldError):
            return 0  # another instance is working: the quiet exit of an automatic start
        code = 2 if isinstance(exc, ConfigError) else 1
        if auto:
            _auto_failure_report(s, info)
        else:
            s.say(f"Fehler: {info.message}")
            if info.hint:
                s.say(f"Hinweis: {info.hint}")
        return code
    except Exception:  # noqa: BLE001 - the error handler must not fail
        log.exception("Fehlerbehandlung fehlgeschlagen")
        return 1


def _auto_failure_report(s: _Session, info: ErrorInfo, mode: RunMode = RunMode.AUTO) -> None:
    """Minimal FAILED report for a run that died outside the runner (or was killed by the watchdog); opened once per day
    and error kind."""
    now = s.env.clock()
    day = today_vienna(now)
    text = info.message + (f" ({info.detail})" if info.detail else "")
    rep = RunReport(
        started_at=s.started,
        finished_at=now,
        mode=mode,
        day=day,
        status=OverallStatus.FAILED,
        notices=[Notice(Severity.ERROR, info.code, text)],
        next_steps=[info.hint] if info.hint else [],
    )
    try:
        path = write_report(s.paths, rep)
    except OSError as exc:
        log.error("Fehlerbericht konnte nicht geschrieben werden (%s).", type(exc).__name__)
        s.say(f"Fehler: {info.message}")
        return
    rep.report_path = str(path)
    log.error("Automatischer Lauf fehlgeschlagen; Bericht: %s", path)
    cfg = s.config_or_none()
    # Reports are kept for a limited time (ANFORDERUNGEN 8). The runner prunes after every run; a run that dies before the
    # runner never gets there, so a permanently broken installation would grow the folder by one report per day.
    keep_days = cfg.report.keep_days if cfg is not None else ReportConfig().keep_days
    try:
        prune_reports(s.paths, keep_days, now)
    except Exception:  # noqa: BLE001 - housekeeping must never hide the failure it reports
        log.warning("Alte Berichte konnten nicht aufgeräumt werden.", exc_info=True)
    if cfg is not None and cfg.report.open_on == "never":
        return
    fingerprint = hashlib.sha256(f"{info.code}|{info.message}".encode("utf-8")).hexdigest()[:16]
    marker = s.paths.home / FAILURE_MARKER
    try:
        seen = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if seen == {"day": day.isoformat(), "fingerprint": fingerprint}:
        log.info("Derselbe Fehlerbericht wurde heute schon geöffnet; kein weiterer Tab.")
        return
    if s.env.open_report(path):
        try:
            marker.write_text(json.dumps({"day": day.isoformat(), "fingerprint": fingerprint}), encoding="utf-8")
        except OSError:
            log.warning("Marker für den Fehlerbericht konnte nicht geschrieben werden.")


# --------------------------------------------------------------------------- run / preview / restore / report


class _Watchdog:
    """Hard time limit of `run`. A daemon thread waits for `seconds`; if the run is not finished by then it logs, writes a
    best-effort FAILED report (code `timeout`) and ends the process with exit code 3 (`env.hard_exit`, `os._exit`): a call
    that hangs inside the HTTP stack cannot be interrupted, and a stuck process would hold the program lock until reboot."""

    def __init__(self, s: _Session, seconds: float, mode: RunMode) -> None:
        self._s, self._seconds, self._mode = s, seconds, mode
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="schulsync-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()  # the run ended by itself: the watchdog thread returns without doing anything

    def _watch(self) -> None:
        if self._done.wait(self._seconds):
            return
        s = self._s
        try:
            limit = f"{self._seconds / 60:.0f} Minuten" if self._seconds >= 60 else f"{self._seconds:.0f} Sekunden"
            info = ErrorInfo(
                "timeout",
                f"Der Lauf wurde nach {limit} hart abgebrochen: Ein Aufruf hat nicht mehr geantwortet.",
                hint="Internetverbindung prüfen und 'schulsync run' erneut ausführen. Bereits Übernommenes bleibt erhalten, der Rest "
                "wird beim nächsten Start nachgeholt. Tritt das wiederholt auf, 'schulsync check' ausführen.",
            )
            log.error("Hartes Zeitlimit erreicht (%s): Der Lauf hängt und wird mit Exit-Code 3 beendet.", limit)
            try:
                _auto_failure_report(s, info, self._mode)
                if self._mode != RunMode.AUTO:
                    s.say(f"Fehler: {info.message}")
                    s.say(f"Hinweis: {info.hint}")
            except Exception:  # noqa: BLE001 - best effort: nothing may keep the process alive
                log.exception("Der Bericht zum harten Zeitlimit konnte nicht erstellt werden")
            for handler in logging.getLogger().handlers + log.handlers + logging.getLogger("schulsync").handlers:
                try:
                    handler.flush()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            s.env.hard_exit(3)


def _probe_state(s: _Session, deps: runner.Deps) -> None:
    """The runner opens the state database itself and, if that fails, can only report on every start (it has no memory of
    what it showed). Probe an existing database file here so that an unusable one takes the CLI's way: `_fail`, which for an
    automatic run writes and opens the failure report once per day and error kind (marker file, independent of the DB).
    Only a ConfigError (corrupt / unreadable file) counts; a busy database is the runner's business."""
    if deps.state is not None or not s.paths.db.exists():
        return
    try:
        StateStore(s.paths.db).close()
    except ConfigError:
        raise
    except SchulSyncError as exc:
        log.warning("Zustandsdatenbank beim Vorabtest nicht verfügbar (%s); der Lauf versucht es selbst.", exc.code)


def _cmd_run(s: _Session) -> int:
    auto = bool(s.args.auto)
    mode = RunMode.AUTO if auto else RunMode.MANUAL
    deps = s.deps()
    _probe_state(s, deps)
    watchdog = _Watchdog(s, deps.config.retry.run_timeout_seconds + s.env.watchdog_grace, mode)
    watchdog.start()
    try:
        outcome = runner.run(deps, mode, force=bool(s.args.force))
    finally:
        watchdog.stop()
    # A normal automatic run is silent; text only for problems (the runner opens the report itself).
    if outcome.console_text and not outcome.silent and (not auto or outcome.exit_code != 0):
        s.say(outcome.console_text)
    if auto and outcome.exit_code != 0 and outcome.report_path is None:
        # The runner could not report (e.g. the lock file cannot be created). Under pythonw the console text is lost,
        # so the user would never learn that nothing ran: write and open the minimal failure report ourselves.
        _auto_failure_report(
            s,
            ErrorInfo(
                "run_failed",
                outcome.console_text or "Der automatische Lauf ist fehlgeschlagen.",
                hint="Details stehen in der Logdatei (Ordner 'logs' im SchulSync-Datenordner).",
            ),
        )
    return outcome.exit_code


def _cmd_preview(s: _Session) -> int:
    deps = s.deps()
    outcome = runner.run(deps, RunMode.PREVIEW)
    if outcome.console_text:
        s.say(outcome.console_text)
    if outcome.report_path is not None:
        if deps.open_report(outcome.report_path):
            s.say(f"Die Vorschau wurde im Browser geöffnet ({outcome.report_path}).")
        else:
            s.say(f"Die Vorschau konnte nicht im Browser geöffnet werden. Datei: {outcome.report_path}")
    s.say("Es wurde nichts bei Google geschrieben und kein Tageserfolg verbucht.")
    return outcome.exit_code


def _cmd_restore(s: _Session) -> int:
    outcome = runner.restore(s.deps(), list(s.args.uids) or None)
    if outcome.console_text:
        s.say(outcome.console_text)
    return outcome.exit_code


def _cmd_report(s: _Session) -> int:
    path = latest_report(s.paths)
    if path is None:
        s.say("Es gibt noch keinen Bericht. Berichte entstehen bei 'schulsync run' oder 'schulsync preview'.")
        return 1
    if s.env.open_report(path):
        s.say(f"Der Bericht wurde im Browser geöffnet ({path}).")
        return 0
    s.say(f"Der Bericht konnte nicht im Browser geöffnet werden. Du findest ihn hier: {path}")
    return 1


# --------------------------------------------------------------------------- autostart


def _cmd_autostart(s: _Session) -> int:
    action = s.args.action
    if action == "install":
        result = s.env.autostart.install(s.paths)
    elif action == "remove":
        result = s.env.autostart.remove()
    else:
        result = s.env.autostart.status()
    s.say(result.details)
    return 0 if result.ok else 1


# --------------------------------------------------------------------------- probe output (check, setup)


def _login_verified(probe: ProbeReport) -> bool:
    """Both adapters report the login as the step named 'Anmeldung'."""
    return any(st.name == "Anmeldung" and st.ok for st in probe.steps)


def _print_probe(s: _Session, probe: ProbeReport) -> None:
    """Steps and categories of an adapter's self test. Everything that failed is shown IN FULL (message, error code, hint,
    technical details), so that the exact failure text can be copied into a bug report; `s.say` masks secrets."""
    for st in probe.steps:
        s.say(f"  [{'OK' if st.ok else 'FEHLER'}] {st.name}" + (f": {st.detail}" if st.detail else ""))
    s.say("  Datenkategorien (was wirklich gelesen werden kann):")
    if not probe.categories:
        s.say("    keine Kategorie geprüft (siehe Fehler oben)")
    for cat, oc in probe.categories.items():
        line = f"    {_CATEGORY_NAME.get(cat, str(cat))}: {_CATEGORY_STATUS.get(oc.status, str(oc.status))}"
        if oc.status == CategoryStatus.OK:
            line += f" ({oc.item_count} Einträge im Prüffenster)"
        s.say(line)
        if oc.error is not None:
            s.say(f"      {oc.error.message}")
            s.say(f"      Fehlercode: {oc.error.code}")
            if oc.error.hint:
                s.say(f"      Tipp: {oc.error.hint}")
            if oc.error.detail:
                s.say(f"      Technische Details: {oc.error.detail}")
        if oc.detail and oc.status != CategoryStatus.OK:
            s.say(f"      {oc.detail}")


def _unblock_if_login_ok(s: _Session, source: Source, probe: ProbeReport, deps: runner.Deps | None = None) -> None:
    if not _login_verified(probe):
        return
    with _state(s, deps) as store:
        if store is not None and store.is_auth_blocked(source):
            store.clear_auth_block(source)
            s.say(f"  Die Anmeldung bei {LABEL[source]} funktioniert wieder; die Sperre automatischer Läufe wurde aufgehoben.")


# --------------------------------------------------------------------------- check


def _problem_text(exc: Exception) -> str:
    """One line for the console; a bug in one probe must not stop the other probes (details go to the log)."""
    if isinstance(exc, SchulSyncError):
        return exc.message + (f" {exc.hint}" if exc.hint else "")
    log.error("Unerwarteter Fehler in der Prüfung: %s", type(exc).__name__, exc_info=exc)
    return f"Unerwarteter Fehler ({type(exc).__name__}). Technische Details stehen in der Logdatei."


def _problem_details(exc: Exception) -> list[str]:
    """What to copy into a bug report: error code and technical details of the exception (shown after `_problem_text`)."""
    if isinstance(exc, SchulSyncError):
        lines = [f"Fehlercode: {exc.code}"]
        if exc.detail:
            lines.append(f"Technische Details: {exc.detail}")
        return lines
    return [f"Technische Details: {type(exc).__name__}: {exc}"[:600]]


def _say_problem(s: _Session, prefix: str, exc: Exception) -> None:
    s.say(f"{prefix} {_problem_text(exc)}")
    for line in _problem_details(exc):
        s.say(f"{' ' * len(prefix)} {line}")


def _locked(s: _Session, command: Callable[[_Session], int]) -> int:
    """Run `command` under the program lock (non-blocking). `check` and `setup` may refresh or write the Google token and
    the secret store, which must not happen while a run (or the other command) does the same."""
    lock = ProcessLock(s.paths.lock)
    try:
        acquired = lock.acquire(blocking=False)
    except OSError as exc:  # lock file not creatable: do not run unprotected
        s.say(f"Die Prozesssperre konnte nicht angelegt werden ({type(exc).__name__}). Bitte Datenordner und Zugriffsrechte prüfen.")
        return 1
    if not acquired:
        log.info("Befehl '%s' wartet nicht: Die Prozesssperre ist belegt.", s.args.command)
        s.say(f"{LOCK_BUSY_TEXT} Ein Lauf, 'check' oder die Einrichtung ist noch aktiv; danach den Befehl erneut ausführen.")
        return 1
    try:
        return command(s)
    finally:
        lock.release()


def _cmd_check(s: _Session) -> int:
    return _locked(s, _check)


def _check(s: _Session) -> int:
    say = s.say
    hints: list[str] = []
    say("SchulSync - Prüfung (es wird nichts geschrieben)")
    say()
    try:
        cfg = s.load_config()
    except ConfigError as exc:
        say(f"[FEHLER] Konfiguration: {exc.message}")
        say(f"         {exc.hint}" if exc.hint else "")
        for line in _problem_details(exc):
            say(f"         {line}")
        return 2
    sources = [Source(n) for n in cfg.enabled_sources]
    say(f"[OK] Konfiguration gültig. Aktive Quellen: {', '.join(LABEL[x] for x in sources) or 'keine'}.")

    # secrets: names only, values are never read for display
    try:
        present = set(s.secrets.names())
    except SchulSyncError as exc:
        present = set()
        say(f"[FEHLER] Der geschützte Speicher ist nicht lesbar: {exc.message}")
        for line in _problem_details(exc):
            say(f"         {line}")
        hints.append("Bitte 'schulsync setup' ausführen, um die Zugangsdaten neu zu speichern.")
    required = [google_auth.SECRET_TOKEN] + [factories.PASSWORD_SECRETS[x] for x in sources]
    for name in required:
        if name in present:
            say(f"[OK] Gespeichert: {name}")
        else:
            say(f"[FEHLER] Nicht gespeichert: {name}")
            hints.append("Fehlende Zugangsdaten mit 'schulsync setup' (oder --google/--eduvidual/--webuntis) eintragen.")
    for x in sources:
        if not (cfg.eduvidual if x is Source.EDUVIDUAL else cfg.webuntis).username.strip():
            say(f"[FEHLER] Kein Benutzername für {LABEL[x]} in config.toml.")
            hints.append(f"Benutzername mit 'schulsync setup --{x.value}' eintragen.")

    deps = s.deps()
    say()
    say("Google:")
    try:
        target = deps.gateway_factory().check_targets()
        say(f"  [OK] Anmeldung gültig. Kalender '{target.calendar_summary}' ist erreichbar und beschreibbar (nicht der Hauptkalender).")
        say(f"  [OK] Aufgabenliste '{target.tasklist_title}' ist erreichbar.")
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, TargetsUnavailableError) and Target.CALENDAR in exc.verified:  # only the task list is the problem
            say(f"  [OK] Kalender '{cfg.google.calendar_name}' ist erreichbar und beschreibbar (nicht der Hauptkalender).")
        _say_problem(s, "  [FEHLER]", exc)
        hints.append("Google mit 'schulsync setup --google' neu einrichten bzw. neu anmelden.")

    for x in Source:
        say()
        say(f"{LABEL[x]}:")
        if x not in sources:
            say("  abgeschaltet (enabled = false in config.toml)")
            continue
        try:
            probe = deps.adapter_factories[x]().check()
        except Exception as exc:  # noqa: BLE001
            _say_problem(s, "  [FEHLER]", exc)
            hints.append(f"{LABEL[x]}: Zugangsdaten mit 'schulsync setup --{x.value}' eintragen bzw. prüfen.")
            continue
        _print_probe(s, probe)
        _unblock_if_login_ok(s, x, probe, deps)
        if not probe.ok:
            hints.append(f"{LABEL[x]}: Probleme siehe oben. Bei Anmeldefehlern 'schulsync setup --{x.value}' ausführen, sonst später erneut prüfen.")

    say()
    if hints:
        say("Ergebnis: Es gibt Probleme. Nächste Schritte:")
        for h in dict.fromkeys(hints):
            say(f"  - {h}")
        return 1
    say("Ergebnis: Alles in Ordnung. Nächste Schritte: 'schulsync preview' zeigt, was übernommen würde; 'schulsync run' gleicht ab; "
        "'schulsync autostart install' startet SchulSync nach jeder Windows-Anmeldung.")
    return 0


# --------------------------------------------------------------------------- status


_KIND_DE = {Kind.ASSIGNMENT: "Abgabe", Kind.HOMEWORK: "Hausaufgabe", Kind.EXAM: "Prüfung", Kind.CANCELLATION: "Entfall"}


def _status_week(say: Callable[[str], None], store: StateStore, today: date) -> None:
    """Wochenübersicht aus dem lokalen Stand (ohne Internet): was kommt in 7 Tagen, was ist neu."""
    end = today + timedelta(days=7)
    coming = sorted(
        (it for it in store.all_items() if not it.cancelled and today <= it.item.when.start_date < end),
        key=lambda it: (it.item.when.start_date, it.item.title),
    )
    say("Nächste 7 Tage:" if coming else "Nächste 7 Tage: nichts eingetragen.")
    for it in coming[:15]:
        t = it.item.when.start
        when = to_vienna(t).strftime("%a %d.%m. %H:%M") if isinstance(t, datetime) else t.strftime("%a %d.%m.")
        say(f"  {when}  {_KIND_DE[it.item.kind]}: {it.item.title}")
    if len(coming) > 15:
        say(f"  ... und {len(coming) - 15} weitere")
    fresh = sorted((it for it in store.all_items() if to_vienna(it.first_seen_at).date() >= today - timedelta(days=3)), key=lambda it: it.first_seen_at, reverse=True)
    if fresh:
        say("Neu erfasst (letzte 3 Tage): " + "; ".join(f"{_KIND_DE[it.item.kind]} {it.item.title}" for it in fresh[:5]) + (" ..." if len(fresh) > 5 else ""))


def _cmd_status(s: _Session) -> int:
    say = s.say
    cfg = s.config_or_none()
    today = today_vienna(s.env.clock())
    say("SchulSync - Status")
    say(f"Datenordner: {s.paths.home}")
    say("Konfiguration: " + ("gültig." if cfg else "fehlt oder ist fehlerhaft (schulsync setup / schulsync check)."))
    sources = [Source(n) for n in cfg.enabled_sources] if cfg else list(Source)
    with _state(s) as store:
        if store is None:
            say("Noch keine Daten: Es wurde noch kein Lauf durchgeführt.")
        else:
            run = store.last_run()
            if run is None:
                say("Letzter Lauf: noch keiner aufgezeichnet.")
            else:
                say(f"Letzter Lauf: {_fmt(run.finished_at)} ({_MODE.get(run.mode, run.mode)}), Ergebnis: {_OVERALL.get(run.status, run.status)}.")
            say("Quellen:")
            for x in sources:
                last = store.last_complete_fetch(x)
                day = store.get_source_day(today, x)
                if day is not None and day.done:
                    today_text = "heute erledigt"
                elif day is not None and day.fetch_complete:
                    today_text = "heute abgerufen, Google-Schreibvorgänge stehen noch aus"
                else:
                    today_text = "heute noch nicht erfolgreich"
                say(f"  {LABEL[x]}: letzter vollständiger Abruf {_fmt(last)}; {today_text}.")
                row = store.get_source_status(x)
                if row is not None and row.auth_blocked:
                    say(f"    Anmeldung abgelehnt seit {_fmt(row.auth_blocked_at)}: automatische Läufe überspringen diese Quelle. "
                        f"Lösung: 'schulsync setup --{x.value}' oder 'schulsync check'.")
            say(f"Ausstehende Google-Schreibvorgänge: {store.pending_count()}")
            _status_week(say, store, today)
            deleted = []
            for it in store.all_items():
                parts = [n for n, hit in (("Termin", it.event_state == EventState.USER_DELETED),
                                          ("Aufgabe", it.task_state == TaskState.USER_DELETED)) if hit]
                if parts:
                    deleted.append(f"  {it.uid} - {it.item.title} ({' und '.join(parts)} von dir gelöscht)")
            if deleted:
                say("Bewusst gelöschte Einträge (werden nicht neu angelegt):")
                for line in deleted:
                    say(line)
                say("  Wiederherstellen: schulsync restore <UID>  (ohne UID: alle)")
            else:
                say("Bewusst gelöschte Einträge: keine")
    try:
        say(f"Autostart: {s.env.autostart.status().details}")
    except Exception as exc:  # noqa: BLE001 - status must work even if the scheduler query fails
        log.warning("Autostart-Status nicht lesbar: %s", type(exc).__name__)
        say("Autostart: Der Zustand konnte nicht ermittelt werden.")
    latest = latest_report(s.paths)
    say(f"Letzter Bericht: {latest if latest else 'noch keiner'}")
    return 0


# --------------------------------------------------------------------------- setup


def _repo_example_config() -> Path:
    return frozen.resource("config.example.toml") or Path(__file__).resolve().parents[2] / "config.example.toml"


def _write_default_config(path: Path) -> None:
    """config.toml from the dataclass defaults (only used when config.example.toml is not available)."""
    path.write_text("# SchulSync-Konfiguration. Hier KEINE Passwörter eintragen (die liegen im geschützten Speicher).\n", encoding="utf-8")
    default = Config()
    for section in fields(Config):
        value = getattr(default, section.name)
        for f in fields(value):
            set_config_value(path, section.name, f.name, getattr(value, f.name))


def _ensure_config(s: _Session) -> None:
    s.paths.ensure()
    if s.paths.config.exists():
        s.say(f"Konfiguration: {s.paths.config} (vorhanden, wird beibehalten)")
        return
    example = s.env.example_config or _repo_example_config()
    if example.is_file():
        shutil.copyfile(example, s.paths.config)
        s.say(f"Konfiguration angelegt: {s.paths.config} (Kopie von {example.name})")
    else:
        _write_default_config(s.paths.config)
        s.say(f"Konfiguration angelegt: {s.paths.config} (aus den Standardwerten)")


def _stored(s: _Session, name: str) -> str | None:
    """A stored secret, or None. An entry that cannot be decrypted (other Windows user/PC) counts as absent: the wizard
    is exactly the place where the user replaces it."""
    try:
        return s.secrets.get(name)
    except ConfigError as exc:
        s.say(f"Hinweis: {exc.message} Der Eintrag wird bei dieser Einrichtung ersetzt.")
        return None


def _clean_path(text: str) -> str:
    return text.strip().strip('"').strip("'")


def _check_client_json(text: str) -> None:
    try:
        data = json.loads(text)
        ok = isinstance(data, dict) and isinstance(data.get("installed"), dict)
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError(
            "Die OAuth-Client-Datei ist ungültig.",
            hint="Im Google-Cloud-Projekt einen OAuth-Client vom Typ 'Desktop-App' erstellen und die JSON-Datei herunterladen.",
        )


def _register_json_secret(name: str, value: str) -> None:
    """Mask a secret that exists only in memory so far (nothing is stored yet): the whole text and its sensitive JSON leaves."""
    register_loaded_secrets(MemorySecretStore({name: value}), [name])


def _authorized_account(calendar_service: Any) -> str:
    """The Google account the login belongs to: the id of the primary calendar entry is the address of the account."""
    primary = call_api(calendar_service.calendarList().get(calendarId="primary"), op="Hauptkalender lesen", retries=1)
    account = str(primary.get("id") or "").strip()
    if not account:
        raise ConfigError("Das angemeldete Google-Konto konnte nicht ermittelt werden.", hint="Bitte später erneut versuchen.")
    return account


def _abort_google_setup(s: _Session, message: str, hint: str | None, *, relogin: bool) -> int:
    s.say(f"Abbruch: {message}" + (f" {hint}" if hint else ""))
    s.say(
        "Es wurde nichts gespeichert oder verändert (eine vorhandene Google-Anmeldung, die Client-Datei und die Ziele in "
        "config.toml bleiben, wie sie waren). Nach der Korrektur 'schulsync setup --google' erneut ausführen"
        + (" (die Anmeldung im Browser ist dann noch einmal nötig)." if relogin else ".")
    )
    return 1


def _setup_google(s: _Session) -> int | None:
    """None = done, otherwise the exit code of the aborted setup.

    Order (H1): authorize (the new credentials stay in MEMORY) -> account the login belongs to -> calendar 'Schule' ->
    task list -> only then store client file, token and ids. Any abort before that leaves the previous state untouched."""
    say = s.say
    cfg = s.load_config(reload=True)
    say()
    say("Schritt: Google (Kalender und Aufgaben)")
    say("Dafür brauchst du die OAuth-Client-Datei (Typ 'Desktop-App') aus deinem Google-Cloud-Projekt; die Anleitung steht in der README.")
    say("Hinweis: Solange die Google-App im Status 'Testing' steht, läuft die Anmeldung nach 7 Tagen ab; SchulSync öffnet dann beim "
        "nächsten Lauf automatisch den Google-Login. Mit 'In Produktion' entfällt das, Google verlangt dafür aber eine Startseite und "
        "eine Datenschutzerklärung (Details in der README). Eine unbegrenzte Gültigkeit kann niemand garantieren.")
    existing = _stored(s, google_auth.SECRET_CLIENT)
    bundled = google_auth.bundled_client_json()
    new_client_json: str | None = None  # a client file found/given now is stored at the very end
    if bundled:
        say("OAuth-Client-Datei im Projektordner gefunden und verwendet.")
        client_json = new_client_json = bundled
        _check_client_json(client_json)
        _register_json_secret(google_auth.SECRET_CLIENT, client_json)
    else:
        answer = _clean_path(s.env.ask(
            "Pfad zur OAuth-Client-JSON-Datei" + (" (Enter = vorhandene behalten)" if existing else "") + ": "))
        if answer:
            try:
                client_json = Path(answer).expanduser().read_text(encoding="utf-8-sig")
            except OSError:
                say("Die Datei konnte nicht gelesen werden. Pfad prüfen und 'schulsync setup --google' wiederholen.")
                return 1
            _check_client_json(client_json)
            _register_json_secret(google_auth.SECRET_CLIENT, client_json)
            new_client_json = client_json
        elif existing:
            client_json = existing
        else:
            say("Ohne die OAuth-Client-Datei kann Google nicht eingerichtet werden. Bitte 'schulsync setup --google' mit dem Pfad wiederholen.")
            return 1

    creds = None
    if new_client_json is None and _stored(s, google_auth.SECRET_TOKEN):
        try:
            creds = google_auth.load_credentials(s.secrets, timeout=cfg.retry.read_timeout)
        except GoogleAuthError as exc:
            say(f"Die gespeicherte Google-Anmeldung ist nicht mehr gültig ({exc.message}). Es wird neu angemeldet.")
        else:
            register_loaded_secrets(s.secrets, [google_auth.SECRET_TOKEN])
            if s.yes("Google ist bereits angemeldet. Erneut anmelden?", False):
                creds = None
    fresh_login = creds is None  # a login made now is stored only after the checks below
    if creds is None:
        say("Der Browser öffnet sich jetzt für die Google-Anmeldung. Bitte alle Kästchen (Kalender und Aufgaben) aktivieren.")
        creds = s.env.authorize(client_json)
        _register_json_secret(google_auth.SECRET_TOKEN, google_auth.credentials_to_json(creds))

    # 1. the account this login belongs to
    try:
        calendar_service, tasks_service = s.env.google_services(creds, cfg)
        login = _authorized_account(calendar_service)
    except SchulSyncError as exc:
        return _abort_google_setup(s, f"Das angemeldete Google-Konto konnte nicht geprüft werden: {exc.message}", exc.hint, relogin=fresh_login)
    expected = cfg.google.account.strip()
    if expected and login.lower() != expected.lower():
        say(f"Abbruch: Du hast dich bei Google als {login} angemeldet, in config.toml steht aber das Konto {expected} "
            "(Abschnitt [google] account).")
        say(f"Hast du mehrere Google-Konten? Dann wähle im Browser bei der Anmeldung das richtige Konto ({expected}) aus und "
            "führen 'schulsync setup --google' erneut aus"
            + ("." if fresh_login else " (bei der Frage 'Erneut anmelden?' mit 'j' antworten)."))
        say(f"Möchtest du bewusst das Konto {login} verwenden, trage es zuerst in config.toml im Abschnitt [google] bei 'account' ein.")
        say("Es wurde nichts gespeichert oder verändert: Eine vorhandene Google-Anmeldung, die Client-Datei und die Ziele in config.toml "
            "bleiben, wie sie waren.")
        if fresh_login:
            say(f"Hinweis: Google hat dem Zugriff von SchulSync auf das Konto {login} dabei bereits zugestimmt. Wenn du das nicht willst, "
                "entferne den Zugriff in den Einstellungen dieses Google-Kontos unter 'Sicherheit'.")
        return 1
    say(f"  Google-Konto: {login}.")

    # 2. the calendar (never the primary one)
    try:
        calendar = google_auth.find_calendar_by_name(calendar_service, cfg.google.calendar_name)
    except SchulSyncError as exc:
        return _abort_google_setup(s, exc.message, exc.hint, relogin=fresh_login)
    say(f"  Kalender '{cfg.google.calendar_name}' gefunden und beschreibbar (nicht der Hauptkalender).")

    # 3. the task list
    try:
        tasklist = google_auth.find_or_create_tasklist(tasks_service, cfg.google.tasklist_title)
    except SchulSyncError as exc:
        return _abort_google_setup(s, exc.message, exc.hint, relogin=fresh_login)
    say(f"  Aufgabenliste '{cfg.google.tasklist_title}' " + ("neu angelegt." if tasklist.created else "gefunden."))
    if tasklist.note:
        say(f"  Hinweis: {tasklist.note}")
    say("  Hinweis: SchulSync verändert nur Einträge mit eigener Projektkennung und lässt fremde Einträge unangetastet. "
        "Läuft noch eine alte Schulautomation (Skript, Kalender-Plugin, Zapier o. Ä.), die in denselben Kalender oder dieselbe "
        "Aufgabenliste schreibt, können doppelte Einträge entstehen. Bitte schalte eine solche Automation ab.")

    # 4. everything is verified: now (and only now) persist
    if new_client_json is not None:
        s.secrets.set(google_auth.SECRET_CLIENT, new_client_json)
    if fresh_login:
        google_auth.save_credentials(creds, s.secrets)
        register_loaded_secrets(s.secrets, [google_auth.SECRET_TOKEN])
        say("Google-Anmeldung gespeichert.")
    set_config_value(s.paths.config, "google", "calendar_id", calendar["id"])
    set_config_value(s.paths.config, "google", "tasklist_id", tasklist.id)
    s.load_config(reload=True)
    say("  Die Ziele wurden in config.toml gespeichert.")
    return None


def _setup_source(s: _Session, source: Source) -> None:
    say = s.say
    label = LABEL[source]
    section = source.value
    secret = factories.PASSWORD_SECRETS[source]
    cfg = s.load_config(reload=True)
    current = (cfg.eduvidual if source is Source.EDUVIDUAL else cfg.webuntis).username
    say()
    say(f"Schritt: {label}")
    answer = s.env.ask(f"{label}-Benutzername" + (f" [aktuell: {current}]" if current else "") + " (Enter = behalten): ").strip()
    username = answer or current
    if answer and answer != current:
        set_config_value(s.paths.config, section, "username", answer)
    has_password = bool(_stored(s, secret))
    password = s.env.ask_secret(
        f"{label}-Passwort (die Eingabe bleibt unsichtbar" + (", Enter = behalten" if has_password else "") + "): ")
    if password:
        register_secret(password)
        s.secrets.set(secret, password)
        has_password = True
        say("  Passwort geschützt gespeichert.")
    if not username or not has_password:
        say(f"  {label} ist noch unvollständig (Benutzername und Passwort nötig); 'schulsync setup --{source.value}' wiederholen.")
        return
    cfg = s.load_config(reload=True)
    if not s.yes(f"Anmeldung bei {label} jetzt testen?", True):
        return
    try:
        probe = s.env.make_adapter(source, cfg, s.secrets).check()
    except Exception as exc:  # noqa: BLE001 - like `check`: a bug in one probe must not abort the wizard half-way
        _say_problem(s, "  [FEHLER]", exc)
        return
    _print_probe(s, probe)
    _unblock_if_login_ok(s, source, probe)


def _offer_autostart(s: _Session) -> None:
    say = s.say
    say()
    say("Schritt: Autostart")
    try:
        current = s.env.autostart.status()
    except Exception as exc:  # noqa: BLE001 - an unreadable scheduler must not abort the wizard
        log.warning("Autostart-Status nicht lesbar: %s", type(exc).__name__)
        current = None
    if current is not None and current.ok:
        say(f"  {current.details}")
        return
    if s.yes("SchulSync nach jeder Windows-Anmeldung automatisch starten (Autostart einrichten)?", False):
        result = s.env.autostart.install(s.paths)
        say(f"  {result.details}")
    else:
        say("  Kein Autostart eingerichtet. Später möglich mit: schulsync autostart install")


def _cmd_setup(s: _Session) -> int:
    # The wizard rewrites secrets.dat and config.toml; a running sync could refresh the Google token at the same time,
    # so both exclude each other via the process lock (the secret store itself serialises its writers as well).
    return _locked(s, _setup)


def _setup(s: _Session) -> int:
    only = [n for n in ("google", "eduvidual", "webuntis") if getattr(s.args, n)]
    everything = not only
    s.say("SchulSync - Einrichtung (Eingaben mit Enter bestätigen; leere Eingabe behält den vorhandenen Wert)")
    _ensure_config(s)
    s.load_config(reload=True)
    if everything or "google" in only:
        code = _setup_google(s)
        if code is not None:
            return code
    for source in Source:
        if everything or source.value in only:
            _setup_source(s, source)
    if everything:
        _offer_autostart(s)
    s.say()
    s.say("Die Einrichtung ist abgeschlossen. Nächste Schritte: 'schulsync check' prüft alle Zugänge, "
          "'schulsync preview' zeigt, was übernommen würde, 'schulsync run' gleicht ab.")
    return 0


# --------------------------------------------------------------------------- argument parser (German)

def _cmd_update(s: _Session) -> int:
    return updater.run_update(s.say, check_only=bool(s.args.check))


def _cmd_install(s: _Session) -> int:
    if not frozen.is_frozen():
        s.say("'install' gibt es nur in der fertigen schulsync.exe. Aus dem Quellcode: scripts\\install.ps1.")
        return 1
    frozen.install(s.say)
    if s.yes("Jetzt einrichten (Google, Eduvidual, WebUntis)?", True):
        return _cmd_setup(s)
    s.say("Später mit 'schulsync setup' einrichten.")
    return 0


def _cmd_uninstall(s: _Session) -> int:
    s.say("SchulSync wird entfernt: Autostart, Programm und PATH-Eintrag.")
    s.say(s.env.autostart.remove().details)
    if not frozen.is_frozen():
        s.say("Quellcode-Version: Lösche den Projektordner und die .venv selbst.")
        return 0
    data = s.paths.home if s.args.data and s.yes(f"Auch Einstellungen, Passwörter und Datenbank in {s.paths.home} unwiderruflich löschen?", False) else None
    frozen.uninstall(s.say, data_dir=data)
    return 0


_COMMANDS: dict[str, Callable[[_Session], int]] = {
    "uninstall": _cmd_uninstall,
    "update": _cmd_update,
    "install": _cmd_install,
    "setup": _cmd_setup,
    "run": _cmd_run,
    "preview": _cmd_preview,
    "check": _cmd_check,
    "status": _cmd_status,
    "restore": _cmd_restore,
    "report": _cmd_report,
    "autostart": _cmd_autostart,
}

_ARGPARSE_DE = (
    (re.compile(r"the following arguments are required: (.+)"), r"Es fehlt: \1"),
    (re.compile(r"unrecognized arguments: (.+)"), r"Unbekannte Argumente: \1"),
    (re.compile(r"invalid choice: (.+?) \(choose from (.+)\)"), r"Ungültige Auswahl \1 (erlaubt: \2)"),
    (re.compile(r"expected one argument"), "Es fehlt ein Wert"),
)


def _german(message: str) -> str:
    for rx, repl in _ARGPARSE_DE:
        found = rx.search(message)
        if found:
            return rx.sub(repl, found.group(0))
    return message


def _build_parser(say: Callable[[str], None]) -> argparse.ArgumentParser:
    class Formatter(argparse.HelpFormatter):
        def add_usage(self, usage, actions, groups, prefix=None):  # noqa: ANN001
            super().add_usage(usage, actions, groups, "Aufruf: " if prefix is None else prefix)

    class Parser(argparse.ArgumentParser):
        def __init__(self, *a: Any, **kw: Any):
            kw.setdefault("add_help", False)
            kw.setdefault("formatter_class", Formatter)
            super().__init__(*a, **kw)
            self._positionals.title = "Argumente"
            self._optionals.title = "Optionen"
            self.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help="Diese Hilfe anzeigen")

        def _print_message(self, message: str, file: Any = None) -> None:
            if message:
                say(message.rstrip("\n"))

        def error(self, message: str) -> None:  # type: ignore[override]
            self.print_usage()
            self.exit(2, f"{self.prog}: Fehler im Aufruf. {_german(message)}\n")

    def common(p: argparse.ArgumentParser, *, top: bool) -> None:
        # behind the sub-command the defaults are SUPPRESSed so that they never overwrite a value given before it
        p.add_argument("--home", metavar="ORDNER", default=None if top else argparse.SUPPRESS,
                       help="Datenordner (sonst Umgebungsvariable SCHULSYNC_HOME, sonst %%LOCALAPPDATA%%\\SchulSync)")
        p.add_argument("-v", "--verbose", action="store_true", default=False if top else argparse.SUPPRESS,
                       help="Protokollzeilen zusätzlich in der Konsole anzeigen")

    parser = Parser(
        prog=PROG,
        description="SchulSync: gleicht Eduvidual und WebUntis nach festen Regeln (ohne KI) mit Google Kalender und Google Tasks "
        "'Schule' ab. Die Schulplattformen werden nur gelesen.",
    )
    parser.add_argument("--version", action="version", version=f"SchulSync {__version__}", help="Programmversion anzeigen")
    common(parser, top=True)
    sub = parser.add_subparsers(dest="command", title="Befehle", metavar="BEFEHL")

    def add(name: str, summary: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=summary, description=summary)
        common(p, top=False)
        return p

    p = add("setup", "Einrichtungsassistent (wiederholbar; leere Eingabe behält den vorhandenen Wert)")
    p.add_argument("--google", action="store_true", help="nur Google einrichten bzw. neu anmelden")
    p.add_argument("--eduvidual", action="store_true", help="nur Eduvidual einrichten")
    p.add_argument("--webuntis", action="store_true", help="nur WebUntis einrichten")
    p = add("run", "Tageslauf: Quellen lesen und nach Google schreiben")
    p.add_argument("--auto", action="store_true", help="Autostart-Modus: still, wenn nichts zu tun ist; Bericht im Browser nur bei Problemen")
    p.add_argument("--force", action="store_true", help="Tagesbegrenzung umgehen (manueller Start)")
    add("preview", "Vorschau: zeigt die geplanten Änderungen, schreibt nichts und verbucht keinen Erfolg")
    add("check", "Prüft Konfiguration, Zugangsdaten, Google-Ziele und je Quelle, welche Datenkategorien wirklich lesbar sind")
    add("status", "Lokaler Überblick ohne Internet: letzter Lauf, Quellen, Sperren, ausstehende und gelöschte Einträge, Autostart")
    p = add("restore", "Von dir in Google gelöschte Einträge ausdrücklich wiederherstellen")
    p.add_argument("uids", nargs="*", metavar="UID", help="Kennungen laut 'schulsync status' (ohne Angabe: alle)")
    add("report", "Den neuesten Bericht im Browser öffnen")
    p = add("update", "Prüft, ob es eine neue Version gibt, und installiert sie (nur fertige schulsync.exe)")
    p.add_argument("--check", action="store_true", help="nur prüfen, nichts installieren (Exit-Code 10 = neue Version vorhanden)")
    p = add("uninstall", "Entfernt Autostart, Programm und PATH-Eintrag (Daten bleiben, außer mit --data)")
    p.add_argument("--data", action="store_true", help="auch Einstellungen, Passwörter und Datenbank löschen (mit Rückfrage)")
    add("install", "Installiert die schulsync.exe für deinen Benutzer (Programme-Ordner und PATH)")
    p = add("autostart", "Start nach der Windows-Anmeldung einrichten, entfernen oder anzeigen")
    p.add_argument("action", choices=("install", "remove", "status"), metavar="install|remove|status", help="Aktion")
    return parser


# --------------------------------------------------------------------------- entry point


def _bootstrap(s: _Session) -> None:
    """Data directory and file logging; failures are logged/ignored, a command must still be able to report them."""
    try:
        s.paths.ensure()
        cfg = s.config_or_none()
        setup_logging(
            s.paths,
            console=bool(getattr(s.args, "verbose", False)),
            keep_days=cfg.report.log_keep_days if cfg else 30,
            now=s.env.clock(),
        )
    except OSError as exc:
        s.say(f"Hinweis: Der Datenordner {s.paths.home} ist nicht beschreibbar ({type(exc).__name__}); es gibt keine Logdatei.")


def main(argv: list[str] | None = None, *, env: CliEnv | None = None) -> int:
    env = env or CliEnv()
    parser = _build_parser(env.say)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version, usage error
        return exc.code if isinstance(exc.code, int) else 0
    if not args.command:
        parser.print_help()
        return 2
    session = _Session(args, env)
    try:
        _bootstrap(session)
        log.info("Befehl '%s' gestartet (SchulSync %s).", args.command, __version__)
        code = _COMMANDS[args.command](session)
        log.info("Befehl '%s' beendet mit Exit-Code %s.", args.command, code)
        return code
    except KeyboardInterrupt:
        env.say("Abgebrochen.")
        return 1
    except Exception as exc:  # noqa: BLE001 - last line of defence: German message, log entry, exit code
        return _fail(session, exc)
    finally:
        shutdown_logging()


def entry() -> int:
    """Einstieg der fertigen schulsync.exe. Ohne Befehl (Doppelklick) wird installiert; im Autostart verschwindet das Fenster."""
    argv = sys.argv[1:]
    frozen.remove_old_exe()
    auto = "run" in argv and "--auto" in argv
    if auto:
        frozen.hide_console()
    if not argv:
        argv = ["install"]
    pause = frozen.own_console() and not auto
    code = main(argv)
    if pause:
        input("Enter drücken zum Schließen ...")
    return code
