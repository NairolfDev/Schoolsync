"""CLI level tests of the hardening work.

H1  setup wizard: wrong-account protection; nothing is persisted before account, calendar and task list are verified.
H3  `check` and `setup` take the program lock (`status` / `report` stay lock-free).
H4  `run` starts a hard watchdog (injectable exit): FAILED report, os._exit(3) after run_timeout_seconds + 60 s.
H8  `check` prints technical details and hints of every failure, all console output is redacted.
H6  unreadable state database: the CLI reports once per day (the runner no longer has a JSON memory for it).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta

import pytest
from google.oauth2.credentials import Credentials

from schulsync import cli, google_auth, runner
from schulsync.cli import main
from schulsync.config import load_config, set_config_value
from schulsync.errors import ConfigError, ErrorInfo, NetworkError, TargetUnavailableError, TransientError
from schulsync.lock import ProcessLock
from schulsync.models import Category, CategoryOutcome, CategoryStatus, ProbeReport, ProbeStep, Source
from schulsync.redact import register_secret
from schulsync.state import StateStore
from tests.support import samples
from tests.support.fake_adapter import FakeAdapter

# the fixtures and doubles of tests/test_cli.py (fixtures must be imported by name to be visible here)
from tests.test_cli import (  # noqa: F401
    expect_account,
    ACCOUNT,
    CLIENT_SECRET,
    PW_EDU,
    REFRESH,
    CalService,
    Req,
    SetupWorld,
    TaskService,
    UnexpectedQuestion,
    _clean,
    all_logs,
    autostart_fake,
    cal,
    check_env,
    clock,
    config_file,
    creds,
    env,
    io,
    make_deps,
    opened,
    outcome,
    probe_ok,
    store,
    with_usernames,
    world,
)

OTHER = "jemand.anderes@example.com"


def other_creds() -> Credentials:
    return Credentials(
        token="ya29.dummy-other-access", refresh_token="1//other-dummy-refresh", token_uri="https://oauth2.googleapis.com/token",
        client_id="other.apps.googleusercontent.com", client_secret="GOCSPX-other-dummy", scopes=list(google_auth.SCOPES),
    )


def snapshot(world: SetupWorld) -> dict:
    return {
        "secrets": {n: world.store.get(n) for n in world.store.names()},
        "config": world.paths.config.read_text(encoding="utf-8") if world.paths.config.exists() else None,
    }


# ====================================================================================== H1


class TestSetupWrongAccount:
    @pytest.fixture(autouse=True)
    def _expected_account(self, world):
        expect_account(world)

    def test_a_first_setup_with_the_wrong_account_stops_and_persists_nothing(self, world):
        w = world
        w.cal_service.primary_id = OTHER
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 1
        out = w.io.out
        assert "Abbruch" in out and OTHER in out and ACCOUNT in out
        assert "mehrere Google-Konten" in out and "Browser" in out and "[google] account" in out
        assert w.store.names() == []  # neither client file nor token
        assert load_config(w.paths.config).google.calendar_id == "" and load_config(w.paths.config).google.tasklist_id == ""
        assert w.task_service.inserted == []  # no task list was created behind the wrong account
        assert w.authorized  # the browser login did happen, only its result is thrown away
        assert "Zugriff" in out and "'Sicherheit'" in out  # Google already granted the wrong account: how to take it back

    def test_a_rerun_with_the_wrong_account_keeps_old_token_client_and_ids(self, world):
        w = world
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 0
        before = snapshot(w)
        # the user now picks another Google account in the browser; a different client file as well
        new_client = w.client_file.with_name("client_other.json")
        new_client.write_text(json.dumps({"installed": {"client_id": "other.apps.googleusercontent.com", "client_secret": "GOCSPX-other-dummy"}}), encoding="utf-8")
        w.env.authorize = lambda cj: other_creds()
        w.cal_service.primary_id = OTHER
        w.cal_service.entries = [cal("neu-1", "Schule")]
        w.io.answers.append(str(new_client))
        assert main(["setup", "--google"], env=w.env) == 1
        assert snapshot(w) == before
        assert json.loads(w.store.get("google_token"))["refresh_token"] == REFRESH
        assert json.loads(w.store.get("google_client"))["installed"]["client_secret"] == CLIENT_SECRET
        assert "GOCSPX-other-dummy" not in w.io.out and "1//other-dummy-refresh" not in w.io.out

    def test_a_rerun_that_keeps_the_stored_login_also_checks_the_account(self, world):
        w = world
        w.io.answers.append(str(w.client_file))
        main(["setup", "--google"], env=w.env)
        before = snapshot(w)
        w.authorized.clear()
        w.cal_service.primary_id = OTHER  # e.g. config.toml was edited, or the stored token belongs to another account
        w.io.answers.extend(["", "n"])  # keep the client file, do not log in again
        assert main(["setup", "--google"], env=w.env) == 1
        assert w.authorized == [] and snapshot(w) == before
        assert "Erneut anmelden" in "\n".join(w.io.prompts) and "mit 'j'" in w.io.out

    def test_the_account_comparison_ignores_case_and_whitespace(self, world):
        w = world
        w.cal_service.primary_id = ACCOUNT.upper()
        w.paths.config.write_text(f'[google]\naccount = "  {ACCOUNT.title()} "\n', encoding="utf-8")
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 0
        assert sorted(w.store.names()) == ["google_client", "google_token"]

    def test_a_deliberately_edited_account_in_the_config_is_accepted(self, world):
        w = world
        w.cal_service.primary_id = OTHER
        w.paths.config.write_text(f'[google]\naccount = "{OTHER}"\n', encoding="utf-8")
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 0
        assert load_config(w.paths.config).google.calendar_id == "cal-1"

    def test_the_account_is_checked_before_the_calendar_is_searched(self, world):
        w = world
        w.cal_service.primary_id = OTHER
        w.cal_service.entries = []  # would be "no calendar" otherwise
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 1
        assert "mehrere Google-Konten" in w.io.out and "Kein Kalender" not in w.io.out

    def test_an_account_that_cannot_be_determined_stops_too(self, world, monkeypatch):
        w = world

        class Broken(CalService):
            def get(self, calendarId):
                return Req(NetworkError("Keine Verbindung zu Google.", hint="Internet prüfen."))

        w.env.google_services = lambda c, cfg: (Broken(w.cal_service.entries), w.task_service)
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 1
        assert "konnte nicht geprüft werden" in w.io.out and "Keine Verbindung zu Google." in w.io.out
        assert w.store.names() == [] and w.task_service.inserted == []

    def test_an_empty_account_in_the_config_means_no_expectation(self, world):
        w = world
        w.cal_service.primary_id = OTHER
        w.paths.config.write_text('[google]\naccount = ""\n', encoding="utf-8")
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 0


class TestSetupPersistsLast:
    CASES = [
        ("no_calendar", lambda w: setattr(w.cal_service, "entries", [])),
        ("two_calendars", lambda w: setattr(w.cal_service, "entries", [cal("s1"), cal("s2")])),
        ("primary_named_schule", lambda w: setattr(w.cal_service, "entries", [cal(ACCOUNT, "Schule", primary=True)])),
        ("read_only", lambda w: setattr(w.cal_service, "entries", [cal("s1", role="reader")])),
    ]

    @pytest.mark.parametrize("name, break_it", CASES, ids=[c[0] for c in CASES])
    def test_a_failed_calendar_lookup_keeps_the_old_state_after_a_good_setup(self, name, break_it, world):
        w = world
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 0
        before = snapshot(w)
        break_it(w)
        w.env.authorize = lambda cj: other_creds()
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 1
        assert snapshot(w) == before
        assert "Abbruch" in w.io.out and "nichts gespeichert oder verändert" in w.io.out

    def test_a_failed_tasklist_step_keeps_the_old_state_and_the_new_login_is_not_stored(self, world):
        w = world
        w.io.answers.append(str(w.client_file))
        main(["setup", "--google"], env=w.env)
        before = snapshot(w)

        class Failing(TaskService):
            def list(self, **kw):
                return Req(TargetUnavailableError("Aufgabenlisten nicht lesbar.", hint="Später erneut versuchen."))

        w.env.google_services = lambda c, cfg: (w.cal_service, Failing())
        w.env.authorize = lambda cj: other_creds()
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 1
        assert snapshot(w) == before and "Aufgabenlisten nicht lesbar." in w.io.out

    def test_the_login_is_stored_only_after_calendar_and_list_are_verified(self, world):
        """Order of events: authorize -> account -> calendar -> task list -> THEN client file, token and ids."""
        w = world
        events: list[str] = []
        real_set = w.store.set
        w.store.set = lambda name, value: (events.append(f"set:{name}"), real_set(name, value))[1]
        w.env.authorize = lambda cj: (events.append("authorize"), creds())[1]

        class Cal(CalService):
            def get(self, calendarId):
                events.append("account")
                return super().get(calendarId)

            def list(self, **kw):
                events.append("calendar")
                return super().list(**kw)

        class Tasks(TaskService):
            def list(self, **kw):
                events.append("tasklist")
                return super().list(**kw)

        w.env.google_services = lambda c, cfg: (Cal(w.cal_service.entries), Tasks(w.task_service.lists))
        w.io.answers.append(str(w.client_file))
        assert main(["setup", "--google"], env=w.env) == 0
        assert events[:4] == ["authorize", "account", "calendar", "tasklist"]
        assert sorted(e for e in events[4:]) == ["set:google_client", "set:google_token"]
        assert "Google-Anmeldung gespeichert" in w.io.out

    def test_secrets_of_an_unsaved_login_are_still_masked_in_the_output(self, world):
        w = world
        w.cal_service.primary_id = OTHER
        w.io.answers.append(str(w.client_file))
        main(["setup", "--google"], env=w.env)
        haystack = w.io.out + all_logs(w.paths)
        for secret in (CLIENT_SECRET, REFRESH, "ya29.dummy-access"):
            assert secret not in haystack


# ====================================================================================== H3 locks


class TestProgramLock:
    def test_check_refuses_while_the_program_lock_is_held(self, env, io, paths, config_file):
        with ProcessLock(paths.lock):
            assert main(["check"], env=env) == 1  # env.build_deps would raise UnexpectedQuestion: not reached
        assert "SchulSync läuft gerade, bitte kurz warten" in io.out

    def test_check_holds_the_lock_while_it_runs_and_releases_it_afterwards(self, env, io, paths, config_file, store):
        with_usernames(paths)
        seen = {}

        class Probing(FakeAdapter):
            def check(self):
                lock = ProcessLock(paths.lock)
                seen["free_during_check"] = lock.acquire()
                lock.release()
                return probe_ok(Source.EDUVIDUAL)

        check_env(env, paths, store, adapters={Source.EDUVIDUAL: Probing(Source.EDUVIDUAL), Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS))})
        assert main(["check"], env=env) == 0
        assert seen == {"free_during_check": False}
        lock = ProcessLock(paths.lock)
        assert lock.acquire()  # released after the command
        lock.release()

    def test_setup_prints_the_same_wait_message(self, world, paths):
        with ProcessLock(paths.lock):
            assert main(["setup"], env=world.env) == 1
        assert "SchulSync läuft gerade, bitte kurz warten" in world.io.out and world.authorized == []

    def test_status_and_report_never_take_the_lock(self, env, io, paths, opened):
        (paths.reports / "latest.html").write_text("<html></html>", encoding="utf-8")
        with ProcessLock(paths.lock):
            assert main(["status"], env=env) == 0
            assert main(["report"], env=env) == 0
        assert "SchulSync - Status" in io.out and opened == [paths.reports / "latest.html"]

    def test_a_lock_file_that_cannot_be_created_is_a_german_message_not_a_traceback(self, env, io, config_file, monkeypatch):
        monkeypatch.setattr(cli.ProcessLock, "acquire", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "gesperrt")))
        assert main(["check"], env=env) == 1
        assert "Prozesssperre" in io.out and "PermissionError" in io.out and "Traceback" not in io.out


# ====================================================================================== H4 watchdog


class Killed(BaseException):
    """What is left of the main thread once the watchdog has 'ended the process' (a BaseException: nothing may catch it)."""


class Hang:
    """A `runner.run` that never comes back by itself (a hung HTTP call) until the watchdog 'kills the process'."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.exit_codes: list[int] = []
        self.exited_at: float | None = None
        self.started_at = time.monotonic()

    def run(self, deps, mode, *, force=False):
        self.release.wait(30)
        raise Killed  # in reality the process is gone before it could come back

    def hard_exit(self, code: int) -> None:
        self.exit_codes.append(code)
        self.exited_at = time.monotonic()
        self.release.set()  # in reality the process is gone here; the test lets the stuck call return


