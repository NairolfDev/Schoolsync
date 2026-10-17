"""Adversarial review (area "protocol"): each test pins one confirmed defect of sync.py / rules.py / state.py.

Every test here FAILS on the current code for the stated reason (they document defects, they are not regression
tests of working behaviour). Deterministic: FakeGoogle, injected time, no network."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest

from schulsync.errors import NetworkError, OutcomeUnknownError, TargetUnavailableError
from schulsync.models import ItemStatus, ItemTime, Source, Target, TaskState
from schulsync.state import StateStore
from schulsync.timeutil import VIENNA
from tests.support import samples as S
from tests.support.engine_harness import EDU, NOW, Rig
from tests.support.fake_google import FakeGoogle

CAL, TSK = Target.CALENDAR, Target.TASKS


def cancelled(item):
    return replace(item, status=ItemStatus.CANCELLED)


def moved(item, *when):
    return replace(item, when=ItemTime(S.at(*when)))


class ListFailsOnSecondRead(FakeGoogle):
    """list_tasks #1 works (the run's task list), #2 fails (the reconcile read after an unclear insert)."""

    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0
        self.block = False

    def list_tasks(self):
        self.list_calls += 1
        if self.list_calls == 2 or self.block:
            raise NetworkError("Kein Netz (Test).")
        return super().list_tasks()


def only_event(rig: Rig) -> dict:
    (rec,) = rig.google.events.values()
    return rec


# ====================================================================================== 1. dirty cleared with an open intent


class TestDirtyClearedWhileIntentOpen:
    def test_event_with_open_create_intent_withdrawn_while_google_is_down_stays_live(self):
        """DESIGN 6 / ANFORDERUNGEN 6. Run 1: insert_event applied, response lost (intent open). Run 2: the school
        withdraws the item while the target check fails: the planner sees event NONE + cancelled -> SKIP
        'already_cancelled' -> `_process` clears `dirty` although the create intent is still open and Google holds a
        live event. Run 3: the recovery adopts the orphan as ACTIVE (hash of the NON-cancelled payload) but the item
        is no longer dirty, so nobody ever marks it 'Abgesagt:' and its 24h reminder stays."""
        rig = Rig()
        c = S.cancellation()
        rig.google.fail_next("insert_event", OutcomeUnknownError, applied=True)
        rig.sync(c)
        assert len(rig.google.events) == 1 and len(rig.store.open_intents()) == 1
        rig.google.fail_next("check_targets", NetworkError)
        rig.sync(cancelled(c))  # run 2: Google not reachable
        rig.sync(cancelled(c))  # run 3: Google back
        rec = only_event(rig)
        assert rec["summary"].startswith("Abgesagt:"), rec["summary"]
        assert rec["reminders"]["overrides"] == []

    def test_task_with_open_create_intent_cancelled_while_list_unreadable_stays_open(self):
        """Same root cause for tasks: run 1 inserts the task (response lost, reconcile read fails -> intent stays
        open); run 2 cancels while the list is unreadable (plan NONE 'already_cancelled' clears dirty); run 3 adopts
        the orphan as OPEN task of a cancelled item and nobody removes it (ANFORDERUNGEN 6: remove the open task)."""
        rig = Rig()
        rig.google = ListFailsOnSecondRead()
        a = S.assignment()
        rig.google.fail_next("insert_task", OutcomeUnknownError, applied=True)
        rig.sync(a)
        assert len(rig.google.tasks) == 1 and len(rig.store.open_intents()) == 1
        rig.google.block = True
        rig.sync(cancelled(a))  # run 2: list unreadable
        rig.google.block = False
        rig.sync(cancelled(a))  # run 3
        assert rig.google.open_tasks == [], rig.google.open_tasks
        assert not rig.st(a).dirty and rig.store.open_intents() == []


# ====================================================================================== 2. user deletion of an orphan task


class TestUserDeletionOfAnInterruptedTask:
    def test_task_created_by_an_interrupted_run_and_deleted_by_the_user_is_created_again(self):
        """DESIGN 6 + ANFORDERUNGEN 6/7 ('geloeschte Task-Zustaende bei der Zuordnung beruecksichtigen'). The process
        dies after insert_task (intent open); before the next run the user deletes that task. `_recover_task_create`
        only looks at LIVE marker tasks, aborts the intent ('nicht angelegt') and the item loop inserts a second task:
        a deliberate deletion is silently undone. (The event path records USER_DELETED for the same situation.)"""
        rig = Rig()
        rig.google = ListFailsOnSecondRead()
        a = S.assignment()
        rig.google.fail_next("insert_task", OutcomeUnknownError, applied=True)
        rig.sync(a)
        assert len(rig.google.tasks) == 1 and len(rig.store.open_intents()) == 1
        rig.google.user_delete_task(rig.google.tasks[0]["id"])
        rig.sync(a)
        assert rig.google.open_tasks == [], "the task the user deleted was created again"
        assert len(rig.google.writes("insert_task")) == 1
        assert rig.st(a).task_state == TaskState.USER_DELETED


