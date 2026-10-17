"""Regression tests for details of the round-2 fixes that the independent review tests (test_review2_*.py) do not pin.

Each test names the behaviour it protects; they exercise the engine (`Rig`) and the runner (`Env`) with the test doubles
only: deterministic, no network, no real clock.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from schulsync.errors import ErrorInfo, NetworkError, OutcomeUnknownError, TargetUnavailableError
from schulsync.google_types import make_event_id
from schulsync.models import ChangeStatus, ItemStatus, Target, TaskState
from tests.support import samples as S
from tests.support.engine_harness import EDU, WU, Env, Rig
from tests.support.fake_google import FakeGoogle

CAL, TSK = Target.CALENDAR, Target.TASKS


class ListFailsAt(FakeGoogle):
    """list_tasks call number `fail_at` fails (counted from 1 over the whole life of the fake)."""

    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0
        self.fail_at: int | None = None

    def list_tasks(self):
        self.list_calls += 1
        if self.list_calls == self.fail_at:
            raise NetworkError("Kein Netz (Test).")
        return super().list_tasks()


# ====================================================================================== engine


class TestPatchInvalidation:
    def test_text_only_patch_with_lost_response_does_not_make_the_planner_rewrite_the_due_date(self):
        """P-02: only a patch that carries the due date may forget the base deadline. A text-only patch must not turn
        the next plan into a 'deadline change' that would reset a due date the user moved on purpose."""
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.user_move_task_due(rig.google.tasks[0]["id"], date(2026, 10, 20))  # the user's own choice
        rig.google.fail_next("patch_task", OutcomeUnknownError, applied=True)
        rig.sync(replace(a, title="Anderer Titel"))
        rig.sync(replace(a, title="Wieder anderer Titel"))
        (task,) = rig.google.open_tasks
        assert task["due"] == "2026-10-20T00:00:00.000Z"
        assert task["title"] == "Bearbeiten: Wieder anderer Titel"


class TestTargetsFailIndependently:
    def test_a_calendar_that_vanishes_mid_run_defers_calendar_writes_only(self):
        """P-06, the other direction: the calendar going away after the check must not stop the task writes."""
        rig = Rig()
        a = S.assignment("1")
        b = S.assignment("2", title="Andere Abgabe", due=S.at(2026, 10, 22, 23, 59))
        rig.google.fail_next("insert_event", TargetUnavailableError)
        eng = rig.sync(a, b)
        assert eng.calendar_target.ok is False and eng.tasks_target.ok is not False
        assert len(rig.google.events) == 0 and len(rig.google.open_tasks) == 2
        statuses = sorted(c.status for c in eng.changes if c.target == CAL)
        assert statuses == sorted([ChangeStatus.FAILED, ChangeStatus.DEFERRED])


class TestEventIdCollision:
    def test_the_old_uid_returning_before_the_adopted_event_was_rewritten_gets_its_own_event(self):
        """P-03, other order: the adopted item still owns the old event id while that event still carries the OLD uid
        (Google was not written in between). The returning old uid must not take over the event of the other item."""

        def hw(d):
            h = S.homework("x", title="Hausuebung", due=d, course="Mathe")
            return replace(h, source_id=f"hw:Hausuebung|{d.isoformat()}", id_stable=False)

        rig = Rig()
        first, moved_to = hw(date(2026, 10, 16)), hw(date(2026, 10, 19))
        rig.sync(first)
        rig.sync(moved_to, write=False)  # adopted by identity; Google is not written yet
        eng = rig.sync(first, moved_to)  # the old uid shows up again as a NEW item
        assert [c.error for c in eng.changes if c.error is not None] == []
        uids = {e["extendedProperties"]["private"]["schulsync_uid"] for e in rig.google.events.values()}
        assert len(rig.google.events) == 2 and uids == {first.uid, moved_to.uid}


class TestInterruptedTaskCreate:
    def test_a_tombstone_of_an_earlier_task_is_not_taken_for_a_user_deletion_after_a_reactivation(self):
        """P-04, guard: the deleted marker task is only evidence of a USER deletion for an item that never had a task
        (state NONE). After cancel + reactivation the old tombstone is OUR deletion; the interrupted second create
        must be redone, not turned into 'deleted by the user'."""
        rig = Rig()
        rig.google = g = ListFailsAt()
        a = S.assignment()
        rig.sync(a)  # list call 1: task created
        rig.sync(replace(a, status=ItemStatus.CANCELLED))  # list call 2: our delete -> state REMOVED, tombstone stays
        assert rig.st(a).task_state == TaskState.REMOVED
        g.fail_at = g.list_calls + 2  # run 3: the first list is fine, the reconcile read after the unclear insert fails
        g.fail_next("insert_task", OutcomeUnknownError)  # not applied
        rig.sync(a)  # reactivated: create intent stays open
        assert len(rig.store.open_intents()) == 1 and g.open_tasks == []
        rig.sync(a)  # run 4: recovery finds nothing live
        assert rig.st(a).task_state == TaskState.OPEN
        assert len(g.open_tasks) == 1 and rig.store.open_intents() == []


class TestContainedErrors:
    @staticmethod
    def _poison(rig: Rig, item) -> None:
        real = rig.google.insert_event

        def insert(body):
            if body["id"] == make_event_id(item.uid, 0):
                raise KeyError("etag")  # a bug, not a SchulSyncError
            return real(body)

        rig.google.insert_event = insert

    def test_a_contained_engine_reports_the_failed_item_and_writes_the_others(self):
        rig = Rig()
        a, x = S.assignment(), S.exam()
        self._poison(rig, a)
        eng = rig.sync(a, x, engine=rig.engine(contain_errors=True))
        assert make_event_id(x.uid, 0) in rig.google.events
        bad = [c for c in eng.changes if c.error is not None]
        assert [(c.uid, c.target, c.status, c.error.code) for c in bad] == [(a.uid, CAL, ChangeStatus.FAILED, "unexpected")]
        assert rig.st(a).dirty and not rig.st(x).dirty

    def test_without_the_flag_a_bug_still_propagates(self):
        """The engine's own contract is unchanged: bugs surface unless the caller (the runner) asks for containment."""
        rig = Rig()
        a = S.assignment()
        self._poison(rig, a)
        with pytest.raises(KeyError):
            rig.sync(a)


