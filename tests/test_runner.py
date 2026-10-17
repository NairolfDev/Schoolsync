"""Runner tests: lock, day gate, network wait, retries/backoff, auth block, timeouts, report + browser rules,
preview, crash handling, restore and exit codes (DESIGN 7 and the runner part of DESIGN 11).

Everything is injected: clock, sleep, monotonic clock, browser, network check (see tests/support/engine_harness.Env).
Google is FakeGoogle, the schools are FakeAdapters. No test waits, opens a browser or touches the network."""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import fields, replace
from datetime import date

import pytest

from schulsync import runner
from schulsync.config import Config, Paths, ReportConfig, RetryConfig
from schulsync.errors import (
    AuthError,
    ConfigError,
    ErrorInfo,
    GoogleAuthError,
    NetworkError,
    OutcomeUnknownError,
    SchemaError,
    SchulSyncError,
    TransientError,
)
from schulsync.google_types import make_event_id
from schulsync.lock import ProcessLock
from schulsync.models import (
    Category,
    CategoryStatus,
    ChangeStatus,
    EventState,
    ItemStatus,
    ItemTime,
    Notice,
    OverallStatus,
    RunMode,
    Severity,
    SourceStatus,
)
from schulsync.state import StateStore
from schulsync.sync import SyncEngine
from schulsync.timeutil import now_vienna, today_vienna
from tests.support import samples as S
from tests.support.engine_harness import EDU, NOW, WU, Env

DAY = date(2026, 10, 14)
AUTO, MANUAL, PREVIEW = RunMode.AUTO, RunMode.MANUAL, RunMode.PREVIEW


def cfg(**sections) -> Config:
    """Default Config with whole sections replaced, e.g. cfg(report=ReportConfig(open_on="never"))."""
    return Config(**sections)


def retry_cfg(**kw) -> Config:
    return Config(retry=replace(RetryConfig(), **kw))


def eid(item, gen: int = 0) -> str:
    return make_event_id(item.uid, gen)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.state.close()


@pytest.fixture
def make_env(tmp_path):
    made: list[Env] = []

    def factory(**kw) -> Env:
        e = Env(tmp_path / f"e{len(made)}", **kw)
        made.append(e)
        return e

    yield factory
    for e in made:
        e.state.close()


@pytest.fixture
def logs(caplog):
    """Capture the 'schulsync' loggers even if another test switched propagation off."""
    lg = logging.getLogger("schulsync")
    lg.addHandler(caplog.handler)
    lg.setLevel(logging.DEBUG)
    yield caplog
    lg.removeHandler(caplog.handler)


def sources(out, src):
    return out.report.sources[src]


# ====================================================================================== contract


