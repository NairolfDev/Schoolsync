"""Adversarial review (round 2) of the runner: each test pins ONE defect with a minimal deterministic scenario."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date

import pytest

from schulsync import runner
from schulsync.config import Config, Paths, RetryConfig
from schulsync.errors import ConfigError, ErrorInfo, GoogleAuthError, TransientError
from schulsync.google_types import make_event_id
from schulsync.models import OverallStatus, RunMode, SourceStatus
from tests.support import samples as S
from tests.support.engine_harness import EDU, WU, Env

DAY = date(2026, 10, 14)
AUTO, MANUAL, PREVIEW = RunMode.AUTO, RunMode.MANUAL, RunMode.PREVIEW


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.state.close()


def eid(item, gen: int = 0) -> str:
    return make_event_id(item.uid, gen)


class TestSourceIndependence:
    def test_unexpected_error_in_one_source_does_not_stop_the_other(self, env):
        """ANFORDERUNGEN 7 / DESIGN 7.4: the failure of one source must not prevent the success of the other."""
        a, x = S.assignment(), S.exam()
        env.set_items(a, x)
        env.adapters[EDU].raise_next(RuntimeError("Parserfehler in Eduvidual"))
        out = env.run()
        assert eid(x) in env.google.events  # WebUntis was still fetched and written
        assert out.report.sources[WU].status == SourceStatus.SUCCESS
        assert env.state.day_done(DAY, WU)
        assert out.report.sources[EDU].errors[0].code == "unexpected"

    def test_unexpected_error_of_the_adapter_factory_does_not_stop_the_other_source(self, env):
        x = S.exam()
        env.set_items(S.assignment(), x)
        env.factory_errors[EDU] = OSError("Geheimnisspeicher nicht lesbar")
        out = env.run()
        assert eid(x) in env.google.events
        assert out.report.sources[WU].status == SourceStatus.SUCCESS

    def test_poison_title_in_one_source_does_not_keep_the_other_source_out_of_google(self, env):
        """A lone surrogate (JSON "\\ud83d", e.g. a title cut in the middle of an emoji) must not cost the whole sync."""
        a = S.assignment()
        env.set_items(a, S.homework(title="Beispiele \ud83d"))
        out = env.run()
        assert eid(a) in env.google.events, out.console_text
        assert out.report.sources[EDU].status == SourceStatus.SUCCESS

    def test_unexpected_error_while_writing_one_item_does_not_block_the_items_of_the_other_source(self, env):
        """A bug that is triggered by ONE item (here: a calendar insert) must not keep every other item, of either
        source, out of Google on every start (poison item)."""
        a, x = S.assignment(), S.exam()
        env.set_items(a, x)
        real = env.google.insert_event

        def insert(body):
            if body["id"] == eid(a):
                raise KeyError("etag")  # not a SchulSyncError: an unexpected bug for this item only
            return real(body)

        env.google.insert_event = insert
        out = env.run()
        assert eid(x) in env.google.events  # the WebUntis exam is not lost because of the Eduvidual item
        assert out.report.sources[WU].status == SourceStatus.SUCCESS


class TestAuthBlockOnlyForRejectedCredentials:
    @pytest.mark.parametrize("code", ["not_found", "error"])
    def test_a_login_failure_that_is_not_a_credential_verdict_does_not_block_the_source(self, env, code):
        """ANFORDERUNGEN 1: only DAUERHAFTE Anmeldefehler stop the automatic retries. A login that dies on a missing
        page (HTTP 404, e.g. maintenance or a moved endpoint) or an unexpected status says nothing about the password,
        yet the runner blocks the source for ALL later automatic starts until the user intervenes."""
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].login_fails(ErrorInfo(code, "WebUntis-Anmeldung: Seite oder Schnittstelle nicht gefunden."))
        env.run()
        assert not env.state.is_auth_blocked(WU)
        env.adapters[WU].login_ok()
        out = env.run()  # next logon of the day: the healed source is simply fetched again
        assert out.report.sources[WU].status == SourceStatus.SUCCESS


class TestTabFlood:
    def test_unreadable_database_gives_the_runner_no_memory_so_the_cli_guards_the_tab(self, env, tmp_path):
        """ANFORDERUNGEN 8: identical errors on the same day must not open a tab each time. Reworked for H6: the runner's
        own tab memory is the state database only (the `report_opened.json` workaround is removed), so a runner that
        cannot open the database opens the report on every start (ACCEPTANCE_NOTES A10). The user-visible protection for
        this case moved to the CLI, which probes the database first and uses its database-independent marker; see
        tests/test_harden_cli.py::test_run_auto_with_an_unreadable_database_opens_one_tab_per_day."""
        bad = Paths(tmp_path / "Kaputt")
        bad.ensure()
        bad.db.write_bytes(b"das ist keine Datenbank" * 40)
        env.run(paths=bad, state=None)
        env.advance(hours=1)
        env.run(paths=bad, state=None)
        assert len(env.opened) == 2 and all(p.is_file() for p in env.opened)

    def test_a_problem_that_was_already_shown_today_does_not_open_a_third_tab_after_another_problem(self, env):
        """A, B, A on the same day: A was shown already (ANFORDERUNGEN 8, wiederholte identische Fehler)."""
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].raise_next(TransientError("Zeitüberschreitung."), times=4)  # problem A
        env.run()
        env.advance(hours=1)
        env.adapters[WU].raise_next(RuntimeError("Parserfehler"))  # problem B
        env.run()
        env.advance(hours=1)
        env.adapters[WU].raise_next(TransientError("Zeitüberschreitung."), times=4)  # A again
        env.run()
        assert len(env.opened) == 2


class TestHostileNoticeText:
    def test_warning_notice_with_a_lone_surrogate_in_the_title_does_not_cost_the_report_in_warnings_mode(self, tmp_path):
        """A `no_date` notice is never stored in the database, so its (hostile) title reaches the runner's own
        fingerprint code, which hashes it without the surrogate guard that report.fingerprint() has."""
        from schulsync.config import ReportConfig
        from schulsync.models import Notice, Severity

        e = Env(tmp_path, cfg=Config(report=ReportConfig(open_on="warnings")))
        try:
            e.set_items(S.assignment())
            e.adapters[EDU].set_notices([Notice(Severity.WARNING, "no_date", "Eintrag ohne Datum.", EDU, title="Projekt \ud83d")])
            out = e.run()
            assert out.report is not None and out.report_path is not None and out.report_path.is_file()
        finally:
            e.state.close()


class TestPreviewWithoutGoogle:
    @pytest.mark.parametrize(
        "error, needle",
        [
            (ConfigError("Kalender 'Schule' und Aufgabenliste 'Schule' sind noch nicht eingerichtet (leer: calendar_id)."), "nicht eingerichtet"),
            (GoogleAuthError("Die Google-Anmeldung ist abgelaufen oder widerrufen."), "abgelaufen"),
        ],
    )
    def test_preview_names_the_real_reason_why_google_could_not_be_read(self, env, error, needle):
        """The runner stores the reason (`preview_google_error`) but never shows it: the notice always claims that
        there is no Google login, which is wrong (and misleading) for a missing setup or an expired token."""
        env.set_items(S.assignment())
        env.gateway_error = error
        out = env.run(PREVIEW)
        notice = next(n for n in out.report.notices if n.code == "preview_incomplete")
        assert needle in notice.message
        assert "keine Google-Anmeldung vorhanden" not in notice.message


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt byte-range lock")
class TestLockErrors:
    def test_a_lock_that_cannot_be_taken_for_another_reason_than_contention_is_not_reported_as_another_run(
        self, env, monkeypatch
    ):
        """lock._try_lock treats EVERY OSError of msvcrt.locking as "another process holds the lock". On a file system
        that cannot lock (EINVAL/ENOSYS: some network or sync-client profiles) every automatic start would then end
        silently with exit 0 and the log line "ein anderer Lauf ist aktiv" - SchulSync would never run and never say so."""
        import errno

        from schulsync import lock

        def broken(fd, mode, nbytes):
            raise OSError(errno.EINVAL, "Invalid argument")

        monkeypatch.setattr(lock.msvcrt, "locking", broken)
        env.set_items(S.assignment())
        out = env.run()
        assert not out.silent or out.exit_code != 0