# ====================================================================================== 3. patch without invalidation


class TestUnclearPatchThenRevert:
    def test_event_patch_with_lost_response_then_source_reverts_leaves_google_on_the_intermediate_state(self):
        """DESIGN 6 says patches 'need no intent' because they are idempotent. They are, but `event_applied_hash`
        is only updated after a SUCCESSFUL patch, so after a lost response the state claims 'Google = old content'
        while Google holds the new content. If the source reverts to the old content (hash == applied hash) the planner
        answers NONE and the wrong deadline stays in the calendar. (`_adopt_event` does invalidate the hash first.)"""
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        original_start = only_event(rig)["start"]["dateTime"]
        rig.google.fail_next("patch_event", OutcomeUnknownError, applied=True)
        rig.sync(moved(a, 2026, 10, 28, 23, 59))
        assert only_event(rig)["start"]["dateTime"].startswith("2026-10-28")  # Google applied it
        rig.sync(a)  # the school put the original deadline back
        assert only_event(rig)["start"]["dateTime"] == original_start

    def test_event_cancel_patch_lost_then_reactivated_stays_marked_abgesagt(self):
        """Same hole through cancellation: the 'Abgesagt:' patch was applied, the response lost; the item is then
        un-cancelled: desired(not cancelled) == stored applied hash -> NONE -> the event keeps 'Abgesagt:' and no
        reminder although the school says the entry is valid."""
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.fail_next("patch_event", NetworkError, applied=True)
        rig.sync(cancelled(a))
        assert only_event(rig)["summary"].startswith("Abgesagt:")
        rig.sync(a)
        rec = only_event(rig)
        assert not rec["summary"].startswith("Abgesagt:") and rec["reminders"]["overrides"] != []

    def test_process_death_after_the_event_patch_then_revert_leaves_the_wrong_state_in_google(self, monkeypatch):
        """Crash window (ANFORDERUNGEN 10 'Prozessabbruch nach einem Google-Schreibvorgang') for an UPDATE: patch
        applied, the process dies before the commit, the school reverts -> NONE (see above). Only creates/deletes
        have intents."""
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        original_start = only_event(rig)["start"]["dateTime"]
        eng = rig.engine()
        eng.ingest(rig.fetch(EDU, [moved(a, 2026, 10, 28, 23, 59)]), NOW)
        real = StateStore.upsert

        def die(self, state, at=None):
            raise RuntimeError("Prozess abgebrochen (Test).")

        monkeypatch.setattr(StateStore, "upsert", die)
        with pytest.raises(RuntimeError):
            eng.write_phase([EDU], NOW)
        monkeypatch.setattr(StateStore, "upsert", real)
        assert only_event(rig)["start"]["dateTime"].startswith("2026-10-28")
        rig.sync(a)
        assert only_event(rig)["start"]["dateTime"] == original_start

    def test_task_patch_with_lost_response_then_source_reverts_keeps_the_moved_task_date(self):
        """Same hole for tasks: patch_task (title + `due`) applied, response lost, deadline reverted ->
        `task_base_deadline`/`task_applied_hash` still describe the old task -> NONE; the task keeps the due date
        of the intermediate deadline (ANFORDERUNGEN 5: task date = deadline minus 3 calendar days)."""
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        assert rig.google.open_tasks[0]["due"] == "2026-10-18T00:00:00.000Z"
        rig.google.fail_next("patch_task", OutcomeUnknownError, applied=True)
        rig.sync(moved(a, 2026, 10, 28, 23, 59))
        assert rig.google.open_tasks[0]["due"] == "2026-10-25T00:00:00.000Z"
        rig.sync(a)
        assert rig.google.open_tasks[0]["due"] == "2026-10-18T00:00:00.000Z"


# ====================================================================================== 4. personal notes clipped