class TestContract:
    def test_deps_defaults_and_field_order(self, tmp_path):
        names = [f.name for f in fields(runner.Deps)]
        assert names == [
            "paths", "config", "adapter_factories", "gateway_factory", "clock", "sleep", "monotonic",
            "open_report", "network_ready", "state", "notify",
        ]  # fmt: skip
        d = runner.Deps(Paths(tmp_path), Config(), {}, lambda: None)
        assert d.clock is now_vienna and d.sleep is time.sleep and d.monotonic is time.monotonic
        assert d.network_ready is runner.default_network_ready and d.state is None
        assert d.open_report.__name__ == "open_report"

    def test_run_outcome_fields(self):
        assert [f.name for f in fields(runner.RunOutcome)] == ["exit_code", "report", "report_path", "console_text", "silent"]

    def test_default_network_ready_needs_one_reachable_host(self, monkeypatch):
        seen = []

        def connect(addr, timeout=None):
            seen.append(addr)
            if addr[0] == "b.example":
                import contextlib

                return contextlib.nullcontext()
            raise OSError("unreachable")

        monkeypatch.setattr(runner.socket, "create_connection", connect)
        assert runner.default_network_ready(["a.example", "b.example", "c.example"]) is True
        assert seen == [("a.example", 443), ("b.example", 443)]
        monkeypatch.setattr(runner.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        assert runner.default_network_ready(["a.example"]) is False
        assert runner.default_network_ready() is False  # default hosts: school platforms and Google

    def test_network_hosts_follow_the_configuration(self):
        assert runner.network_hosts(Config()) == ("www.googleapis.com", "www.eduvidual.at", "htbla-grieskirchen.webuntis.com")
        c = Config(webuntis=replace(Config().webuntis, enabled=False))
        assert runner.network_hosts(c) == ("www.googleapis.com", "www.eduvidual.at")


# ====================================================================================== happy path and the day gate


class TestDayGate:
    def test_successful_run_writes_everything_and_reports_without_opening_a_browser(self, env):
        a, x = S.assignment(), S.exam()
        env.set_items(a, x)
        out = env.run()
        assert out.exit_code == 0 and not out.silent and out.report.status == OverallStatus.SUCCESS
        assert env.opened == []  # success never opens a browser
        assert out.report_path.name == "bericht-2026-10-14.html" and out.report_path.is_file()
        assert out.report.report_path == str(out.report_path) and "Erfolgreich" in out.report_path.read_text(encoding="utf-8")
        assert set(env.google.events) == {eid(a), eid(x)} and len(env.google.open_tasks) == 2
        assert env.google.calls["check_targets"] == 1 and env.google.calls["list_tasks"] == 1  # once per run
        for src in (EDU, WU):
            assert sources(out, src).status == SourceStatus.SUCCESS and not sources(out, src).stale
            assert env.state.day_done(DAY, src) and sources(out, src).last_success_at == NOW
        assert sources(out, EDU).calendar.new == 1 and sources(out, EDU).tasks.new == 1
        assert [c.status for c in out.report.changes] == [ChangeStatus.APPLIED] * 4 and out.report.pending_writes == 0
        run = env.state.last_run()
        assert run.status == OverallStatus.SUCCESS and run.mode == AUTO and run.report_path == str(out.report_path)
        assert "Erfolgreich" in out.console_text and str(out.report_path) in out.console_text
        assert env.sleeps == [] and env.network_checks == 1

    def test_second_start_on_the_same_day_ends_silently(self, env):
        env.set_items(S.assignment(), S.exam())
        env.run()
        env.google.reset_counters()
        env.google.reset_write_log()
        checks, calls, last = env.network_checks, (env.fetch_calls(EDU), env.fetch_calls(WU)), env.state.last_run().id
        out = env.run()
        assert out.silent and out.exit_code == 0 and out.report is None and out.report_path is None and out.console_text == ""
        assert env.google.calls == {} and env.google.write_log == [] and env.opened == []
        assert (env.fetch_calls(EDU), env.fetch_calls(WU)) == calls and env.network_checks == checks
        assert env.state.last_run().id == last  # no run record either

    def test_next_day_runs_again_and_a_long_break_means_exactly_one_check(self, env):
        env.set_items(S.assignment(), S.exam())
        env.run()
        env.advance(days=5)  # PC was off for days: one check of the current data, no catch-up runs
        out = env.run()
        assert out.exit_code == 0 and not out.silent
        assert env.fetch_calls(EDU) == 2 and env.fetch_calls(WU) == 2 and len(env.google.events) == 2
        assert env.state.last_run().day == today_vienna(env.now)
        assert env.run().silent  # and again silent the same day

    def test_day_is_the_vienna_calendar_day_across_dst_and_utc_midnight(self, env):
        env.set_items(S.assignment(due=S.at(2026, 11, 2, 23, 59)))
        env.now = S.at(2026, 10, 25, 0, 30)  # 22:30 UTC on the 24th, summer time
        assert not env.run().silent
        env.now = S.at(2026, 10, 25, 5, 50)  # 04:50 UTC on the 25th: other UTC day, same Vienna day, same time slot
        assert env.run().silent
        env.now = S.at(2026, 10, 26, 0, 30)
        assert not env.run().silent

    def test_manual_run_and_force_bypass_the_gate_without_duplicates(self, env):
        env.set_items(S.assignment(), S.exam())
        env.run()
        n = len(env.google.write_log)
        for out in (env.run(MANUAL), env.run(AUTO, force=True)):
            assert not out.silent and out.exit_code == 0
        assert env.fetch_calls(EDU) == 3 and len(env.google.write_log) == n  # nothing new, nothing duplicated
        assert len(env.google.events) == 2 and len(env.google.tasks) == 2
        assert sources(out, EDU).calendar.unchanged == 1 and sources(out, WU).tasks.unchanged == 1
        assert env.state.last_run().mode == AUTO and out.report.mode == AUTO

    def test_no_enabled_source(self, env):
        c = Config(
            eduvidual=replace(Config().eduvidual, enabled=False), webuntis=replace(Config().webuntis, enabled=False)
        )
        out_auto = runner.run(env.deps(config=c), AUTO)
        out_manual = runner.run(env.deps(config=c), MANUAL)
        assert out_auto.silent and out_auto.exit_code == 0
        assert not out_manual.silent and out_manual.exit_code == 0 and "keine quelle" in out_manual.console_text.lower()

    def test_disabled_source_is_listed_as_disabled_and_not_fetched(self, make_env):
        c = Config(webuntis=replace(Config().webuntis, enabled=False))
        e = make_env(cfg=c)
        e.set_items(S.assignment(), S.exam())
        out = e.run()
        assert out.report.status == OverallStatus.SUCCESS and e.fetch_calls(WU) == 0
        assert sources(out, WU).status == SourceStatus.DISABLED and sources(out, EDU).status == SourceStatus.SUCCESS
        assert len(e.google.events) == 1


# ====================================================================================== retries, auth, factories


class TestFetchAndRetries:
    def test_partial_failure_retries_with_backoff_then_only_the_failed_source(self, env):
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].raise_next(TransientError("Zeitüberschreitung bei WebUntis."), times=4)
        out = env.run()
        assert env.sleeps == [15.0, 60.0, 180.0] and env.fetch_calls(WU) == 4  # bounded, growing waits
        assert out.exit_code == 1 and out.report.status == OverallStatus.PARTIAL
        assert sources(out, EDU).status == SourceStatus.SUCCESS and sources(out, WU).status == SourceStatus.FAILED
        assert sources(out, WU).errors[0].code == "transient" and sources(out, WU).stale and not sources(out, EDU).stale
        assert env.opened == [out.report_path]  # one tab, after all retries
        assert env.state.day_done(DAY, EDU) and not env.state.day_done(DAY, WU) and not env.state.is_auth_blocked(WU)
        assert env.state.get_source_status(WU).last_error_code == "transient"
        assert len(env.google.events) == 1  # what worked is already in Google
        env.sleeps.clear()
        out2 = env.run()  # next start the same day: only the failed part is tried again
        assert env.fetch_calls(EDU) == 1 and env.fetch_calls(WU) == 5 and env.sleeps == []
        assert out2.report.status == OverallStatus.SUCCESS and sources(out2, EDU).status == SourceStatus.ALREADY_DONE
        assert len(env.google.events) == 2 and len(env.opened) == 1 and env.state.day_done(DAY, WU)

    def test_transient_failures_that_heal_within_the_attempts_are_a_success(self, env):
        env.set_items(S.assignment())
        env.adapters[EDU].raise_next(NetworkError("Kein Netz."), times=2)
        out = env.run()
        assert env.sleeps == [15.0, 60.0] and env.fetch_calls(EDU) == 3
        assert out.report.status == OverallStatus.SUCCESS and env.opened == []

    def test_only_transient_errors_are_retried(self, env):
        env.adapters[EDU].raise_next(SchemaError("Seite hat sich geändert."))
        out = env.run()
        assert env.sleeps == [] and env.fetch_calls(EDU) == 1
        assert sources(out, EDU).errors[0].code == "schema" and not env.state.is_auth_blocked(EDU)
        assert out.report.status == OverallStatus.PARTIAL

    def test_auth_error_one_attempt_blocks_the_source_and_not_the_other(self, env):
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].raise_next(AuthError("Benutzername oder Passwort abgelehnt."))
        out = env.run()
        assert env.fetch_calls(WU) == 1 and env.sleeps == []
        assert env.state.is_auth_blocked(WU) and not env.state.is_auth_blocked(EDU)
        assert env.state.get_source_status(WU).auth_block_code == "auth"
        assert sources(out, EDU).status == SourceStatus.SUCCESS and sources(out, WU).status == SourceStatus.FAILED
        assert out.report.status == OverallStatus.PARTIAL and len(env.google.events) == 1
        # the next AUTO starts skip the blocked source, with an explicit entry; no tab flood
        out2 = env.run()
        assert env.fetch_calls(WU) == 1 and sources(out2, EDU).status == SourceStatus.ALREADY_DONE
        err = sources(out2, WU).errors[0]
        assert err.code == "auth" and "überspringen" in err.message and "schulsync setup --webuntis" in err.hint
        assert not out2.silent and out2.report is not None and len(env.opened) == 1 and out2.exit_code == 1
        assert any("schulsync setup --webuntis" in s for s in out2.report.next_steps)

    def test_manual_run_retries_a_blocked_source_and_a_success_unblocks_it(self, env):
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].raise_next(AuthError("abgelehnt"))
        env.run()
        out = env.run(MANUAL)
        assert env.fetch_calls(WU) == 2 and not env.state.is_auth_blocked(WU)
        assert sources(out, WU).status == SourceStatus.SUCCESS and out.report.status == OverallStatus.SUCCESS
        assert env.state.day_done(DAY, WU) and env.state.get_source_status(WU).last_error_code is None

    def test_a_still_rejected_manual_run_keeps_the_block(self, env):
        env.adapters[WU].raise_next(AuthError("abgelehnt"), times=2)
        env.run()
        out = env.run(MANUAL)
        assert env.fetch_calls(WU) == 2 and env.state.is_auth_blocked(WU) and out.exit_code == 1

    def test_rejected_login_in_the_fetch_result_blocks_without_retry(self, env):
        env.adapters[WU].login_fails(ErrorInfo("auth", "Anmeldung abgelehnt."))
        out = env.run()
        assert env.fetch_calls(WU) == 1 and env.sleeps == [] and env.state.is_auth_blocked(WU)
        assert sources(out, WU).errors[0].code == "auth" and all(oc.status == CategoryStatus.FAILED for oc in sources(out, WU).categories.values())
        assert out.report.status == OverallStatus.PARTIAL and not env.state.day_done(DAY, WU)

    @pytest.mark.parametrize("how", ["login_error", "exception"])
    def test_a_rejected_login_is_one_problem_so_a_later_blocked_start_opens_no_second_tab(self, env, how):
        """The first start sees the rejection, the later starts of the day skip the blocked source: all three describe
        the same problem and must share one fingerprint (ANFORDERUNGEN 8: no tab flood for identical errors)."""
        env.set_items(S.assignment(), S.exam())
        if how == "login_error":
            env.adapters[WU].login_fails(ErrorInfo("auth", "Anmeldung abgelehnt."))
        else:
            env.adapters[WU].raise_next(AuthError("Anmeldung abgelehnt."))
        first = env.run()
        env.advance(hours=2)
        second = env.run()  # a later logon the same day: WebUntis is skipped (blocked)
        assert env.fetch_calls(WU) == 1 and second.report.fingerprint() == first.report.fingerprint()
        assert env.opened == [first.report_path] and env.state.report_opened_fingerprint(DAY) == first.report.fingerprint()

    def test_login_error_that_is_a_schema_problem_does_not_block(self, env):
        env.adapters[WU].login_fails(ErrorInfo("schema", "Anmeldeseite unerwartet."))
        env.run()
        assert not env.state.is_auth_blocked(WU)

    def test_transient_login_error_is_retried(self, env):
        env.adapters[WU].login_fails(ErrorInfo("network", "Kein Netz."))
        out = env.run()
        assert env.fetch_calls(WU) == 4 and env.sleeps == [15.0, 60.0, 180.0] and not env.state.is_auth_blocked(WU)
        assert out.report.status == OverallStatus.PARTIAL

    def test_missing_password_fails_the_source_with_a_setup_hint_without_blocking(self, env):
        env.set_items(S.assignment(), S.exam())
        env.factory_errors[WU] = ConfigError("Das Passwort für WebUntis fehlt.")
        out = env.run()
        err = sources(out, WU).errors[0]
        assert err.code == "config" and "schulsync setup" in err.hint and env.fetch_calls(WU) == 0
        assert not env.state.is_auth_blocked(WU) and sources(out, EDU).status == SourceStatus.SUCCESS
        assert out.report.status == OverallStatus.PARTIAL
        del env.factory_errors[WU]  # the user ran the setup: the next start works without any unblocking
        assert env.run().report.status == OverallStatus.SUCCESS

    def test_source_without_a_factory_fails_clearly(self, env):
        out = env.run(adapter_factories={EDU: env.deps().adapter_factories[EDU]})
        assert sources(out, WU).status == SourceStatus.FAILED and sources(out, WU).errors[0].code == "config"

    def test_incomplete_fetch_never_cancels_and_is_not_a_daily_success(self, env):
        x = S.exam()
        env.set_items(x, S.homework(due=date(2026, 10, 20)))
        env.run()
        n = len(env.google.write_log)
        env.adapters[WU].fail_category(Category.EXAMS, ErrorInfo("incomplete", "Seite 2 fehlt."), CategoryStatus.INCOMPLETE)
        out = env.run(MANUAL)
        assert len(env.google.write_log) == n and eid(x) in env.google.events  # nothing cancelled or removed
        assert not env.google.events[eid(x)]["summary"].startswith("Abgesagt")
        assert sources(out, WU).status == SourceStatus.PARTIAL and out.report.status == OverallStatus.PARTIAL
        assert not env.state.day_done(DAY, WU) and env.state.get_source_day(DAY, WU).fetch_complete is False
        assert env.opened == [out.report_path]
        env.adapters[WU].heal_category(Category.EXAMS)
        out2 = env.run()  # AUTO: the incomplete part is fetched again, the finished source is not
        assert (env.fetch_calls(EDU), env.fetch_calls(WU)) == (2, 3)
        assert out2.report.status == OverallStatus.SUCCESS and env.state.day_done(DAY, WU)

    def test_last_success_and_stale_after_a_later_failure(self, env):
        env.set_items(S.assignment(), S.exam())
        env.run()
        env.advance(days=1)
        env.adapters[WU].raise_next(TransientError("Zeitüberschreitung."), times=4)
        out = env.run()
        res = sources(out, WU)
        assert res.stale and res.last_success_at == NOW and not sources(out, EDU).stale

    def test_never_succeeded_source_has_no_last_success(self, env):
        env.adapters[WU].raise_next(TransientError("x"), times=4)
        assert sources(env.run(), WU).last_success_at is None