def run_cli(env, *argv: str) -> None:
    with pytest.raises(Killed):
        main(list(argv), env=env)


def watched(env, paths, monkeypatch, *, timeout=0.2, grace=0.0):
    set_config_value(paths.config, "retry", "run_timeout_seconds", timeout)
    hang = Hang()
    monkeypatch.setattr(runner, "run", hang.run)
    env.build_deps = lambda p, c, s: make_deps(p, c)
    env.hard_exit = hang.hard_exit
    env.watchdog_grace = grace
    return hang


class TestWatchdog:
    def test_a_hung_run_is_killed_after_timeout_plus_grace_with_a_failed_report(self, env, io, paths, config_file, monkeypatch, opened):
        hang = watched(env, paths, monkeypatch, timeout=0.3, grace=0.3)
        run_cli(env, "run", "--auto")
        assert hang.exit_codes == [3]
        assert hang.exited_at - hang.started_at >= 0.55  # run_timeout_seconds + grace, not earlier
        report = paths.reports / "bericht-2026-10-14.html"
        assert report.is_file() and opened == [report]
        text = report.read_text(encoding="utf-8")
        assert "Fehlgeschlagen" in text and "timeout" in text and "hart abgebrochen" in text
        assert "Internetverbindung" in text and "schulsync check" in text  # the specific next step of H9, not the generic one
        assert "nicht genauer einordnen" not in text
        assert "Hartes Zeitlimit" in all_logs(paths) and "Exit-Code 3" in all_logs(paths)

    def test_the_default_grace_is_sixty_seconds_and_the_default_exit_is_os_exit(self):
        import os

        e = cli.CliEnv()
        assert e.watchdog_grace == 60.0 and e.hard_exit is os._exit

    def test_a_normal_fast_run_never_fires_and_leaves_no_thread_behind(self, env, io, paths, config_file, monkeypatch):
        set_config_value(paths.config, "retry", "run_timeout_seconds", 0.2)
        monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: outcome(0, silent=True))
        env.build_deps = lambda p, c, s: make_deps(p, c)
        fired: list[int] = []
        env.hard_exit = fired.append
        env.watchdog_grace = 0.0
        assert main(["run", "--auto"], env=env) == 0
        time.sleep(0.6)  # well past timeout + grace
        assert fired == [] and not [t for t in threading.enumerate() if t.name == "schulsync-watchdog"]
        assert list(paths.reports.glob("*.html")) == []

    def test_a_run_that_fails_normally_does_not_trigger_the_watchdog_either(self, env, io, paths, config_file, monkeypatch):
        set_config_value(paths.config, "retry", "run_timeout_seconds", 0.2)
        monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: outcome(1, "Eduvidual fehlgeschlagen."))
        env.build_deps = lambda p, c, s: make_deps(p, c)
        fired: list[int] = []
        env.hard_exit = fired.append
        env.watchdog_grace = 0.0
        assert main(["run"], env=env) == 1
        time.sleep(0.5)
        assert fired == []

    def test_a_manual_run_is_guarded_too_and_the_report_says_manual(self, env, io, paths, config_file, monkeypatch):
        hang = watched(env, paths, monkeypatch)
        run_cli(env, "run")
        assert hang.exit_codes == [3]
        assert "Manuell" in (paths.reports / "bericht-2026-10-14.html").read_text(encoding="utf-8")
        assert "hart abgebrochen" in io.out  # the user at the console is told as well

    def test_the_process_still_ends_when_the_report_cannot_be_written(self, env, io, paths, config_file, monkeypatch):
        hang = watched(env, paths, monkeypatch)
        monkeypatch.setattr(cli, "write_report", lambda *a, **k: (_ for _ in ()).throw(OSError("Datenträger voll")))
        run_cli(env, "run", "--auto")
        assert hang.exit_codes == [3]

    def test_even_a_crashing_report_path_cannot_stop_the_exit(self, env, io, paths, config_file, monkeypatch):
        hang = watched(env, paths, monkeypatch)
        monkeypatch.setattr(cli, "_auto_failure_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaputt")))
        run_cli(env, "run", "--auto")
        assert hang.exit_codes == [3]

    def test_the_timeout_report_opens_only_once_per_day(self, env, io, paths, config_file, monkeypatch, opened):
        for _ in range(2):
            hang = watched(env, paths, monkeypatch)
            run_cli(env, "run", "--auto")
            assert hang.exit_codes == [3]
        assert len(opened) == 1  # same kind of error, same day: the report is refreshed, no second tab

    def test_preview_and_other_commands_have_no_watchdog(self, env, io, paths, config_file, monkeypatch):
        set_config_value(paths.config, "retry", "run_timeout_seconds", 0.1)
        monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: (time.sleep(0.4), outcome(0, "ok"))[1])
        env.build_deps = lambda p, c, s: make_deps(p, c, open_report=lambda path: True)
        fired: list[int] = []
        env.hard_exit = fired.append
        env.watchdog_grace = 0.0
        assert main(["preview"], env=env) == 0
        assert fired == []


