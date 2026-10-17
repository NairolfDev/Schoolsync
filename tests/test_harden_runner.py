"""Runner level tests of the hardening work: H2 (changed targets before the day gate, preview), H5 (restore output),
H6 (tab memory in the database), H9 (SchulSyncError crash is logged, plain-text fallback for a lost HTML report)."""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from datetime import date

import pytest

from schulsync import runner
from schulsync.config import Config, GoogleConfig
from schulsync.errors import ConfigError, ErrorInfo
from schulsync.google_types import make_event_id
from schulsync.logging_setup import prune_logs
from schulsync.models import ChangeStatus, EventState, ItemStatus, ItemTime, OverallStatus, RunMode, Severity
from schulsync.redact import clear_registered_secrets, register_secret
from tests.support import samples as S
from tests.support.engine_harness import NOW, WU, Env
from tests.support.fake_google import FakeGoogle

DAY = date(2026, 10, 14)
AUTO, MANUAL, PREVIEW = RunMode.AUTO, RunMode.MANUAL, RunMode.PREVIEW


def cfg_with(calendar_id: str = "cal-1", tasklist_id: str = "list-1") -> Config:
    return Config(google=GoogleConfig(calendar_id=calendar_id, tasklist_id=tasklist_id))


def eid(item, gen: int = 0) -> str:
    return make_event_id(item.uid, gen)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, cfg=cfg_with())
    yield e
    e.state.close()


@pytest.fixture
def logs(caplog):
    lg = logging.getLogger("schulsync")
    lg.addHandler(caplog.handler)
    lg.setLevel(logging.DEBUG)
    yield caplog
    lg.removeHandler(caplog.handler)


@pytest.fixture(autouse=True)
def _secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


# ====================================================================================== H2