# ====================================================================================== Google side


class TestGoogleSide:
    def test_missing_calendar_defers_the_calendar_writes_and_never_falls_back(self, make_env):
        """Before the audit this pinned "all writes deferred" (ACCEPTANCE_NOTES A4); ANFORDERUNGEN 4 says only the AFFECTED
        writes wait: the tasks are written, the calendar entries are deferred, and nothing ever goes to another calendar."""
        e = make_env(calendar_exists=False)
        e.set_items(S.assignment(), S.exam())
        out = e.run()
        assert {w[0] for w in e.google.write_log} == {"insert_task"} and e.google.misrouted_writes() == [] and e.google.primary_events == {}
        assert out.exit_code == 1 and out.report.status != OverallStatus.SUCCESS and e.opened == [out.report_path]
        assert out.report.calendar_target.ok is False and out.report.calendar_target.error.code == "target_unavailable"
        assert out.report.tasks_target.ok is True
        assert len(out.report.changes) == 4
        deferred = [c for c in out.report.changes if c.status == ChangeStatus.DEFERRED]
        assert len(deferred) == 2 and {c.target.value for c in deferred} == {"calendar"}
        assert {c.status for c in out.report.changes if c.target.value == "tasks"} == {ChangeStatus.APPLIED}
        assert out.report.pending_writes == 2 and any("nachgeholt" in s for s in out.report.next_steps)
        assert e.sleeps == []  # a missing target is not transient
        assert "Kalender" in out.console_text and "Nicht erledigt: 2" in out.console_text

    def test_missing_task_list_defers_the_task_writes_and_the_calendar_is_written(self, make_env):
        e = make_env(tasklist_exists=False)
        e.set_items(S.assignment(), S.exam())
        out = e.run()
        assert {w[0] for w in e.google.write_log} == {"insert_event"} and len(e.google.events) == 2
        assert out.report.tasks_target.ok is False and out.report.calendar_target.ok is True
        deferred = [c for c in out.report.changes if c.status == ChangeStatus.DEFERRED]
        assert len(deferred) == 2 and {c.target.value for c in deferred} == {"tasks"}
        assert out.report.pending_writes == 2 and out.exit_code == 1

    def test_primary_calendar_is_never_written(self, make_env):
        e = make_env(calendar_is_primary=True)
        e.set_items(S.assignment())
        out = e.run()
        assert e.google.misrouted_writes() == [] and {w[0] for w in e.google.write_log} <= {"insert_task"} and out.exit_code == 1
        assert e.google.primary_events == {}

    def test_writes_are_retried_next_start_without_fetching_again(self, make_env):
        e = make_env(calendar_exists=False)
        e.set_items(S.assignment(), S.exam())
        e.run()
        sd = e.state.get_source_day(DAY, EDU)
        assert sd.fetch_complete and sd.writes_done_at is None and not e.state.day_done(DAY, EDU)
        assert e.state.pending_count() == 2
        e.google.calendar_exists = True
        e.opened.clear()
        out = e.run()  # only the missing writes: no refetch
        assert (e.fetch_calls(EDU), e.fetch_calls(WU)) == (1, 1)
        assert out.report.status == OverallStatus.SUCCESS and e.state.day_done(DAY, EDU) and e.state.day_done(DAY, WU)
        assert sources(out, EDU).status == SourceStatus.SUCCESS and sources(out, EDU).calendar.new == 1
        assert len(e.google.events) == 2 and len(e.google.open_tasks) == 2 and e.opened == []
        assert e.run().silent

    def test_transient_google_failure_is_retried_with_backoff_then_deferred(self, env):
        env.set_items(S.assignment(), S.exam())
        env.google.fail_always("check_targets", NetworkError)
        out = env.run()
        assert env.sleeps == [15.0, 60.0, 180.0]  # the same bounded retries as for the sources
        assert env.google.calls["check_targets"] == 4 and env.google.write_log == []
        assert out.report.status == OverallStatus.FAILED and out.report.calendar_target.error.code == "network"
        assert env.state.get_source_day(DAY, EDU).fetch_complete
        env.google.clear_faults()
        assert env.run().report.status == OverallStatus.SUCCESS and env.fetch_calls(EDU) == 1

    def test_google_auth_failure_defers_with_an_actionable_report(self, env):
        env.set_items(S.assignment())
        env.gateway_error = GoogleAuthError("Google-Anmeldung abgelaufen.")
        out = env.run()
        assert env.sleeps == [] and out.report.calendar_target.error.code == "google_auth" and out.exit_code == 1
        assert not env.state.is_auth_blocked(EDU)  # Google trouble never blocks a school source
        assert "google_auth" in {c.error.code for c in out.report.changes}
        assert "setup" in out.report_path.read_text(encoding="utf-8")

    def test_partial_failure_per_target(self, env):
        a = S.assignment()
        env.set_items(a)
        env.google.fail_always("insert_task", NetworkError)
        out = env.run()
        assert out.report.status == OverallStatus.PARTIAL
        assert len(env.google.events) == 1 and env.google.tasks == [] and sources(out, EDU).tasks.failed == 1
        assert sources(out, EDU).status == SourceStatus.PARTIAL and sources(out, EDU).calendar.new == 1
        env.google.clear_faults()
        out2 = env.run()
        assert out2.report.status == OverallStatus.SUCCESS and len(env.google.open_tasks) == 1
        assert len(env.google.writes("insert_event")) == 1 and env.fetch_calls(EDU) == 1

    def test_lost_response_then_next_start_creates_no_duplicates(self, env):
        env.set_items(S.assignment())
        env.google.fail_next("insert_event", OutcomeUnknownError, applied=True)
        out = env.run()
        assert [c.status for c in out.report.changes if c.target.value == "calendar"] == [ChangeStatus.UNCERTAIN]
        assert out.report.status != OverallStatus.SUCCESS
        env.run()
        assert len(env.google.events) == 1 and len(env.google.writes("insert_event")) == 1 and len(env.google.open_tasks) == 1

    @pytest.mark.parametrize("what", ["event", "task"])
    def test_process_death_after_a_google_write_creates_no_duplicates(self, env, monkeypatch, what):
        env.set_items(S.assignment())
        target = "calendar" if what == "event" else "tasks"
        real, armed = StateStore.resolve_intent, [True]

        def die(self, intent_id, note=None):
            if armed[0] and self.get_intent(intent_id).target.value == target:
                armed[0] = False
                raise RuntimeError("Prozess abgebrochen (Test).")
            return real(self, intent_id, note)

        monkeypatch.setattr(StateStore, "resolve_intent", die)
        out = env.run()  # the "process" dies between the Google write and its bookkeeping
        # the bug is contained to its item (FAILED change, code "unexpected"); it no longer aborts the whole run
        assert out.exit_code == 1 and any(c.error is not None and c.error.code == "unexpected" for c in out.report.changes)
        assert not env.state.day_done(DAY, EDU)
        monkeypatch.setattr(StateStore, "resolve_intent", real)
        out2 = env.run()
        assert out2.report.status == OverallStatus.SUCCESS
        assert len(env.google.events) == 1 and len(env.google.tasks) == 1
        assert len(env.google.writes("insert_event")) == 1 and len(env.google.writes("insert_task")) == 1

    def test_user_deleted_entries_stay_deleted_until_an_explicit_restore(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        env.google.user_delete_event(eid(a))
        env.google.user_delete_task(env.google.tasks[0]["id"])
        env.set_items(replace(a, when=ItemTime(S.at(2026, 10, 28, 23, 59))))
        out = env.run(MANUAL)
        assert {n.code for n in out.report.notices} >= {"user_deleted_event", "user_deleted_task"}
        assert out.report.status == OverallStatus.SUCCESS and sources(out, EDU).calendar.skipped_reasons == {"user_deleted": 1}
        n = len(env.google.write_log)
        for _ in range(3):  # not silently re-created on every run
            env.run(MANUAL)
        assert len(env.google.write_log) == n and env.google.events == {} and env.google.open_tasks == []
        out = env.restore()
        assert out.exit_code == 0 and out.report.mode == MANUAL and "Freigegeben: 1" in out.console_text
        assert list(env.google.events) == [eid(a, 1)] and len(env.google.open_tasks) == 1
        assert env.google.events[eid(a, 1)]["start"]["dateTime"].startswith("2026-10-28T23:59")
        assert [c.action.value for c in out.report.changes] == ["restore", "restore"]
        assert env.state.pending_count() == 0 and env.run().silent

    def test_restore_arguments(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        out = env.restore()
        assert out.exit_code == 0 and out.report is None and "keine bewusst gelöschten" in out.console_text
        out = env.restore(["eduvidual:assignment:assign:nope"])
        assert out.exit_code == 2 and out.report is None and "assign:nope" in out.console_text
        env.google.user_delete_event(eid(a))
        env.set_items(replace(a, title="Neu benannt"))
        env.run(MANUAL)
        assert env.restore([a.uid]).exit_code == 0 and eid(a, 1) in env.google.events

    def test_restore_respects_the_lock_and_google_problems(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        env.google.user_delete_event(eid(a))
        env.set_items(replace(a, title="Neu benannt"))
        env.run(MANUAL)
        env.google.calendar_exists = False
        out = env.restore()
        assert out.exit_code == 1 and out.report.status != OverallStatus.SUCCESS and env.google.write_log[-1][0] != "insert_event"
        lock = ProcessLock(env.paths.lock)
        assert lock.acquire()
        locked = env.restore()
        lock.release()
        assert locked.exit_code == 1 and "läuft bereits" in locked.console_text and not locked.silent


# ====================================================================================== network, time limits, lock


class TestWaitingAndLimits:
    def test_waits_for_the_network_with_growing_pauses_and_then_goes_on(self, env):
        env.set_items(S.assignment())
        env.network_answers = [False, False, True]
        out = env.run()
        assert env.sleeps == [2.0, 4.0] and env.network_checks == 3 and out.report.status == OverallStatus.SUCCESS

    def test_gives_up_waiting_after_the_configured_time_but_still_tries(self, env):
        env.set_items(S.assignment())
        env.network_answers = [False] * 1000
        out = env.run()
        assert sum(env.sleeps) == 120.0 and max(env.sleeps) <= 15.0 and out.report.status == OverallStatus.SUCCESS
        assert env.fetch_calls(EDU) == 1

    def test_the_network_wait_is_bounded_in_real_time_even_when_every_probe_runs_into_its_timeout(self, env):
        """`network_wait_seconds` is a limit on elapsed time, not on the sum of the pauses: a probe that takes 40 s
        (blackholed host, several timeouts) counts. Otherwise a logon without internet could wait minutes longer."""
        env.set_items(S.assignment())

        def slow_probe() -> bool:
            env.network_checks += 1
            env.mono += 40.0
            return False

        out = env.run(network_ready=slow_probe)
        assert env.mono <= 120.0 + 40.0  # the limit plus at most one last probe, not 10 probes x 40 s
        assert env.fetch_calls(EDU) == 1 and out.report.status == OverallStatus.SUCCESS  # and then it tries anyway

    def test_a_broken_network_probe_does_not_stop_the_run(self, env):
        env.set_items(S.assignment())

        def boom():
            raise OSError("Probe kaputt")

        out = env.run(network_ready=boom)
        assert out.report.status == OverallStatus.SUCCESS and env.sleeps == []

    def test_manual_runs_and_silent_runs_do_not_wait(self, env):
        env.set_items(S.assignment())
        env.network_answers = [False] * 5
        env.run(MANUAL)
        assert env.network_checks == 0 and env.sleeps == []
        env.run(AUTO, force=True)  # AUTO waits even when forced: five "no", then "yes"
        # one pause per "no", bounded by the configured wait. (The exact sum used to be pinned here, which only coupled the test
        # to the step constants of the backoff; test_waits_for_the_network... and the limit test pin the schedule itself.)
        assert env.network_checks == 6 and len(env.sleeps) == 5 and 0 < sum(env.sleeps) <= env.cfg.retry.network_wait_seconds
        env.network_checks = 0
        assert env.run().silent and env.network_checks == 0

    def test_run_timeout_stops_the_remaining_work_and_the_rest_follows_next_start(self, make_env):
        e = make_env(cfg=retry_cfg(run_timeout_seconds=100.0))
        e.set_items(S.assignment(), S.exam())
        orig = e.adapters[EDU].fetch

        def slow(window, tracked, now):
            res = orig(window, tracked, now)
            e.mono += 200.0  # the Eduvidual fetch "took" 200 seconds
            return res

        e.adapters[EDU].fetch = slow
        out = e.run()
        assert e.fetch_calls(WU) == 0 and sources(out, WU).errors[0].code == "run_timeout"
        assert e.google.write_log == [] and {c.error.code for c in out.report.changes} == {"run_timeout"}
        assert out.exit_code == 1 and any("Zeitlimit" in s for s in out.report.next_steps)
        e.adapters[EDU].fetch = orig
        out2 = e.run()  # the next start: Eduvidual only writes, WebUntis is fetched
        assert e.fetch_calls(EDU) == 1 and e.fetch_calls(WU) == 1 and out2.report.status == OverallStatus.SUCCESS
        assert len(e.google.events) == 2

    def test_run_timeout_cuts_the_retries_short(self, make_env):
        e = make_env(cfg=retry_cfg(run_timeout_seconds=50.0))
        e.adapters[EDU].raise_next(TransientError("x"), times=10)
        out = e.run()
        assert e.sleeps == [15.0] and e.fetch_calls(EDU) == 2  # a wait of 60 s would end beyond the limit: not started
        assert sources(out, EDU).status == SourceStatus.FAILED

    def test_lock_held_auto_exits_silently_and_manual_reports(self, env):
        env.set_items(S.assignment())
        lock = ProcessLock(env.paths.lock)
        assert lock.acquire()
        try:
            auto = env.run(AUTO)
            manual = env.run(MANUAL)
            preview = env.run(PREVIEW)
        finally:
            lock.release()
        assert auto.silent and auto.exit_code == 0 and auto.report is None and auto.console_text == ""
        assert not manual.silent and manual.exit_code == 1 and "läuft bereits" in manual.console_text and manual.report is None
        assert preview.exit_code == 1 and env.fetch_calls(EDU) == 0 and env.google.calls == {}
        assert env.run().report.status == OverallStatus.SUCCESS  # the lock is released again


# ====================================================================================== report and browser


class TestReportAndBrowser:
    def _failing(self, env, times=4):
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].raise_next(TransientError("Zeitüberschreitung bei WebUntis."), times=times)

    def test_the_same_failure_opens_one_tab_per_day_but_refreshes_the_file(self, env):
        self._failing(env, times=8)
        first = env.run()
        text1 = first.report_path.read_text(encoding="utf-8")
        env.advance(hours=1)
        second = env.run()
        text2 = second.report_path.read_text(encoding="utf-8")
        assert env.opened == [first.report_path] and second.report_path == first.report_path
        assert "08:00:00" in text1 and "09:00:00" in text2 and text1 != text2  # refreshed, no new tab
        assert env.state.report_opened_fingerprint(DAY) == first.report.fingerprint()

    def test_a_different_problem_opens_the_report_again(self, env):
        self._failing(env, times=4)
        env.run()
        env.adapters[WU].raise_next(AuthError("abgelehnt"))
        env.run()
        assert len(env.opened) == 2

    def test_a_new_day_opens_a_new_tab(self, env):
        self._failing(env, times=8)
        env.run()
        env.advance(days=1)
        env.run()
        assert len(env.opened) == 2 and env.opened[0] != env.opened[1]

    def test_open_on_never_writes_the_report_but_opens_nothing(self, make_env):
        e = make_env(cfg=cfg(report=ReportConfig(open_on="never")))
        self._failing(e)
        out = e.run()
        assert e.opened == [] and out.report_path.is_file() and out.exit_code == 1

    def test_open_on_warnings_opens_once_for_a_warning_only_run(self, make_env):
        e = make_env(cfg=cfg(report=ReportConfig(open_on="warnings")))
        e.set_items(S.assignment())
        e.adapters[EDU].set_notices([Notice(Severity.WARNING, "no_date", "Eintrag ohne Datum.", EDU, title="Ohne Datum")])
        out = e.run()
        assert out.report.status == OverallStatus.SUCCESS and e.opened == [out.report_path]
        e.run(MANUAL)
        assert len(e.opened) == 1  # same warning, same day
        e.adapters[EDU].set_notices([Notice(Severity.WARNING, "incomplete_data", "Titel abgeschnitten.", EDU, title="Anderer")])
        e.run(MANUAL)
        assert len(e.opened) == 2

    def test_default_never_opens_for_warnings(self, env):
        env.adapters[EDU].set_notices([Notice(Severity.WARNING, "no_date", "Eintrag ohne Datum.", EDU, title="X")])
        out = env.run()
        assert out.report.status == OverallStatus.SUCCESS and env.opened == []

    def test_failed_browser_start_is_logged_and_retried_next_time(self, env, logs):
        self._failing(env, times=12)
        env.open_result = False
        out = env.run()
        assert env.opened == [out.report_path] and out.report_path.is_file()
        assert str(out.report_path) in logs.text  # the location of the kept report is logged
        assert env.state.report_opened_fingerprint(DAY) is None  # not recorded: a later start may succeed
        env.open_result = True
        env.run()
        assert len(env.opened) == 2 and env.state.report_opened_fingerprint(DAY) is not None
        env.run()
        assert len(env.opened) == 2

    def test_a_crashing_opener_does_not_break_the_run(self, env):
        self._failing(env)

        def boom(path):
            raise OSError("kein Browser")

        out = env.run(open_report=boom)
        assert out.exit_code == 1 and out.report_path.is_file()

    def test_report_is_german_masks_secrets_and_lists_the_details(self, env):
        self._failing(env, times=0)
        env.adapters[WU].raise_next(SchulSyncError("Fehler.", detail="password=hunter2-dummy; sesskey=abc123def"))
        out = env.run()
        text = out.report_path.read_text(encoding="utf-8")
        assert "hunter2-dummy" not in text and "abc123def" not in text and "Teilweise fehlgeschlagen" in text
        assert "WebUntis" in text and "Eduvidual" in text and "Letzter erfolgreicher Abruf" in text
        assert "hunter2-dummy" not in out.console_text and "<script" not in text.lower()

    def test_pruning_of_reports_logs_and_old_rows(self, make_env):
        e = make_env(cfg=cfg(report=ReportConfig(keep_days=5, log_keep_days=5)))
        old_report = e.paths.reports / "bericht-2026-01-01.html"
        recent_report = e.paths.reports / "bericht-2026-10-12.html"
        old_log = e.paths.logs / "schulsync-2026-01-01.log"
        for f in (old_report, recent_report, old_log):
            f.write_text("x", encoding="utf-8")
        e.state.record_fetch(date(2026, 9, 1), EDU, S.at(2026, 9, 1, 8, 0), True)
        e.state.record_fetch(date(2026, 10, 12), EDU, S.at(2026, 10, 12, 8, 0), True)
        e.set_items(S.assignment())
        e.run()
        assert not old_report.exists() and not old_log.exists() and recent_report.exists()
        assert e.state.get_source_day(date(2026, 9, 1), EDU) is None and e.state.get_source_day(date(2026, 10, 12), EDU) is not None
        assert (e.paths.reports / "latest.html").exists()

    def test_console_io_is_never_needed(self, env, monkeypatch):
        env.set_items(S.assignment())
        monkeypatch.setattr(sys, "stdout", None)  # pythonw.exe
        monkeypatch.setattr(sys, "stderr", None)
        out = env.run()
        assert out.exit_code == 0 and out.report_path.is_file()


# ====================================================================================== preview


class TestPreview:
    def test_preview_writes_nothing_books_nothing_and_does_not_suppress_later_writes(self, env):
        a, x = S.assignment(), S.exam()
        env.set_items(a, x)
        out = env.run(PREVIEW)
        assert env.google.write_log == [] and env.google.events == {} and env.google.tasks == []
        assert out.report.mode == PREVIEW and out.exit_code == 0 and not out.silent and env.opened == []
        assert out.report_path.name == "vorschau.html" and not (env.paths.reports / "bericht-2026-10-14.html").exists()
        assert all(c.status == ChangeStatus.PLANNED for c in out.report.changes) and len(out.report.changes) == 4
        assert "Vorschau" in out.console_text and "Geplante Änderungen: 4" in out.console_text
        assert "Laborprotokoll 2" in out.console_text
        # the real database is untouched, including the day tables
        st = env.state
        assert st.all_items() == [] and st.pending_count() == 0 and st.open_intents() == []
        assert st.get_source_day(DAY, EDU) is None and st.get_source_day(DAY, WU) is None
        assert st.last_complete_fetch(EDU) is None and st.last_run() is None and st.report_opened_fingerprint(DAY) is None
        assert st.get_source_status(EDU) is None
        # a later real run still writes everything and the gate does not suppress it
        out2 = env.run()
        assert out2.report.status == OverallStatus.SUCCESS and not out2.silent
        assert len(env.google.events) == 2 and len(env.google.open_tasks) == 2

    def test_preview_ignores_the_day_gate_and_never_books_success(self, env):
        env.set_items(S.assignment())
        env.run()
        n = len(env.google.write_log)
        env.set_items(S.assignment(), S.homework(due=date(2026, 10, 20)))
        run_id, wu_day = env.state.last_run().id, env.state.get_source_day(DAY, WU)
        out = env.run(PREVIEW)
        assert not out.silent and out.report.mode == PREVIEW and len(env.google.write_log) == n
        assert [c.status for c in out.report.changes] == [ChangeStatus.PLANNED] * 2
        assert sources(out, EDU).status == SourceStatus.SUCCESS and env.state.last_run().id == run_id
        assert env.state.get_source_day(DAY, WU) == wu_day  # the preview booked nothing for WebUntis
        assert len(env.state.all_items()) == 1

    def test_preview_does_not_touch_auth_blocks_and_never_opens_the_browser(self, env):
        env.adapters[WU].raise_next(AuthError("abgelehnt"))
        env.adapters[EDU].raise_next(TransientError("x"), times=4)
        out = env.run(PREVIEW)
        assert not env.state.is_auth_blocked(WU) and env.state.get_source_status(WU) is None
        assert env.state.get_source_status(EDU) is None
        assert out.exit_code == 1 and env.opened == [] and not out.report.needs_attention

    def test_preview_with_a_blocked_source_still_tries_it_and_keeps_the_block(self, env):
        env.adapters[WU].raise_next(AuthError("abgelehnt"))
        env.run()
        env.set_items(S.exam())
        out = env.run(PREVIEW)
        assert env.state.is_auth_blocked(WU) and sources(out, WU).status == SourceStatus.SUCCESS and env.fetch_calls(WU) == 2

    def test_preview_without_a_google_login_is_still_possible_but_says_so(self, env):
        env.set_items(S.assignment())
        env.gateway_error = GoogleAuthError("Google-Anmeldung abgelaufen.")
        out = env.run(PREVIEW)
        assert out.exit_code == 0 and len(out.report.changes) == 2
        assert "preview_incomplete" in {n.code for n in out.report.notices}

    def test_preview_survives_unreadable_google_reads(self, env):
        env.set_items(S.assignment())
        env.run()
        env.google.fail_always("list_tasks", NetworkError)
        env.set_items(replace(S.assignment(), when=ItemTime(S.at(2026, 10, 28, 23, 59))))
        out = env.run(PREVIEW)
        codes = {n.code for n in out.report.notices}
        assert out.report.mode == PREVIEW and "preview_incomplete" in codes
        # an unreadable list is unknowable, not "the user deleted the task": the change is planned, nothing is claimed
        assert not codes & {"user_deleted_event", "user_deleted_task"}
        assert [(c.target.value, c.action.value, c.status) for c in out.report.changes] == [
            ("calendar", "update", ChangeStatus.PLANNED),
            ("tasks", "update", ChangeStatus.PLANNED),
        ]

    def test_preview_without_a_google_login_plans_known_entries_without_claiming_deletions(self, env):
        env.set_items(S.assignment())
        env.run()
        env.gateway_error = GoogleAuthError("Google-Anmeldung abgelaufen.")  # e.g. the 7-day limit of the Testing status
        env.set_items(replace(S.assignment(), when=ItemTime(S.at(2026, 10, 28, 23, 59))))
        out = env.run(PREVIEW)
        codes = {n.code for n in out.report.notices}
        assert "preview_incomplete" in codes and not codes & {"user_deleted_event", "user_deleted_task"}
        assert [c.status for c in out.report.changes] == [ChangeStatus.PLANNED, ChangeStatus.PLANNED]
        assert env.state.get(S.assignment().uid).event_state == EventState.ACTIVE  # the real state is untouched


# ====================================================================================== crashes and configuration errors


class TestUnexpectedProblems:
    def test_unexpected_exception_becomes_a_failed_report(self, env):
        env.set_items(S.assignment())
        env.adapters[EDU].raise_next(RuntimeError("kaputt password=geheim1234 im Programm"))
        out = env.run()  # must not raise
        assert out.exit_code == 1 and out.report.status == OverallStatus.PARTIAL and not out.silent
        err = sources(out, EDU).errors[0]
        assert err.code == "unexpected" and "geheim1234" not in (err.detail or "") and "RuntimeError" in err.detail
        assert out.report_path.is_file() and "geheim1234" not in out.report_path.read_text(encoding="utf-8")
        assert env.opened == [out.report_path] and "geheim1234" not in out.console_text
        assert sources(out, WU).status == SourceStatus.SUCCESS  # the other source is fetched and written regardless
        assert not env.state.day_done(DAY, EDU) and env.state.day_done(DAY, WU)
        out2 = env.run()  # the next start simply works
        assert out2.report.status == OverallStatus.SUCCESS and len(env.google.events) == 1

    def test_unexpected_exception_in_the_write_phase_keeps_the_ingested_data(self, env, monkeypatch):
        a = S.assignment()
        env.set_items(a)

        def boom(self, sources, now):
            raise ZeroDivisionError("kaputt")

        monkeypatch.setattr(SyncEngine, "write_phase", boom)
        out = env.run()
        assert out.report.status == OverallStatus.FAILED and sources(out, EDU).errors[0].code == "unexpected"
        assert env.state.get(a.uid).dirty and not env.state.day_done(DAY, EDU) and env.google.write_log == []
        monkeypatch.undo()
        assert env.run().report.status == OverallStatus.SUCCESS and len(env.google.events) == 1

    def test_unreadable_database_is_a_configuration_error_with_a_report(self, env, tmp_path):
        other = Paths(tmp_path / "Kaputt")
        other.ensure()
        other.db.write_bytes(b"das ist keine Datenbank" * 40)
        out = env.run(paths=other, state=None)
        assert out.exit_code == 2 and out.report is not None and out.report.status == OverallStatus.FAILED
        assert "Zustandsdatenbank" in out.console_text and out.report_path.is_file()

    def test_without_an_injected_state_the_runner_opens_and_closes_its_own(self, env):
        env.set_items(S.assignment())
        out = env.run(state=None)
        assert out.exit_code == 0 and env.state.day_done(DAY, EDU) and env.state.last_run().status == OverallStatus.SUCCESS


# ====================================================================================== window and rules through the runner


class TestRulesThroughTheRunner:
    def test_window_edges_at_runner_level(self, env):
        inside = S.homework(hid="in", due=date(2026, 11, 3))
        outside = S.homework(hid="out", due=date(2026, 11, 4), title="Zu spät")
        env.set_items(inside, outside)
        out = env.run()
        assert list(env.google.events) == [eid(inside)]
        assert sources(out, WU).calendar.skipped_reasons == {"outside_window": 1}
        assert out.report.status == OverallStatus.SUCCESS and sources(out, WU).calendar.skipped == 1

    def test_cancellation_and_reactivation_through_two_days(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        env.advance(days=1)
        env.set_items(replace(a, status=ItemStatus.CANCELLED))
        out = env.run()
        assert env.google.events[eid(a)]["summary"].startswith("Abgesagt: ") and env.google.open_tasks == []
        assert sources(out, EDU).calendar.cancelled == 1 and sources(out, EDU).tasks.cancelled == 1
        env.advance(days=1)
        env.set_items(a)
        out = env.run()
        assert len(env.google.writes("insert_event")) == 1 and len(env.google.open_tasks) == 1
        assert sources(out, EDU).calendar.reactivated == 1 and sources(out, EDU).tasks.reactivated == 1

    def test_the_adapter_gets_the_window_and_the_known_open_items(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        env.advance(days=1)
        env.run()
        window, tracked, now = env.adapters[EDU].calls[-1]
        assert window.start == S.at(2026, 10, 15) and window.end == S.at(2026, 11, 5) and now == env.now
        assert [(t.uid, t.cancelled) for t in tracked] == [(a.uid, False)]
        env.advance(days=30)  # fully past items are no longer tracked
        env.set_items()
        env.run()
        assert env.adapters[EDU].calls[-1][1] == ()