# ====================================================================================== H8 check details


SECRET_DETAIL = "HTTP 403 Zugriff verweigert; token=geheim123abc sesskey=SESS99xyz Set-Cookie: MoodleSession=cookiewert"


class TestCheckDetails:
    def test_failed_categories_show_code_hint_and_detail_redacted(self, env, io, paths, config_file, store):
        with_usernames(paths)
        register_secret(PW_EDU)
        broken = ProbeReport(
            Source.WEBUNTIS,
            [ProbeStep("Erreichbarkeit", True, "WebUntis antwortet."), ProbeStep("Anmeldung", True, "Angemeldet.")],
            {
                Category.HOMEWORK: CategoryOutcome(Category.HOMEWORK, CategoryStatus.OK, item_count=2),
                Category.EXAMS: CategoryOutcome(
                    Category.EXAMS, CategoryStatus.FAILED,
                    ErrorInfo("schema", "Antwort hat sich geändert.", hint="'schulsync check' später wiederholen.", detail=f"{SECRET_DETAIL} pw={PW_EDU}"),
                ),
                Category.CANCELLATIONS: CategoryOutcome(Category.CANCELLATIONS, CategoryStatus.INCOMPLETE, detail="Seite 2 fehlt"),
            },
        )
        check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)), Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=broken)})
        assert main(["check"], env=env) == 1
        out = io.out
        assert "Antwort hat sich geändert." in out and "Fehlercode: schema" in out
        assert "'schulsync check' später wiederholen." in out  # the hint
        assert "Technische Details: HTTP 403 Zugriff verweigert; token=***" in out  # the detail line, in full ...
        for secret in ("geheim123abc", "SESS99xyz", "cookiewert", PW_EDU):  # ... but redacted
            assert secret not in out
        assert "***" in out
        assert "Seite 2 fehlt" in out

    def test_failed_steps_show_their_full_detail_redacted(self, env, io, paths, config_file, store):
        with_usernames(paths)
        bad = ProbeReport(
            Source.WEBUNTIS,
            [ProbeStep("Erreichbarkeit", True, "ok"), ProbeStep("Anmeldung", False, f"Anmeldung abgelehnt. {SECRET_DETAIL}")],
        )
        check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)), Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=bad)})
        assert main(["check"], env=env) == 1
        assert "[FEHLER] Anmeldung: Anmeldung abgelehnt. HTTP 403" in io.out
        assert "geheim123abc" not in io.out and "cookiewert" not in io.out

    def test_a_schulsync_error_of_a_probe_shows_code_and_technical_details(self, env, io, paths, config_file, store):
        with_usernames(paths)
        deps = check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)), Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS))})
        deps.gateway_factory = lambda: (_ for _ in ()).throw(
            TransientError("Google antwortet nicht.", hint="Später erneut versuchen.", detail=f"ReadTimeout: {SECRET_DETAIL}")
        )
        assert main(["check"], env=env) == 1
        out = io.out
        assert "Google antwortet nicht." in out and "Fehlercode: transient" in out and "Technische Details: ReadTimeout: HTTP 403 Zugriff verweigert" in out
        assert "geheim123abc" not in out and "SESS99xyz" not in out

    def test_an_unexpected_exception_is_shown_with_type_and_redacted_text(self, env, io, paths, config_file, store):
        with_usernames(paths)
        register_secret(PW_EDU)
        boom = FakeAdapter(Source.EDUVIDUAL)
        boom.check = lambda: (_ for _ in ()).throw(RuntimeError(f"kaputt {PW_EDU} token=abc123def"))
        check_env(env, paths, store, adapters={Source.EDUVIDUAL: boom, Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS))})
        assert main(["check"], env=env) == 1
        out = io.out
        assert "Unerwarteter Fehler (RuntimeError)" in out and "Technische Details: RuntimeError: kaputt" in out
        assert PW_EDU not in out and "abc123def" not in out

    def test_configuration_errors_show_their_detail(self, env, io, paths):
        paths.config.write_text('[retry]\ndelays_seconds = ["a"]\n', encoding="utf-8")  # ValueError inside: ConfigError with detail
        assert main(["check"], env=env) == 2
        assert "[FEHLER] Konfiguration" in io.out and "Fehlercode: config" in io.out and "Technische Details" in io.out

    def test_a_category_error_without_detail_prints_no_empty_detail_line(self, env, io, paths, config_file, store):
        with_usernames(paths)
        broken = ProbeReport(Source.WEBUNTIS, [ProbeStep("Anmeldung", True, "ok")], {Category.EXAMS: CategoryOutcome(Category.EXAMS, CategoryStatus.FAILED, ErrorInfo("schema", "Antwort hat sich geändert."))})
        check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)), Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=broken)})
        main(["check"], env=env)
        assert "Technische Details" not in io.out and "Fehlercode: schema" in io.out