class TestPersonalNotesAreKept:
    def test_long_personal_text_in_the_event_description_is_clipped_on_update(self):
        """ANFORDERUNGEN 6: 'Persoenliche Notizen ... muessen erhalten bleiben'. `_update_event` calls
        `merge_description(existing, managed)` with the defaults (limit 8000, keep_personal=False): the user's own
        text after the block is cut at its end. textblocks documents `keep_personal=True` for calendar descriptions."""
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        eid = next(iter(rig.google.events))
        personal = "Meine Notiz. " * 800  # 10 400 characters of the user's own text
        rig.google.user_edit_event_description(eid, rig.google.events[eid]["description"] + "\n" + personal)
        rig.sync(replace(a, title="Anderer Titel"))
        assert rig.google.events[eid]["description"].count("Meine Notiz.") == 800


# ====================================================================================== 5. target halting


class TestTargetsAreIndependent:
    def test_a_missing_task_list_must_not_defer_calendar_writes_or_mark_the_calendar_unavailable(self):
        """ANFORDERUNGEN 4: 'Ist ein Ziel nicht erreichbar, die betroffenen Schreibvorgaenge zurueckstellen'. A
        TargetUnavailableError from the TASK list (`_ensure_list`) calls `_set_halt(targets=True)`: every later
        calendar write of the run is DEFERRED and the report claims the calendar 'Schule' is unavailable."""
        rig = Rig()
        a = S.assignment("1")
        b = S.assignment("2", title="Andere Abgabe", due=S.at(2026, 10, 22, 23, 59))
        rig.google.fail_next("list_tasks", TargetUnavailableError)
        eng = rig.sync(a, b)
        assert len(rig.google.events) == 2, "the second event was deferred although the calendar is fine"
        assert eng.calendar_target.ok is not False
        assert eng.tasks_target.ok is False


# ====================================================================================== 6. state.py: DST overlap


class TestStateDstOverlap:
    def test_last_complete_fetch_advances_across_the_autumn_overlap(self):
        """'Never compare aware datetimes in Europe/Vienna directly across the autumn DST overlap': `record_fetch`
        compares `to_vienna(at) > prev` (same tzinfo -> wall clock, fold ignored). 02:45 CEST (00:45 UTC) is stored
        first; a complete fetch at 02:10 CET (01:10 UTC, 25 minutes LATER) is not recorded as newer."""
        s = StateStore(":memory:")
        first = datetime(2026, 10, 25, 2, 45, tzinfo=VIENNA)  # fold=0: CEST
        later = datetime(2026, 10, 25, 2, 10, tzinfo=VIENNA, fold=1)  # CET, really later
        s.record_fetch(date(2026, 10, 25), Source.EDUVIDUAL, first, True)
        s.record_fetch(date(2026, 10, 25), Source.EDUVIDUAL, later, True)
        got = s.last_complete_fetch(Source.EDUVIDUAL)
        assert got.utcoffset().total_seconds() == 3600 and got.replace(tzinfo=None) == datetime(2026, 10, 25, 2, 10)


# ====================================================================================== 7. event id collision after adoption


class TestEventIdAfterIdentityAdoption:
    def test_new_item_reusing_the_old_uid_of_an_adopted_item_fails_forever_with_foreign_event(self):
        """DESIGN 4.3.2 + 4.: an item without a stable id that moved is adopted by content; the new state KEEPS the old
        `event_id` = make_event_id(old uid, gen) and the next patch rewrites `schulsync_uid` to the new uid. If the old uid
        then shows up again as a NEW item (the teacher assigns another 'Hausuebung' for the original date) its
        deterministic id is the id of that event: insert -> 409 -> `_adopt_event` sees a different uid -> BadRequestError
        'fremder Termin' on every run. The new homework never gets a calendar entry (permanent FAILED change)."""
        def hw(d):
            h = S.homework("x", title="Hausuebung", due=d, course="Mathe")
            return replace(h, source_id=f"hw:Hausuebung|{d.isoformat()}", id_stable=False)

        rig = Rig()
        first, moved_to = hw(date(2026, 10, 16)), hw(date(2026, 10, 19))
        rig.sync(first)
        rig.sync(moved_to)  # adopted by identity: the event of `first` now belongs to `moved_to`
        assert len(rig.google.events) == 1
        eng = rig.sync(first, moved_to)  # a second homework with the original title/date appears
        assert [c for c in eng.changes if c.error is not None] == [], [c.error for c in eng.changes if c.error]
        assert len(rig.google.events) == 2
        assert not rig.st(first).dirty