class TestChangedTargets:
    def test_a_day_that_is_already_done_is_written_again_into_the_new_targets(self, env):
        a, x = S.assignment(), S.exam()
        env.set_items(a, x)
        assert env.run().report.status == OverallStatus.SUCCESS
        assert env.run().silent  # the day gate: nothing more today
        old_google, old_writes = env.google, list(env.google.write_log)

        env.google = FakeGoogle()  # the user re-created calendar and list ...
        env.cfg = cfg_with("cal-2", "list-2")  # ... and 'schulsync setup' stored their new ids
        env.advance(hours=1)
        out = env.run()  # the SAME day, an automatic start

        assert not out.silent and out.report.status == OverallStatus.SUCCESS
        assert set(env.google.events) == {eid(a, 1), eid(x, 1)} and len(env.google.open_tasks) == 2
        assert old_google.write_log == old_writes  # the old targets are not touched
        codes = [n.code for n in out.report.notices]
        assert "targets_changed" in codes and "user_deleted_event" not in codes and "user_deleted_task" not in codes
        assert any("neuen Zielen" in n.message for n in out.report.notices if n.code == "targets_changed")
        assert env.state.pending_count() == 0 and env.run().silent  # and quiet again afterwards

    def test_the_same_targets_do_not_touch_a_finished_day(self, env):
        env.set_items(S.assignment(), S.exam())
        env.run()
        env.google.reset_write_log()
        out = env.run()
        assert out.silent and env.google.write_log == []

    def test_a_preview_with_changed_targets_shows_the_plan_but_keeps_the_real_database(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        env.google = FakeGoogle()
        env.cfg = cfg_with("cal-2", "list-2")
        out = env.run(PREVIEW)
        assert env.google.write_log == []  # a preview writes nothing
        planned = [c for c in out.report.changes if c.status == ChangeStatus.PLANNED]
        assert len(planned) == 2 and "targets_changed" in [n.code for n in out.report.notices]
        assert env.state.get_meta("target.calendar_id") == "cal-1"  # real database untouched ...
        assert env.state.get(a.uid).event_gen == 0 and env.state.get(a.uid).event_state == EventState.ACTIVE
        out = env.run(MANUAL)  # ... so the real run still has to do the whole job
        assert eid(a, 1) in env.google.events

    def test_the_ids_are_remembered_after_the_first_real_run(self, env):
        env.set_items(S.assignment())
        env.run()
        assert env.state.get_meta("target.calendar_id") == "cal-1" and env.state.get_meta("target.tasklist_id") == "list-1"


# ====================================================================================== H5


def delete_and_move(env: Env, item, *when):
    env.google.user_delete_event(eid(item))
    env.set_items(replace(item, when=ItemTime(S.at(*when))))
    env.run(MANUAL)


class TestRestoreOutput:
    def test_a_cancelled_item_says_so_instead_of_pretending(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        delete_and_move(env, a, 2026, 10, 28, 23, 59)
        env.set_items(replace(a, when=ItemTime(S.at(2026, 10, 28, 23, 59)), status=ItemStatus.CANCELLED))
        env.run(MANUAL)
        writes = len(env.google.write_log)
        out = env.restore([a.uid])
        assert out.exit_code == 1 and out.report is None and not out.silent
        assert "Nicht wiederhergestellt" in out.console_text and "abgesagt" in out.console_text and a.uid in out.console_text
        assert len(env.google.write_log) == writes and env.google.events == {}
        assert env.state.get(a.uid).event_state == EventState.USER_DELETED  # still listed by 'status'

    def test_an_expired_item_says_so(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        delete_and_move(env, a, 2026, 10, 18, 23, 59)
        env.advance(days=7)
        out = env.restore([a.uid])
        assert out.exit_code == 1 and "bereits vorbei" in out.console_text and a.uid in out.console_text

    def test_restore_all_with_only_blocked_entries_explains_and_exits_0(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        delete_and_move(env, a, 2026, 10, 18, 23, 59)
        env.advance(days=7)
        out = env.restore(None)
        assert out.exit_code == 0 and out.report is None
        assert "keine wiederherstellbaren" in out.console_text and "bereits vorbei" in out.console_text

    def test_a_uid_that_is_not_deleted_gets_its_own_line_and_exit_2(self, env):
        a = S.assignment()
        env.set_items(a)
        env.run()
        out = env.restore([a.uid, "eduvidual:assignment:assign:nope"])
        assert out.exit_code == 2 and out.report is None
        assert "nicht von dir in Google gelöscht" in out.console_text and "unbekannt" in out.console_text.lower()
        assert a.uid in out.console_text and "assign:nope" in out.console_text

    def test_restore_all_with_nothing_deleted_keeps_its_old_message(self, env):
        env.set_items(S.assignment())
        env.run()
        out = env.restore(None)
        assert out.exit_code == 0 and "keine bewusst gelöschten" in out.console_text

    def test_mixed_restore_lists_every_uid_and_puts_notices_into_the_report(self, env):
        a, b = S.assignment(), S.assignment(cmid="40214", title="Laborprotokoll 3")
        env.set_items(a, b)
        env.run()
        env.google.user_delete_event(eid(a))
        env.google.user_delete_event(eid(b))
        env.set_items(replace(a, when=ItemTime(S.at(2026, 10, 28, 23, 59))), replace(b, when=ItemTime(S.at(2026, 10, 29, 23, 59)), status=ItemStatus.CANCELLED))
        env.run(MANUAL)
        out = env.restore()
        assert out.exit_code == 0 and out.report is not None
        text = out.console_text
        assert "Freigegeben: 1" in text
        assert f"{a.uid} - {a.title}: Wiederhergestellt" in text and f"{b.uid} - {b.title}: Nicht wiederhergestellt" in text
        by_code = {n.code: n for n in out.report.notices if n.uid in (a.uid, b.uid)}
        assert by_code["restore_done"].uid == a.uid and by_code["restore_blocked"].uid == b.uid
        assert by_code["restore_blocked"].severity == Severity.WARNING
        html = out.report_path.read_text(encoding="utf-8")
        assert "Nicht wiederhergestellt" in html and "Wiederhergestellt" in html
        assert list(env.google.events) == [eid(a, 1)]


# ====================================================================================== H6


class TestTabMemoryInTheDatabase:
    def test_a_b_a_opens_two_tabs_and_the_database_holds_both_fingerprints(self, env):
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].login_fails(ErrorInfo("schema", "WebUntis-Anmeldeseite unerwartet."))  # problem A
        first = env.run()
        env.advance(hours=1)
        env.adapters[WU].login_fails(ErrorInfo("incomplete", "WebUntis lieferte nur einen Teil."))  # problem B
        second = env.run()
        env.advance(hours=1)
        env.adapters[WU].login_fails(ErrorInfo("schema", "WebUntis-Anmeldeseite unerwartet."))  # A again
        env.run()
        assert len(env.opened) == 2
        assert first.report.fingerprint() != second.report.fingerprint()
        assert env.state.report_opened_fingerprints(DAY) == {first.report.fingerprint(), second.report.fingerprint()}
        assert not (env.paths.home / "report_opened.json").exists()


# ====================================================================================== H9


class TestCrashLogging:
    def test_a_schulsync_error_crash_is_logged_with_code_and_message_redacted(self, env, monkeypatch, logs):
        register_secret("hunter2-dummy")

        def boom(path):
            raise ConfigError("Die Zustandsdatenbank ist beschädigt.", hint="Datei sichern.", detail="token=hunter2-dummy kaputt")

        monkeypatch.setattr(runner, "StateStore", boom)
        out = runner.run(env.deps(state=None), AUTO)
        assert out.exit_code == 2 and out.report is not None
        lines = [r.getMessage() for r in logs.records if r.levelno >= logging.ERROR]
        text = "\n".join(lines)
        assert "config" in text and "Zustandsdatenbank ist beschädigt" in text
        assert "hunter2-dummy" not in text and "***" in text


class TestPlainTextFallback:
    def _fail_report_writing(self, monkeypatch):
        monkeypatch.setattr(runner.report_mod, "write_report", lambda *a, **k: (_ for _ in ()).throw(OSError("Datenträger voll")))

    def test_a_failed_automatic_run_without_an_html_report_leaves_a_text_copy_and_opens_it(self, env, monkeypatch, logs):
        register_secret("hunter2-dummy")
        self._fail_report_writing(monkeypatch)
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].login_fails(ErrorInfo("schema", "WebUntis-Anmeldeseite unerwartet.", detail="password=hunter2-dummy"))
        out = env.run()
        txt = env.paths.logs / "bericht-2026-10-14.txt"
        assert out.exit_code == 1 and out.report_path == txt and txt.is_file()
        text = txt.read_text(encoding="utf-8")
        assert "WebUntis" in text and "Teilweise fehlgeschlagen" in text and "hunter2-dummy" not in text
        assert env.opened == [txt]  # a FAILED run never shows nothing at all
        logged = "\n".join(r.getMessage() for r in logs.records)
        assert "konnte nicht gespeichert werden" in logged and "bericht-2026-10-14.html" in logged  # the path it was meant for

    def test_if_even_the_text_copy_fails_the_run_only_logs(self, env, monkeypatch, logs):
        self._fail_report_writing(monkeypatch)
        shutil.rmtree(env.paths.logs)
        env.paths.logs.write_text("das ist eine Datei, kein Ordner", encoding="utf-8")  # nothing can be written below it
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].login_fails(ErrorInfo("schema", "WebUntis-Anmeldeseite unerwartet."))
        out = runner.run(env.deps(), AUTO)
        assert out.exit_code == 1 and out.report_path is None and out.console_text and env.opened == []
        assert "Textkopie" in "\n".join(r.getMessage() for r in logs.records)

    def test_a_successful_run_with_a_lost_report_writes_no_text_copy_and_opens_nothing(self, env, monkeypatch):
        self._fail_report_writing(monkeypatch)
        env.set_items(S.assignment(), S.exam())
        out = env.run()
        assert out.exit_code == 0 and env.opened == [] and list(env.paths.logs.glob("*.txt")) == []

    def test_old_text_copies_are_pruned_with_the_logs(self, tmp_path):
        from schulsync.config import Paths

        paths = Paths(tmp_path / "S")
        paths.ensure()
        old, new = paths.logs / "bericht-2026-08-01.txt", paths.logs / "bericht-2026-10-13.txt"
        for f in (old, new):
            f.write_text("x", encoding="utf-8")
        prune_logs(paths, 30, NOW)
        assert not old.exists() and new.exists()