class TestConsoleIsRedacted:
    def test_every_line_of_check_and_status_goes_through_redact(self, env, paths, config_file, store, now):
        register_secret("hunter2-dummy-console")
        seen: list[str] = []
        env.say = seen.append
        with_usernames(paths)
        db = StateStore(paths.db)
        item = samples.assignment(title="Passwort ist hunter2-dummy-console und token=abcdef123456")
        from schulsync.models import EventState, ItemState

        db.upsert(ItemState(item=item, item_hash="h", cancelled=False, first_seen_at=now, last_seen_at=now, event_id="e", event_state=EventState.USER_DELETED, dirty=False), now)
        db.close()
        check_env(env, paths, store, adapters={Source.EDUVIDUAL: FakeAdapter(Source.EDUVIDUAL, check_report=probe_ok(Source.EDUVIDUAL)), Source.WEBUNTIS: FakeAdapter(Source.WEBUNTIS, check_report=probe_ok(Source.WEBUNTIS))})
        assert main(["check"], env=env) == 0
        assert main(["status"], env=env) == 0
        text = "\n".join(seen)
        assert "hunter2-dummy-console" not in text and "abcdef123456" not in text
        assert "Passwort ist *** und token=***" in text  # the deleted entry of `status`, masked


# ====================================================================================== H6: unreadable database


