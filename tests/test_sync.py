"""Engine tests: ingest (DESIGN 4.3), Google write protocol (DESIGN 6) and the rows of the test matrix (DESIGN 11)
that belong to the engine. Google is FakeGoogle, the schools are FakeAdapters, time is passed in: deterministic.

Synthetic data only; the fakes encode documented (and a few conservatively assumed) Google behaviours, see
tests/support/fake_google.py. Nothing here talks to a real service."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime

import pytest

from schulsync.dryrun import DryRunGateway
from schulsync.errors import (
    BadRequestError,
    ErrorInfo,
    NetworkError,
    NotFoundError,
    OutcomeUnknownError,
    TargetUnavailableError,
    TransientError,
)
from schulsync.google_types import make_event_id
from schulsync.models import (
    Action,
    Category,
    CategoryOutcome,
    CategoryStatus,
    ChangeStatus,
    EventState,
    ItemStatus,
    ItemTime,
    Notice,
    Severity,
    Target,
    TaskState,
)
from schulsync.state import StateStore
from schulsync.sync import SyncEngine
from schulsync.timeutil import VIENNA
from tests.support import samples as S
from tests.support.engine_harness import EDU, NOW, WU, Rig
from tests.support.fake_google import FakeGoogle

CAL, TSK = Target.CALENDAR, Target.TASKS


def eid(item, gen: int = 0) -> str:
    return make_event_id(item.uid, gen)


def changes(eng: SyncEngine, target=None, action=None, status=None):
    return [
        c
        for c in eng.changes
        if (target is None or c.target == target) and (action is None or c.action == action) and (status is None or c.status == status)
    ]


def notice_codes(eng: SyncEngine) -> list[str]:
    return [n.code for n in eng.notices]


def moved(item, *when):
    return replace(item, when=ItemTime(S.at(*when)))


def cancelled(item):
    return replace(item, status=ItemStatus.CANCELLED)


def task_of(rig: Rig, item):
    """The (single) non-deleted Google task carrying the marker of `item`."""
    found = [t for t in rig.google.tasks if f"schulsync-id: {item.uid}" in (t.get("notes") or "") and not t.get("deleted")]
    assert len(found) == 1, found
    return found[0]


# ====================================================================================== ingest (DESIGN 4.3)


class TestIngest:
    def test_new_item_is_stored_dirty_without_google_contact(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a, write=False)
        st = rig.st(a)
        assert st.dirty and st.event_state == EventState.NONE and st.task_state == TaskState.NONE and not st.cancelled
        assert st.first_seen_at == NOW and not st.id_uncertain
        assert rig.google.calls == {}

    @pytest.mark.parametrize(
        "item, reason",
        [
            (S.assignment(due=S.at(2026, 10, 13, 23, 59)), "expired"),
            (S.assignment(due=date(2026, 11, 4)), "outside_window"),
            (S.assignment(status=ItemStatus.CANCELLED), "already_cancelled"),
        ],
    )
    def test_skipped_new_items_are_counted_and_not_stored(self, item, reason):
        rig = Rig()
        eng = rig.sync(item)
        c = eng.counts_for(EDU)
        assert c.calendar.skipped == 1 and c.calendar.skipped_reasons == {reason: 1}
        assert c.tasks.skipped_reasons == {reason: 1}
        assert rig.store.all_items() == []
        assert rig.google.calls == {} and eng.changes == []

    def test_cancellation_kind_skip_counts_calendar_only(self):
        rig = Rig()
        eng = rig.sync(S.cancellation(day=date(2026, 10, 13)))  # yesterday
        c = eng.counts_for(WU)
        assert c.calendar.skipped_reasons == {"expired": 1} and c.tasks.skipped == 0

    @pytest.mark.parametrize(
        "due, stored",
        [
            (date(2026, 10, 13), False),  # yesterday
            (date(2026, 10, 14), True),  # today, all day
            (date(2026, 11, 3), True),  # last day inside the 21-day window
            (date(2026, 11, 4), False),  # window end is exclusive
            (S.at(2026, 10, 14, 7, 59), False),  # a minute ago
            (S.at(2026, 10, 14, 8, 0), True),  # exactly now is not past
            (S.at(2026, 11, 3, 23, 59), True),
            (S.at(2026, 11, 4, 0, 0), False),
        ],
    )
    def test_window_and_expiry_edges(self, due, stored):
        rig = Rig()
        a = S.assignment(due=due)
        rig.sync(a, write=False)
        assert (rig.st(a) is not None) is stored

    def test_known_item_outside_window_is_still_updated(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        far = moved(a, 2027, 1, 12, 23, 59)  # a shifted deadline far outside the window must not go stale
        rig.sync(far)
        assert rig.google.events[eid(a)]["start"]["dateTime"].startswith("2027-01-12T23:59")
        assert task_of(rig, a)["due"] == "2027-01-09T00:00:00.000Z"

    def test_unchanged_known_item_is_not_dirty_and_counts_unchanged(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.reset_counters()
        eng = rig.sync(a)
        assert not rig.st(a).dirty and rig.google.calls == {}  # nothing pending: no Google contact at all
        assert eng.counts_for(EDU).calendar.unchanged == 1 and eng.counts_for(EDU).tasks.unchanged == 1
        assert eng.changes == []

    def test_duplicate_within_one_fetch_is_skipped_with_notice(self):
        rig = Rig()
        a1, a2 = S.assignment(cmid="1"), S.assignment(cmid="2")  # same title, course and deadline
        eng = rig.sync(a1, a2)
        assert len(rig.store.all_items()) == 1 and "duplicate_in_source" in notice_codes(eng)
        assert len(rig.google.events) == 1 and len(rig.google.open_tasks) == 1

    def test_known_uid_wins_over_a_new_duplicate(self):
        rig = Rig()
        a1, a2 = S.assignment(cmid="1"), S.assignment(cmid="2")
        rig.sync(a2)
        eng = rig.sync(a1, a2)  # the new a1 is listed first but a2 is the one we know
        assert [s.uid for s in rig.store.all_items()] == [a2.uid]
        assert "duplicate_in_source" in notice_codes(eng) and len(rig.google.events) == 1

    def test_same_uid_listed_twice_is_harmless(self):
        rig = Rig()
        a = S.assignment()
        eng = rig.sync(a, a)
        assert notice_codes(eng) == [] and len(rig.google.events) == 1

    def test_stable_ids_are_never_merged_by_title_or_date(self):
        rig = Rig()
        a1 = S.assignment(cmid="1", title="Protokoll")
        rig.sync(a1)
        a2 = S.assignment(cmid="2", title="Protokoll", due=S.at(2026, 10, 28, 23, 59))  # a1 vanished from the list
        eng = rig.sync(a2)
        assert len(rig.store.all_items()) == 2 and len(rig.google.events) == 2
        assert not rig.st(a1).cancelled and "identity_uncertain" not in notice_codes(eng)  # absence is no cancellation

    @staticmethod
    def _unstable(sid: str, *when):
        return replace(S.assignment(cmid="x"), source_id=sid, id_stable=False, when=ItemTime(S.at(*when)))

    def test_unstable_id_is_matched_conservatively_and_flagged(self):
        rig = Rig()
        u1 = self._unstable("t:1", 2026, 10, 21, 23, 59)
        rig.sync(u1)
        old_event, old_task = rig.st(u1).event_id, rig.st(u1).task_id
        u2 = self._unstable("t:2", 2026, 10, 28, 23, 59)  # moved: the id derived from the content changed
        eng = rig.sync(u2)
        assert [s.uid for s in rig.store.all_items()] == [u2.uid]
        st = rig.st(u2)
        assert st.id_uncertain and st.event_id == old_event and st.task_id == old_task and not st.dirty
        assert len(rig.google.events) == 1 and len(rig.google.writes("insert_event")) == 1
        assert rig.google.events[old_event]["start"]["dateTime"].startswith("2026-10-28T23:59")
        assert f"schulsync-id: {u2.uid}" in rig.google.task(old_task)["notes"]
        assert rig.google.task(old_task)["due"] == "2026-10-25T00:00:00.000Z"
        assert "identity_uncertain" in notice_codes(eng)

    def test_unstable_id_with_two_candidates_is_new_plus_warning(self):
        rig = Rig()
        x1, x2 = self._unstable("t:1", 2026, 10, 20, 23, 59), self._unstable("t:2", 2026, 10, 21, 23, 59)
        rig.sync(x1, x2)
        y1, y2 = self._unstable("t:3", 2026, 10, 27, 23, 59), self._unstable("t:4", 2026, 10, 28, 23, 59)
        eng = rig.sync(y1, y2)
        assert len(rig.store.all_items()) == 4 and len(rig.google.events) == 4  # nothing guessed, nothing merged
        assert "identity_uncertain" in notice_codes(eng)
        assert all(not rig.st(u).id_uncertain for u in (y1, y2))

    def test_unstable_id_waits_for_a_complete_fetch(self):
        rig = Rig()
        u1 = self._unstable("t:1", 2026, 10, 21, 23, 59)
        rig.sync(u1)
        u2 = self._unstable("t:2", 2026, 10, 28, 23, 59)
        fr = rig.fetch(EDU, [u2])
        fr.categories[Category.ASSIGNMENTS] = CategoryOutcome(
            Category.ASSIGNMENTS, CategoryStatus.INCOMPLETE, ErrorInfo("incomplete", "Teilantwort")
        )
        eng = rig.engine()
        eng.ingest(fr, NOW)
        assert rig.st(u2) is None and rig.st(u1) is not None  # no guess on a partial list
        assert "identity_uncertain" in notice_codes(eng)

    def test_withdrawn_proof_cancels_and_is_reported_once(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        fr = rig.fetch(EDU, [])  # the withdrawn item is no longer listed
        fr.withdrawn = {a.uid: "Die Lehrkraft hat die Aufgabe zurückgenommen."}
        eng = rig.engine()
        eng.ingest(fr, NOW)
        eng.write_phase([EDU], NOW)
        assert rig.st(a).cancelled and "withdrawn_confirmed" in notice_codes(eng)
        assert rig.google.events[eid(a)]["summary"].startswith("Abgesagt: ")
        eng2 = rig.engine()
        eng2.ingest(fr, NOW)  # the proof is still delivered: nothing new
        assert notice_codes(eng2) == [] and not rig.st(a).dirty

    def test_absence_never_cancels_complete_empty_or_incomplete(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        n = len(rig.google.write_log)
        rig.sync()  # complete, empty
        rig.adapters[EDU].fail_category(Category.ASSIGNMENTS, ErrorInfo("incomplete", "x"), CategoryStatus.INCOMPLETE)
        rig.sync()  # incomplete (no items delivered)
        assert len(rig.google.write_log) == n and not rig.st(a).cancelled and not rig.st(a).dirty

    def test_reactivation_needs_a_complete_category(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.sync(cancelled(a))
        assert rig.st(a).cancelled
        fr = rig.fetch(EDU, [a])
        fr.categories[Category.ASSIGNMENTS] = CategoryOutcome(Category.ASSIGNMENTS, CategoryStatus.INCOMPLETE)
        rig.engine().ingest(fr, NOW)
        assert rig.st(a).cancelled  # partial list: not a confirmation
        rig.engine().ingest(rig.fetch(EDU, [a]), NOW)
        assert not rig.st(a).cancelled and rig.st(a).dirty

    def test_adapter_notices_are_forwarded(self):
        rig = Rig()
        n = Notice(Severity.WARNING, "no_date", "Eintrag ohne Datum", EDU, title="Ohne Datum")
        rig.adapters[EDU].set_notices([n])
        eng = rig.sync()
        assert n in eng.notices


# ====================================================================================== creating and idempotence


class TestCreate:
    def test_assignment_creates_event_and_task(self):
        rig = Rig()
        a = S.assignment()
        eng = rig.sync(a)
        ev = rig.google.events[eid(a)]
        assert ev["summary"] == "Abgabe: Laborprotokoll 2: Operationsverstärker"
        assert ev["start"] == {"dateTime": "2026-10-21T23:59:00+02:00", "timeZone": "Europe/Vienna"}
        assert ev["end"]["dateTime"] == "2026-10-22T00:00:00+02:00"  # one-minute marker, not a longer deadline
        assert ev["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1440}]}
        assert ev["extendedProperties"]["private"]["schulsync_uid"] == a.uid
        assert "Frist: Mi., 21.10.2026 23:59" in ev["description"] and "Persönliche Notizen" in ev["description"]
        task = task_of(rig, a)
        assert task["title"] == "Bearbeiten: Laborprotokoll 2: Operationsverstärker"
        assert task["due"] == "2026-10-18T00:00:00.000Z"  # three CALENDAR days before the deadline
        st = rig.st(a)
        assert (st.event_state, st.task_state, st.dirty) == (EventState.ACTIVE, TaskState.OPEN, False)
        assert st.event_id == eid(a) and st.task_id == task["id"] and st.task_due == date(2026, 10, 18)
        assert st.task_base_deadline == "2026-10-21T23:59:00+02:00" and st.task_applied_hash and st.event_applied_hash
        assert [(c.target, c.action, c.status) for c in eng.changes] == [
            (CAL, Action.CREATE, ChangeStatus.APPLIED),
            (TSK, Action.CREATE, ChangeStatus.APPLIED),
        ]
        c = eng.counts_for(EDU)
        assert c.calendar.new == 1 and c.tasks.new == 1
        assert eng.calendar_target.ok and eng.tasks_target.ok and eng.calendar_target.name == "Schule"

    def test_exam_cancellation_and_homework_variants(self):
        rig = Rig()
        h = S.homework(due=date(2026, 10, 20))
        x = S.exam()
        c = S.cancellation()
        rig.sync(h, x, c)
        evh = rig.google.events[eid(h)]
        assert evh["start"] == {"date": "2026-10-20"} and evh["end"] == {"date": "2026-10-21"}  # all day, exclusive end
        assert task_of(rig, h)["due"] == "2026-10-17T00:00:00.000Z"
        evx = rig.google.events[eid(x)]
        assert evx["summary"] == "Schularbeit: Schularbeit Angewandte Mathematik"
        assert evx["start"]["dateTime"] == "2026-10-28T08:00:00+01:00" and evx["end"]["dateTime"] == "2026-10-28T09:40:00+01:00"
        tx = task_of(rig, x)
        assert tx["title"] == "Lernen: Schularbeit Angewandte Mathematik" and tx["due"] == "2026-10-21T00:00:00.000Z"
        assert rig.google.events[eid(c)]["summary"] == "Entfall: Betriebstechnik"
        assert len(rig.google.tasks) == 2  # a cancelled lesson has no task
        assert all(e["reminders"]["overrides"] == [{"method": "popup", "minutes": 1440}] for e in rig.google.events.values())

    def test_late_discovery_sets_today_once_and_overdue_task_is_never_moved(self):
        rig = Rig()
        h = S.homework(due=date(2026, 10, 16))  # work start 13.10. is already behind us
        rig.sync(h)
        assert task_of(rig, h)["due"] == "2026-10-14T00:00:00.000Z"
        rig.google.reset_write_log()
        eng = rig.sync(h, now=S.at(2026, 10, 15, 8, 0))  # a day later, unchanged deadline
        assert rig.google.write_log == [] and eng.changes == []
        assert task_of(rig, h)["due"] == "2026-10-14T00:00:00.000Z"

    def test_spring_dst_lead_is_counted_in_calendar_days(self):
        rig = Rig(now=S.at(2027, 3, 15, 8, 0))
        x = S.exam(start=S.at(2027, 3, 29, 0, 30), end=S.at(2027, 3, 29, 1, 15))  # DST began on 28.03.2027
        rig.sync(x)
        assert task_of(rig, x)["due"] == "2027-03-22T00:00:00.000Z"
        assert rig.google.events[eid(x)]["start"]["dateTime"] == "2027-03-29T00:30:00+02:00"

    def test_autumn_overlap_deadline_keeps_its_real_offset_and_expiry_compares_instants(self):
        rig = Rig()
        second = datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=1)  # 02:30 CET = 01:30 UTC
        first = datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=0)  # 02:30 CEST = 00:30 UTC
        a = S.assignment(cmid="late", due=second)
        b = S.assignment(cmid="early", due=first, title="Anderes Protokoll")
        now = datetime(2026, 10, 25, 2, 10, tzinfo=VIENNA, fold=1)  # 01:10 UTC: `first` is over, `second` is not
        rig.sync(a, b, now=now)
        assert rig.st(b) is None and rig.st(a) is not None
        ev = rig.google.events[eid(a)]
        assert ev["start"]["dateTime"] == "2026-10-25T02:30:00+01:00" and ev["end"]["dateTime"] == "2026-10-25T02:31:00+01:00"

    def test_autumn_overlap_deadlines_are_stable_across_runs(self):
        rig = Rig(now=S.at(2026, 10, 20, 8, 0))
        second = S.assignment(cmid="s", due=datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=1), title="Zweite")
        first = S.assignment(cmid="f", due=datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=0), title="Erste")
        rig.sync(first, second)
        assert rig.google.events[eid(first)]["start"]["dateTime"] == "2026-10-25T02:30:00+02:00"
        assert rig.google.events[eid(second)]["start"]["dateTime"] == "2026-10-25T02:30:00+01:00"
        n = len(rig.google.write_log)
        for _ in range(2):  # the stored state must round-trip the offset: no phantom change, no phantom deadline shift
            eng = rig.sync(first, second)
            assert eng.changes == [] and len(rig.google.write_log) == n

    def test_twice_the_same_data_creates_no_duplicates_and_no_extra_writes(self):
        rig = Rig()
        items = [S.assignment(), S.homework(due=date(2026, 10, 20)), S.exam(), S.cancellation()]
        rig.sync(*items)
        n = len(rig.google.write_log)
        eng = rig.sync(*items)
        eng3 = rig.sync(*items)
        assert len(rig.google.write_log) == n and eng.changes == [] and eng3.changes == []
        assert len(rig.google.events) == 4 and len(rig.google.tasks) == 3
        assert eng.counts_for(EDU).calendar.unchanged == 1 and eng.counts_for(WU).calendar.unchanged == 3
        assert eng.counts_for(WU).tasks.unchanged == 2

    def test_google_is_contacted_only_when_something_is_pending_and_the_list_is_read_once(self):
        rig = Rig()
        items = [S.assignment(cmid="1"), S.assignment(cmid="2", title="Zweites Protokoll"), S.exam()]
        rig.sync(*items)
        assert rig.google.calls["check_targets"] == 1 and rig.google.calls["list_tasks"] == 1


# ====================================================================================== changes, cancellation, reactivation


class TestChanges:
    def test_deadline_shift_updates_event_and_open_task_and_keeps_personal_notes(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        e, t = eid(a), task_of(rig, a)
        rig.google.user_edit_event_description(e, rig.google.events[e]["description"] + "Mein Hinweis: Taschenrechner")
        rig.google.user_edit_task_notes(t["id"], t["notes"] + "\nMeine Task-Notiz")
        later = moved(a, 2026, 10, 28, 23, 59)
        eng = rig.sync(later)
        ev = rig.google.events[e]
        assert ev["start"]["dateTime"].startswith("2026-10-28T23:59") and len(rig.google.events) == 1
        assert "Mein Hinweis: Taschenrechner" in ev["description"] and "28.10.2026" in ev["description"]
        t2 = rig.google.task(t["id"])
        assert t2["due"] == "2026-10-25T00:00:00.000Z" and "Meine Task-Notiz" in t2["notes"]
        assert t2["notes"].count(f"schulsync-id: {a.uid}") == 1 and "28.10.2026" in t2["notes"]
        assert [(c.target, c.action) for c in eng.changes] == [(CAL, Action.UPDATE), (TSK, Action.UPDATE)]
        st = rig.st(a)
        assert st.task_due == date(2026, 10, 25) and st.task_base_deadline == "2026-10-28T23:59:00+01:00" and not st.dirty
        assert eng.counts_for(EDU).calendar.updated == 1 and eng.counts_for(EDU).tasks.updated == 1

    def test_title_only_change_keeps_the_task_date(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.user_move_task_due(task_of(rig, a)["id"], date(2026, 10, 19))  # the user planned differently
        rig.sync(replace(a, title="Laborprotokoll 2 (korrigiert)"))
        t = task_of(rig, a)
        assert t["title"] == "Bearbeiten: Laborprotokoll 2 (korrigiert)" and t["due"] == "2026-10-19T00:00:00.000Z"

    def test_cancellation_marks_event_and_removes_only_the_open_task(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        tid = task_of(rig, a)["id"]
        eng = rig.sync(cancelled(a))
        ev = rig.google.events[eid(a)]
        assert ev["summary"] == "Abgesagt: Abgabe: Laborprotokoll 2: Operationsverstärker"
        assert ev["reminders"] == {"useDefault": False, "overrides": []}
        assert rig.google.open_tasks == [] and rig.google.task(tid).get("deleted")
        st = rig.st(a)
        assert (st.task_state, st.task_removed_reason, st.task_id, st.applied_cancelled) == (TaskState.REMOVED, "cancelled", None, True)
        assert [(c.target, c.action) for c in eng.changes] == [(CAL, Action.CANCEL), (TSK, Action.REMOVE_TASK)]
        assert eng.counts_for(EDU).calendar.cancelled == 1 and eng.counts_for(EDU).tasks.cancelled == 1

    def test_cancellation_keeps_a_done_task(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        tid = task_of(rig, a)["id"]
        rig.google.user_complete_task(tid)
        eng = rig.sync(cancelled(a))
        assert rig.google.writes("delete_task") == [] and rig.google.task(tid)["status"] == "completed"
        assert rig.st(a).task_state == TaskState.DONE and rig.google.events[eid(a)]["summary"].startswith("Abgesagt: ")
        assert changes(eng, TSK) == [] and eng.counts_for(EDU).tasks.skipped_reasons == {"done_untouched": 1}

    def test_reactivation_reuses_the_event_and_recreates_the_removed_task(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.sync(cancelled(a))
        eng = rig.sync(a)
        assert len(rig.google.writes("insert_event")) == 1 and len(rig.google.events) == 1  # the same event
        ev = rig.google.events[eid(a)]
        assert ev["summary"] == "Abgabe: Laborprotokoll 2: Operationsverstärker"
        assert ev["reminders"]["overrides"] == [{"method": "popup", "minutes": 1440}]
        assert len(rig.google.open_tasks) == 1 and len(rig.google.tasks) == 2  # old one stays deleted
        assert [(c.target, c.action) for c in eng.changes] == [(CAL, Action.REACTIVATE), (TSK, Action.REACTIVATE)]
        assert eng.counts_for(EDU).calendar.reactivated == 1 and eng.counts_for(EDU).tasks.reactivated == 1
        assert rig.st(a).task_state == TaskState.OPEN and rig.st(a).task_id == task_of(rig, a)["id"]

    def test_reactivation_respects_done_and_user_deleted_tasks(self):
        rig = Rig()
        done, gone = S.assignment(cmid="1"), S.assignment(cmid="2", title="Anderes")
        rig.sync(done, gone)
        rig.google.user_complete_task(task_of(rig, done)["id"])
        rig.google.user_delete_task(task_of(rig, gone)["id"])
        rig.sync(cancelled(done), cancelled(gone))
        assert rig.st(done).task_state == TaskState.DONE and rig.st(gone).task_state == TaskState.USER_DELETED
        n = len(rig.google.writes("insert_task"))
        rig.sync(done, gone)
        assert len(rig.google.writes("insert_task")) == n and rig.google.open_tasks == []
        assert len(rig.google.writes("insert_event")) == 2  # events were reused
        assert all(not e["summary"].startswith("Abgesagt") for e in rig.google.events.values())

    def test_done_or_hidden_task_is_never_touched_by_a_change(self):
        for hide in (False, True):
            rig = Rig()
            a = S.assignment()
            rig.sync(a)
            tid = task_of(rig, a)["id"]
            rig.google.user_complete_task(tid)
            if hide:
                rig.google.user_clear_completed()
            rig.sync(moved(a, 2026, 10, 28, 23, 59))
            assert rig.google.writes("patch_task") == [] and rig.st(a).task_state == TaskState.DONE
            assert rig.google.events[eid(a)]["start"]["dateTime"].startswith("2026-10-28")

    def test_cancellation_never_deletes_a_task_the_user_completed_after_the_list_was_read(self):
        """The list is read once per run; the delete must be preceded by a fresh read (requirement 6: completed
        tasks are kept, the user ticks them off on their own at any moment)."""
        rig = Rig()
        rig.google = StaleTaskList()
        a = S.assignment()
        rig.sync(a)
        tid = task_of(rig, a)["id"]
        rig.google.snapshot = copy.deepcopy(rig.google.tasks)  # what the engine will read: still open
        rig.google.user_complete_task(tid)  # ... but the user ticked it off meanwhile
        eng = rig.sync(cancelled(a))
        assert rig.google.writes("delete_task") == []
        assert rig.st(a).task_state == TaskState.DONE and rig.google.task(tid).get("status") == "completed"
        assert changes(eng, TSK) == [] and not rig.st(a).dirty
        assert eng.counts_for(EDU).tasks.skipped_reasons == {"done_untouched": 1}

    def test_cancellation_notices_a_task_the_user_deleted_after_the_list_was_read(self):
        rig = Rig()
        rig.google = StaleTaskList()
        a = S.assignment()
        rig.sync(a)
        tid = task_of(rig, a)["id"]
        rig.google.snapshot = copy.deepcopy(rig.google.tasks)
        rig.google.user_delete_task(tid)
        eng = rig.sync(cancelled(a))
        assert rig.google.writes("delete_task") == []
        assert rig.st(a).task_state == TaskState.USER_DELETED and "user_deleted_task" in notice_codes(eng)
        assert rig.store.open_intents() == []

    @pytest.mark.parametrize("none_variant", [False, True])
    def test_user_deleted_event_is_not_recreated_and_restore_recreates_it(self, none_variant):
        rig = Rig(deleted_event_get_returns_none=none_variant)
        a = S.assignment()
        rig.sync(a)
        rig.google.user_delete_event(eid(a))
        eng = rig.sync(moved(a, 2026, 10, 28, 23, 59))
        assert rig.st(a).event_state == EventState.USER_DELETED
        assert "user_deleted_event" in notice_codes(eng) and eng.counts_for(EDU).calendar.skipped_reasons == {"user_deleted": 1}
        assert len(rig.google.writes("insert_event")) == 1 and rig.google.writes("patch_event") == [] and rig.google.events == {}
        rig.sync(moved(a, 2026, 10, 29, 23, 59))  # still nothing, run after run
        assert rig.google.events == {} and len(rig.google.writes("insert_event")) == 1
        eng3 = rig.engine()
        res = eng3.restore(None, NOW)
        eng3.write_phase([EDU], NOW)
        assert res.restored == [a.uid] and rig.st(a).event_gen == 1 and rig.st(a).event_state == EventState.ACTIVE
        assert list(rig.google.events) == [eid(a, 1)] and changes(eng3, CAL)[0].action == Action.RESTORE
        assert rig.google.events[eid(a, 1)]["start"]["dateTime"].startswith("2026-10-29T23:59")

    def test_event_deleted_between_read_and_patch_is_user_deleted(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.fail_next("patch_event", NotFoundError)
        eng = rig.sync(moved(a, 2026, 10, 28, 23, 59))
        assert rig.st(a).event_state == EventState.USER_DELETED and "user_deleted_event" in notice_codes(eng)
        assert changes(eng, CAL) == [] and rig.st(a).task_due == date(2026, 10, 25)  # the task part went on

    @pytest.mark.parametrize("purge", [False, True])
    def test_user_deleted_task_is_not_recreated_and_restore_recreates_it(self, purge):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        old = task_of(rig, a)["id"]
        rig.google.user_delete_task(old, purge=purge)
        eng = rig.sync(moved(a, 2026, 10, 28, 23, 59))
        assert rig.st(a).task_state == TaskState.USER_DELETED and "user_deleted_task" in notice_codes(eng)
        assert len(rig.google.writes("insert_task")) == 1 and rig.google.writes("patch_task") == []
        rig.sync(moved(a, 2026, 10, 29, 23, 59))
        assert len(rig.google.writes("insert_task")) == 1
        eng3 = rig.engine()
        res = eng3.restore([a.uid], NOW)
        eng3.write_phase([EDU], NOW)
        assert res.restored == [a.uid] and rig.st(a).task_state == TaskState.OPEN and rig.st(a).task_id != old
        assert task_of(rig, a)["due"] == "2026-10-26T00:00:00.000Z" and changes(eng3, TSK)[0].action == Action.RESTORE

    def test_restore_reports_unknown_uids_and_never_touches_done_tasks(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.user_complete_task(task_of(rig, a)["id"])
        rig.google.user_delete_event(eid(a))
        rig.sync(moved(a, 2026, 10, 28, 23, 59))
        eng = rig.engine()
        res = eng.restore([a.uid, "eduvidual:assignment:assign:nope"], NOW)
        eng.write_phase([EDU], NOW)
        assert res.restored == [a.uid] and res.not_found == ["eduvidual:assignment:assign:nope"] and res.sources == {EDU}
        assert rig.st(a).task_state == TaskState.DONE and len(rig.google.tasks) == 1  # done stays done
        assert rig.engine().restore(None, NOW).restored == []  # nothing left to restore

    def test_foreign_event_with_our_id_is_never_modified(self):
        rig = Rig()
        a = S.assignment()
        rig.google.insert_event({"id": eid(a), "summary": "Fremder Termin", "start": {"date": "2026-10-21"}, "end": {"date": "2026-10-22"}})
        rig.google.reset_write_log()
        eng = rig.sync(a)
        bad = changes(eng, CAL)
        assert len(bad) == 1 and bad[0].status == ChangeStatus.FAILED and bad[0].error.code == "request_rejected"
        assert rig.google.writes("patch_event") == [] and rig.google.events[eid(a)]["summary"] == "Fremder Termin"
        assert rig.store.open_intents() == [] and rig.st(a).dirty


# ====================================================================================== targets, partial failures


class TestTargetsAndPartialFailures:
    @pytest.mark.parametrize(
        "kw, down, up",
        [
            (dict(calendar_exists=False), CAL, TSK),
            (dict(calendar_is_primary=True), CAL, TSK),
            (dict(tasklist_exists=False), TSK, CAL),
        ],
        ids=["missing", "primary", "no-list"],
    )
    def test_unavailable_target_defers_only_its_own_writes_without_fallback(self, kw, down, up):
        """ANFORDERUNGEN 4: "die betroffenen Schreibvorgänge zurückstellen". Until the audit this test pinned that an
        unavailable target deferred EVERYTHING (ACCEPTANCE_NOTES A4); the requirement says only the affected writes, so the
        other, reachable target keeps being written. Never a fallback, and the main calendar is never written."""
        rig = Rig(**kw)
        a, h = S.assignment(), S.homework(due=date(2026, 10, 20))
        eng = rig.sync(a, h)
        assert rig.google.misrouted_writes() == [] and rig.google.primary_events == {}
        assert len(eng.changes) == 4
        assert {c.status for c in changes(eng, down)} == {ChangeStatus.DEFERRED} and len(changes(eng, down)) == 2
        assert {c.status for c in changes(eng, up)} == {ChangeStatus.APPLIED} and len(changes(eng, up)) == 2
        assert all(c.error.code == "target_unavailable" for c in changes(eng, down))
        status = {CAL: eng.calendar_target, TSK: eng.tasks_target}
        assert status[down].ok is False and status[down].error.code == "target_unavailable" and status[up].ok is True
        assert {w[0] for w in rig.google.write_log} == ({"insert_task"} if down == CAL else {"insert_event"})
        assert rig.st(a).dirty and rig.st(h).dirty
        assert eng.counts_for(EDU).of(down).failed == 1 and eng.counts_for(WU).of(down).failed == 1
        assert eng.counts_for(EDU).of(up).failed == 0 and eng.counts_for(EDU).of(up).new == 1
        assert rig.google.calls["check_targets"] == 1  # checked ONCE
        rig.google.calendar_exists, rig.google.calendar_is_primary, rig.google.tasklist_exists = True, False, True
        eng2 = rig.write_again()  # the target is back: the missing part is written exactly once, nothing twice
        assert len(rig.google.events) == 2 and len(rig.google.open_tasks) == 2 and len(rig.google.tasks) == 2
        assert not rig.st(a).dirty and all(c.status == ChangeStatus.APPLIED for c in eng2.changes) and len(eng2.changes) == 2
        assert {c.target for c in eng2.changes} == {down}

    def test_a_target_check_that_names_no_target_defers_everything(self):
        """A gateway that cannot say WHICH target failed (a plain TargetUnavailableError, as the contract allows) keeps the
        conservative behaviour: every write waits."""
        rig = Rig()
        rig.google.fail_next("check_targets", TargetUnavailableError)
        a, h = S.assignment(), S.homework(due=date(2026, 10, 20))
        eng = rig.sync(a, h)
        assert rig.google.write_log == [] and len(eng.changes) == 4 and {c.status for c in eng.changes} == {ChangeStatus.DEFERRED}
        assert eng.calendar_target.ok is False and eng.tasks_target.ok is False

    def test_a_calendar_that_was_not_verified_is_not_written_but_the_task_list_is_read_for_itself(self):
        """If the check stops at the calendar the task list was never looked at: its own read is its check, and a missing
        list shows up there (so the tasks are deferred as well, with the list's own error)."""
        rig = Rig(calendar_exists=False, tasklist_exists=False)
        eng = rig.sync(S.assignment())
        assert rig.google.write_log == [] and {c.status for c in eng.changes} == {ChangeStatus.DEFERRED}
        assert eng.calendar_target.ok is False and eng.tasks_target.ok is False
        assert "Aufgabenliste" in eng.tasks_target.error.message and "Kalender" in eng.calendar_target.error.message

    def test_open_calendar_intents_are_left_alone_while_the_calendar_is_down(self):
        """An unclear event create is reconciled by reading the event; with the calendar down that read would say "not
        there" and abort the intent. It must stay open until the calendar answers again."""
        rig = Rig()
        a = S.assignment()
        rig.google.fail_next("insert_event", OutcomeUnknownError, applied=True)
        rig.sync(a)
        assert len(rig.store.open_intents()) == 1 and len(rig.google.events) == 1
        rig.google.calendar_exists = False
        rig.write_again()
        assert len(rig.store.open_intents()) == 1
        rig.google.calendar_exists = True
        rig.write_again()
        assert rig.store.open_intents() == [] and len(rig.google.events) == 1 and not rig.st(a).dirty

    def test_target_check_uses_the_injected_retry(self):
        rig = Rig()
        calls = []

        def retry(fn):
            for attempt in range(3):
                calls.append(attempt)
                try:
                    return fn()
                except TransientError:
                    if attempt == 2:
                        raise

        rig.google.fail_next("check_targets", NetworkError, times=2)
        rig.sync(S.assignment(), engine=rig.engine(retry=retry))
        assert calls[:3] == [0, 1, 2] and len(rig.google.events) == 1  # the same retry also wraps the task-list read

    def test_task_failure_does_not_stop_the_calendar_and_is_retried_next_run(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.fail_always("patch_task", NetworkError)
        eng = rig.sync(moved(a, 2026, 10, 28, 23, 59))
        assert rig.google.events[eid(a)]["start"]["dateTime"].startswith("2026-10-28")
        bad = changes(eng, TSK)
        assert len(bad) == 1 and bad[0].status == ChangeStatus.FAILED and bad[0].error.code == "network"
        assert eng.counts_for(EDU).tasks.failed == 1 and eng.counts_for(EDU).calendar.updated == 1 and rig.st(a).dirty
        rig.google.clear_faults()
        eng2 = rig.write_again()
        assert rig.google.task(task_of(rig, a)["id"])["due"] == "2026-10-25T00:00:00.000Z" and not rig.st(a).dirty
        assert len(rig.google.writes("patch_event")) == 1 and changes(eng2, CAL) == []  # the event is not touched again

    def test_one_failing_item_does_not_stop_the_others(self):
        rig = Rig()
        a1, a2 = S.assignment(cmid="1", title="Eins"), S.assignment(cmid="2", title="Zwei")
        rig.google.fail_next("insert_task", BadRequestError)
        eng = rig.sync(a1, a2)
        assert len(rig.google.events) == 2 and len(rig.google.open_tasks) == 1
        bad = changes(eng, TSK, status=ChangeStatus.FAILED)
        assert len(bad) == 1 and bad[0].uid == a1.uid and bad[0].error.code == "request_rejected"
        assert rig.store.open_intents() == [] and rig.st(a1).dirty and rig.st(a1).task_state == TaskState.NONE  # aborted, retried later
        rig.write_again()
        assert len(rig.google.open_tasks) == 2 and not rig.st(a1).dirty

    def test_should_stop_defers_the_rest_with_a_timeout_error(self):
        rig = Rig()
        eng = rig.engine(should_stop=lambda: True)
        rig.sync(S.assignment(), engine=eng)
        assert rig.google.write_log == [] and all(c.status == ChangeStatus.DEFERRED for c in eng.changes)
        assert {c.error.code for c in eng.changes} == {"run_timeout"} and rig.st(S.assignment()).dirty

    def test_unreadable_task_list_defers_only_the_tasks(self):
        rig = Rig()
        rig.google.fail_always("list_tasks", NetworkError)
        a = S.assignment()
        eng = rig.sync(a)
        assert len(rig.google.events) == 1 and rig.google.tasks == []
        assert [c.status for c in changes(eng, TSK)] == [ChangeStatus.DEFERRED] and eng.tasks_target.ok is False
        assert eng.calendar_target.ok is True and rig.st(a).dirty
        rig.google.clear_faults()
        rig.write_again()
        assert len(rig.google.open_tasks) == 1 and not rig.st(a).dirty


# ====================================================================================== lost answers, crashes, reconciliation


class StaleTaskList(FakeGoogle):
    """list_tasks answers from `snapshot` once it is set: the list was read earlier than the user's last click."""

    snapshot = None

    def list_tasks(self):
        if self.snapshot is not None:
            self.calls["list_tasks"] += 1
            return copy.deepcopy(self.snapshot)
        return super().list_tasks()


class ListFailsOnce(FakeGoogle):
    """list_tasks fails on its second call (the reconciliation after an unclear insert) or while `block` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0
        self.block = False

    def list_tasks(self):
        self.list_calls += 1
        if self.list_calls == 2 or self.block:
            raise NetworkError("Kein Netz (Test).")
        return super().list_tasks()


class TestReconciliation:
    @pytest.mark.parametrize("exc", [OutcomeUnknownError, NetworkError, TransientError])
    def test_event_insert_with_lost_response_is_adopted_next_run(self, exc):
        rig = Rig()
        a = S.assignment()
        rig.google.fail_next("insert_event", exc, applied=True)
        eng = rig.sync(a)
        assert [c.status for c in changes(eng, CAL)] == [ChangeStatus.UNCERTAIN] and len(rig.google.events) == 1
        assert rig.st(a).event_state == EventState.NONE and rig.st(a).dirty and len(rig.store.open_intents()) == 1
        eng2 = rig.write_again()
        assert len(rig.google.writes("insert_event")) == 1 and len(rig.google.events) == 1  # no duplicate
        assert rig.st(a).event_state == EventState.ACTIVE and rig.store.open_intents() == [] and not rig.st(a).dirty
        assert changes(eng2, CAL)[0].action == Action.ADOPT and len(rig.google.open_tasks) == 1

    def test_event_insert_that_did_not_happen_is_created_next_run(self):
        rig = Rig()
        a = S.assignment()
        rig.google.fail_next("insert_event", OutcomeUnknownError, applied=False)
        rig.sync(a)
        assert rig.google.events == {}
        rig.write_again()
        assert len(rig.google.events) == 1 and len(rig.google.writes("insert_event")) == 1 and rig.store.open_intents() == []

    def test_existing_own_event_is_adopted_after_a_lost_database_without_duplicates(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.store = StateStore(":memory:")  # all local state is gone
        eng = rig.sync(a)
        assert len(rig.google.writes("insert_event")) == 1 and len(rig.google.writes("insert_task")) == 1
        assert len(rig.google.events) == 1 and len(rig.google.open_tasks) == 1
        assert [(c.target, c.action) for c in eng.changes] == [(CAL, Action.ADOPT), (TSK, Action.ADOPT)]
        st = rig.st(a)
        assert (st.event_state, st.task_state, st.dirty) == (EventState.ACTIVE, TaskState.OPEN, False)
        assert task_of(rig, a)["due"] == "2026-10-18T00:00:00.000Z" and len(rig.google.writes("patch_task")) == 1

    @pytest.mark.parametrize("none_variant", [False, True])
    def test_lost_database_and_an_event_the_user_deleted_stays_deleted(self, none_variant):
        """The 409 on insert proves the id is taken. A deleted event reads back as a tombstone OR as 404/410 (None):
        both mean 'deleted by the user' (DESIGN 6), never a permanent failure and never a re-creation."""
        rig = Rig(deleted_event_get_returns_none=none_variant)
        a = S.assignment()
        rig.sync(a)
        rig.google.user_delete_event(eid(a))
        rig.store = StateStore(":memory:")  # all local state is gone
        eng = rig.sync(a)
        assert rig.st(a).event_state == EventState.USER_DELETED and "user_deleted_event" in notice_codes(eng)
        assert changes(eng, CAL) == [] and eng.counts_for(EDU).calendar.failed == 0
        assert rig.google.events == {} and rig.store.open_intents() == []
        assert not rig.st(a).dirty  # settled: the next run does not try again
        rig.google.reset_write_log()
        rig.sync(a)
        assert rig.google.write_log == []

    def test_orphan_task_that_is_done_or_deleted(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.user_complete_task(task_of(rig, a)["id"])
        rig.store = StateStore(":memory:")
        rig.sync(a)
        assert rig.st(a).task_state == TaskState.DONE and rig.google.writes("patch_task") == [] and len(rig.google.tasks) == 1
        rig2 = Rig()
        rig2.sync(a)
        rig2.sync(cancelled(a))  # the task is deleted by us
        rig2.store = StateStore(":memory:")
        rig2.sync(a)  # a deleted task is never adopted; it carries OUR removal trace, so it is not the user's deletion: a new one is created
        assert len(rig2.google.tasks) == 2 and len(rig2.google.open_tasks) == 1

    def test_duplicate_markers_adopt_the_first_live_one(self):
        rig = Rig()
        a = S.assignment()
        for _ in range(2):
            rig.google.insert_task({"title": "alt", "notes": f"x\nschulsync-id: {a.uid}"})
        first = rig.google.tasks[0]["id"]
        eng = rig.sync(a)
        assert "duplicate_task_marker" in notice_codes(eng) and rig.st(a).task_id == first
        assert len(rig.google.writes("insert_task")) == 2 and len(rig.google.tasks) == 2  # no third one

    @pytest.mark.parametrize("exc", [OutcomeUnknownError, NetworkError, TransientError])
    def test_task_insert_with_lost_response_is_reconciled_in_the_same_run(self, exc):
        rig = Rig()
        a = S.assignment()
        rig.google.fail_next("insert_task", exc, applied=True)
        eng = rig.sync(a)
        assert len(rig.google.tasks) == 1 and rig.store.open_intents() == [] and rig.google.calls["list_tasks"] == 2
        assert changes(eng, TSK)[0].action == Action.ADOPT and changes(eng, TSK)[0].status == ChangeStatus.APPLIED
        st = rig.st(a)
        assert st.task_state == TaskState.OPEN and st.task_id == rig.google.tasks[0]["id"] and not st.dirty
        assert st.task_due == date(2026, 10, 18) and st.task_base_deadline == "2026-10-21T23:59:00+02:00"
        rig.google.reset_write_log()
        rig.sync(a)
        assert rig.google.write_log == []

    @pytest.mark.parametrize("exc", [OutcomeUnknownError, NetworkError])
    def test_task_insert_not_applied_stays_uncertain_and_is_created_once_next_run(self, exc):
        rig = Rig()
        a = S.assignment()
        rig.google.fail_next("insert_task", exc, applied=False)
        eng = rig.sync(a)
        assert changes(eng, TSK)[0].status == ChangeStatus.UNCERTAIN and rig.google.tasks == []
        assert len(rig.store.open_intents()) == 1 and len(rig.google.writes("insert_task")) == 0
        rig.write_again()
        assert len(rig.google.tasks) == 1 and rig.store.open_intents() == [] and not rig.st(a).dirty

    def test_unreadable_list_never_leads_to_a_second_task(self):
        rig = Rig()
        rig.google = ListFailsOnce()
        a = S.assignment()
        rig.google.fail_next("insert_task", OutcomeUnknownError, applied=True)
        eng = rig.sync(a)
        assert changes(eng, TSK)[0].status == ChangeStatus.UNCERTAIN and len(rig.google.tasks) == 1
        assert len(rig.store.open_intents()) == 1 and rig.st(a).task_state == TaskState.NONE
        rig.google.block = True  # the list stays unreadable: still no second insert
        rig.write_again()
        assert len(rig.google.writes("insert_task")) == 1
        rig.google.block = False
        rig.write_again()
        st = rig.st(a)
        assert len(rig.google.tasks) == 1 and len(rig.google.writes("insert_task")) == 1
        assert st.task_state == TaskState.OPEN and st.task_id == rig.google.tasks[0]["id"] and rig.store.open_intents() == []

    @pytest.mark.parametrize("what", ["event", "task"])
    def test_process_death_between_google_write_and_commit_creates_no_duplicate(self, monkeypatch, what):
        rig = Rig()
        a = S.assignment()
        target = "calendar" if what == "event" else "tasks"
        real, armed = StateStore.resolve_intent, [True]

        def die(self, intent_id, note=None):
            if armed[0] and self.get_intent(intent_id).target.value == target:
                armed[0] = False
                raise RuntimeError("Prozess abgebrochen (Test).")
            return real(self, intent_id, note)

        monkeypatch.setattr(StateStore, "resolve_intent", die)
        with pytest.raises(RuntimeError):
            rig.sync(a)
        monkeypatch.setattr(StateStore, "resolve_intent", real)
        assert rig.st(a).dirty and len(rig.store.open_intents()) == 1
        rig.sync(a)  # the next run
        assert len(rig.google.events) == 1 and len(rig.google.tasks) == 1
        assert len(rig.google.writes("insert_event")) == 1 and len(rig.google.writes("insert_task")) == 1
        st = rig.st(a)
        assert (st.event_state, st.task_state, st.dirty) == (EventState.ACTIVE, TaskState.OPEN, False)
        assert rig.store.open_intents() == [] and st.task_id == rig.google.tasks[0]["id"]

    def test_task_delete_with_lost_response_ends_as_removed_not_user_deleted(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.fail_next("delete_task", OutcomeUnknownError, applied=True)
        eng = rig.sync(cancelled(a))
        assert changes(eng, TSK)[0].status == ChangeStatus.UNCERTAIN and rig.st(a).task_state == TaskState.OPEN
        assert len(rig.store.open_intents()) == 1
        eng2 = rig.write_again()
        st = rig.st(a)
        assert st.task_state == TaskState.REMOVED and st.task_removed_reason == "cancelled" and not st.dirty
        assert rig.store.open_intents() == [] and len(rig.google.writes("delete_task")) == 1
        assert changes(eng2, TSK)[0].action == Action.REMOVE_TASK and "user_deleted_task" not in notice_codes(eng2)

    def test_task_delete_that_did_not_happen_is_retried(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        rig.google.fail_next("delete_task", NetworkError, applied=False)
        rig.sync(cancelled(a))
        assert rig.google.open_tasks != []
        rig.write_again()
        assert rig.google.open_tasks == [] and rig.st(a).task_state == TaskState.REMOVED


# ====================================================================================== preview


class TestPreview:
    @pytest.mark.parametrize("how", ["no_login", "reads_fail"])
    @pytest.mark.parametrize("change", ["moved", "cancelled"])
    def test_a_preview_that_cannot_read_google_plans_the_change_instead_of_claiming_deletions(self, how, change):
        """The dry-run gateway answers an unreadable read with 'nothing there'. That is unknowable, not 'deleted by the
        user': the preview must still show the planned update/cancellation (and the runner adds the incomplete-preview
        warning), otherwise an expired Google login turns every changed entry into a false 'deleted in Google'."""
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        wanted = moved(a, 2026, 10, 28, 23, 59) if change == "moved" else cancelled(a)
        if how == "no_login":
            dry = DryRunGateway(None)
        else:
            for op in ("get_event", "get_task", "list_tasks"):
                rig.google.fail_always(op, NetworkError)
            dry = DryRunGateway(rig.google)
        before = len(rig.google.write_log)
        eng = SyncEngine(rig.store.copy_to_memory(), rig.cfg, lambda: dry, preview=True)
        eng.ingest(rig.fetch(EDU, [wanted]), NOW)
        eng.write_phase([EDU], NOW)
        assert dry.reads_unavailable and len(rig.google.write_log) == before
        assert not {n.code for n in eng.notices} & {"user_deleted_event", "user_deleted_task"}
        action = Action.UPDATE if change == "moved" else Action.CANCEL
        task_action = Action.UPDATE if change == "moved" else Action.REMOVE_TASK
        assert [(c.target, c.action, c.status) for c in eng.changes] == [
            (CAL, action, ChangeStatus.PLANNED),
            (TSK, task_action, ChangeStatus.PLANNED),
        ]
        assert rig.st(a).event_state == EventState.ACTIVE and rig.st(a).task_state == TaskState.OPEN  # the real state

    def test_preview_engine_plans_changes_and_writes_nothing(self):
        rig = Rig()
        a, x = S.assignment(), S.exam()
        dry = DryRunGateway(rig.google)
        copy = rig.store.copy_to_memory()
        eng = SyncEngine(copy, rig.cfg, lambda: dry, preview=True)
        for src, items in ((EDU, [a]), (WU, [x])):
            eng.ingest(rig.fetch(src, items), NOW)
        eng.write_phase([EDU, WU], NOW)
        assert rig.google.write_log == [] and rig.google.tasks == [] and rig.google.events == {}
        assert len(eng.changes) == 4 and all(c.status == ChangeStatus.PLANNED for c in eng.changes)
        assert len(dry.planned_writes) == 4 and eng.counts_for(EDU).calendar.new == 1
        assert rig.store.all_items() == []  # the real state was not touched
        rig.sync(a, x)  # and a real run afterwards still writes everything
        assert len(rig.google.events) == 2 and len(rig.google.open_tasks) == 2