class TestHostileText:
    def test_a_lone_surrogate_in_a_title_is_replaced_and_the_item_still_syncs(self):
        """R2-RUN-2: it cannot be hashed or stored; the item must not cost the whole ingest of its source."""
        rig = Rig()
        h = S.homework(title="Beispiele \ud83d")
        rig.sync(h)
        assert rig.st(h).item.title == "Beispiele �"
        (event,) = rig.google.events.values()
        assert event["summary"] == "Abgabe: Beispiele �"


# ====================================================================================== runner


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.state.close()


class TestAuthBlocking:
    def test_a_permanent_refusal_blocks_automatic_starts(self, env):
        """R2-RUN-4: besides a rejected login ('auth'), a permanent refusal ('access_denied', e.g. terms not accepted)
        is not retried on every logon either."""
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].login_fails(ErrorInfo("access_denied", "WebUntis verweigert den Zugriff."))
        env.run()
        assert env.state.is_auth_blocked(WU) and not env.state.is_auth_blocked(EDU)


class TestTabFloodFile:
    def test_a_leftover_marker_file_of_an_older_version_is_ignored_and_the_database_remembers_the_tab(self, env):
        """R2-RUN-6/7, reworked for H6: the tab memory lives in the state database (report_opened, schema v2). The old
        `report_opened.json` is gone for good: a damaged leftover must not matter and is never written again."""
        leftover = env.paths.home / "report_opened.json"
        leftover.write_text("{kaputt", encoding="utf-8")
        env.set_items(S.assignment(), S.exam())
        env.adapters[WU].login_fails(ErrorInfo("schema", "WebUntis-Anmeldeseite unerwartet."))
        out = env.run()
        assert out.report_path is not None and len(env.opened) == 1
        assert leftover.read_text(encoding="utf-8") == "{kaputt"  # untouched: nobody reads or writes it any more
        assert env.state.report_opened_fingerprints(date(2026, 10, 14)) == {out.report.fingerprint()}
        env.advance(hours=1)
        env.run()
        assert len(env.opened) == 1  # the same problem again: no second tab