def corrupt_db(paths) -> None:
    paths.db.write_bytes(b"das ist keine datenbank" * 50)


class TestUnreadableDatabase:
    def test_run_auto_with_an_unreadable_database_opens_one_tab_per_day(self, env, io, paths, config_file, monkeypatch, opened, clock):
        corrupt_db(paths)
        monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("the runner must not run on an unreadable database"))
        env.build_deps = lambda p, c, s: make_deps(p, c)
        assert main(["run", "--auto"], env=env) == 2
        report = paths.reports / "bericht-2026-10-14.html"
        assert opened == [report] and report.is_file()
        assert "Zustandsdatenbank" in report.read_text(encoding="utf-8")
        assert main(["run", "--auto"], env=env) == 2
        assert len(opened) == 1  # the same problem again: report refreshed, no second tab
        clock.now = clock.now + timedelta(days=1)
        assert main(["run", "--auto"], env=env) == 2
        assert len(opened) == 2  # next day: shown again

    def test_run_manual_with_an_unreadable_database_is_a_german_config_error(self, env, io, paths, config_file, monkeypatch):
        corrupt_db(paths)
        monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("the runner must not run"))
        env.build_deps = lambda p, c, s: make_deps(p, c)
        assert main(["run"], env=env) == 2
        assert "Zustandsdatenbank" in io.out and "Hinweis:" in io.out

    def test_a_busy_database_is_not_taken_for_a_broken_one(self, env, io, paths, config_file, monkeypatch):
        paths.db.write_bytes(b"")  # exists, so the probe runs
        monkeypatch.setattr(cli, "StateStore", lambda path: (_ for _ in ()).throw(TransientError("Datenbank beschäftigt.")))
        seen = []
        monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: seen.append(mode) or outcome(0, silent=True))
        env.build_deps = lambda p, c, s: make_deps(p, c)
        assert main(["run", "--auto"], env=env) == 0 and len(seen) == 1

    def test_no_probe_without_a_database_file_and_with_an_injected_state(self, env, io, paths, config_file, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "StateStore", lambda path: calls.append(path) or pytest.fail("no probe expected"))
        monkeypatch.setattr(runner, "run", lambda deps, mode, *, force=False: outcome(0, silent=True))
        env.build_deps = lambda p, c, s: make_deps(p, c)
        assert main(["run", "--auto"], env=env) == 0 and calls == []
