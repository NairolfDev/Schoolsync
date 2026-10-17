"""Regression tests for the fixes of the requirements audit (tests/test_audit_<n>.py hold the failing tests the auditors wrote).

The audit tests prove that a gap is closed. These tests pin the surrounding behaviour that the fix must not break and the
edges of the new code: per-target deferral, the project-property lookup before an event is created, the tombstone rules of
tasks (completed / deleted by the user / removed by SchulSync), the location the user typed, the taken-over section of a retry
report, the report retention of a failure outside the runner, and the read-only guard of WebUntis.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from schulsync import runner, textblocks
from schulsync.adapters import webuntis as wu_module
from schulsync.adapters.webuntis import READ_ONLY_RPC_METHODS, ForbiddenMethodError, WebUntisAdapter, expect_field_somewhere
from schulsync.cli import CliEnv, main
from schulsync.config import Config, ReportConfig
from schulsync.dryrun import DryRunGateway
from schulsync.errors import (
    ErrorInfo,
    NetworkError,
    OutcomeUnknownError,
    SchemaError,
    TargetUnavailableError,
)
from schulsync.google_bodies import task_marker_uid, task_removed_by_us
from schulsync.google_targets import TargetsUnavailableError
from schulsync.google_types import PROP_UID, make_event_id
from schulsync.logging_setup import shutdown_logging
from schulsync.models import (
    Action,
    Category,
    CategoryStatus,
    ChangeStatus,
    EventState,
    ItemStatus,
    ItemTime,
    Kind,
    RunMode,
    Severity,
    Target,
    TaskState,
)
from schulsync.report import NOTICE_TAKEN_OVER
from schulsync.state import StateStore
from schulsync.sync import _wrote_location
from schulsync.timeutil import VIENNA
from tests.support import samples as S
from tests.support.acceptance_harness import ASSIGNMENT, HOMEWORK, World, at, fail_adapter, read_report, world  # noqa: F401  (fixture)
from tests.support.engine_harness import EDU, NOW, WU, Rig
from tests.support.fake_google import FakeGoogle
from tests.test_google_client import cal_entry, err, make as make_gateway, ok
from tests.test_webuntis import CFG, PW, World as WuWorld, run as run_wu
from tests.support.fake_http import FakeHttp, json_response

CAL, TSK = Target.CALENDAR, Target.TASKS


def events_with_uid(rig: Rig, uid: str) -> list[dict]:
    return [e for e in rig.google.events.values() if (e.get("extendedProperties") or {}).get("private", {}).get(PROP_UID) == uid]


def live_marker_tasks(rig: Rig, uid: str) -> list[dict]:
    return [t for t in rig.google.tasks if f"schulsync-id: {uid}" in (t.get("notes") or "") and not t.get("deleted")]


def cancelled(item):
    return replace(item, status=ItemStatus.CANCELLED)


# =============================================================================== 4.9 per-target deferral


def test_the_real_gateway_names_the_failed_target_and_what_it_verified():
    """Calendar first: a task list that is missing leaves the calendar verified; a calendar problem stops the check before the
    list is looked at (nothing verified), and so does an unconfigured list (the calendar was not looked at either)."""
    gw, _ = make_gateway([ok(cal_entry()), err(404, "notFound")])
    with pytest.raises(TargetsUnavailableError) as ei:
        gw.check_targets()
    assert set(ei.value.down) == {TSK} and ei.value.verified == {CAL}
    assert isinstance(ei.value, TargetUnavailableError) and "Aufgabenliste" in ei.value.message

    gw, _ = make_gateway([err(404, "notFound")])
    with pytest.raises(TargetsUnavailableError) as ei:
        gw.check_targets()
    assert set(ei.value.down) == {CAL} and ei.value.verified == frozenset()

    gw, _ = make_gateway([ok(cal_entry(primary=True, summary="max@example.com"))])
    with pytest.raises(TargetsUnavailableError) as ei:
        gw.check_targets()
    assert set(ei.value.down) == {CAL} and "Hauptkalender" in ei.value.message

    from schulsync.google_client import RealGoogleGateway
    from tests.test_google_client import CAL as CAL_ID, Seq, creds

    gw = RealGoogleGateway(creds(), calendar_id=CAL_ID, tasklist_id="", http=Seq([]), read_retries=0)
    with pytest.raises(TargetsUnavailableError) as ei:
        gw.check_targets()
    assert set(ei.value.down) == {TSK} and ei.value.verified == frozenset()


def test_targets_unavailable_error_needs_a_failed_target_and_joins_the_texts():
    with pytest.raises(ValueError):
        TargetsUnavailableError({})
    a, b = ErrorInfo("target_unavailable", "Kalender fehlt.", "Hinweis 1", "d1"), ErrorInfo("target_unavailable", "Liste fehlt.", "Hinweis 2")
    e = TargetsUnavailableError({CAL: a, TSK: b})
    assert e.message == "Kalender fehlt. Liste fehlt." and e.hint == "Hinweis 1 Hinweis 2" and e.detail == "d1"
    assert e.to_info().code == "target_unavailable"


def test_a_calendar_that_was_not_verified_never_gets_a_write_even_when_only_the_list_is_named():
    """An unconfigured list stops the check before the calendar was looked at: the calendar (never the main calendar, write
    rights) is not verified, so it waits as well."""

    class UnverifiedCalendar(FakeGoogle):
        def check_targets(self):
            self.calls["check_targets"] += 1
            raise TargetsUnavailableError({TSK: ErrorInfo("target_unavailable", "Aufgabenliste nicht eingerichtet.")})

    rig = Rig()
    rig.google = UnverifiedCalendar()
    eng = rig.sync(S.assignment())
    assert rig.google.write_log == [] and {c.status for c in eng.changes} == {ChangeStatus.DEFERRED}
    assert eng.calendar_target.ok is False and eng.tasks_target.ok is False


def test_the_preview_with_a_missing_list_still_plans_the_calendar_part_and_writes_nothing():
    rig = Rig(tasklist_exists=False)
    eng = rig.sync(S.assignment(), engine=rig.engine(preview=True, gateway=DryRunGateway(rig.google)))
    assert rig.google.write_log == []
    by_target = {c.target: c.status for c in eng.changes}
    assert by_target == {CAL: ChangeStatus.PLANNED, TSK: ChangeStatus.DEFERRED}


def test_a_task_list_that_fails_later_in_the_run_still_defers_only_the_tasks():
    rig = Rig()
    rig.google.fail_always("list_tasks", TargetUnavailableError)
    eng = rig.sync(S.assignment(), S.homework(due=date(2026, 10, 20)))
    assert len(rig.google.events) == 2 and rig.google.tasks == []
    assert {c.target for c in eng.changes if c.status == ChangeStatus.DEFERRED} == {TSK}


# =============================================================================== 7.6a known twins


def test_a_known_twin_that_is_cancelled_after_becoming_identical_is_still_cancelled():
    rig = Rig()
    a = S.homework("1", title="Uebung", due=date(2026, 10, 20))
    b = S.homework("2", title="Uebung", due=date(2026, 10, 22))
    rig.sync(a, b)
    b_same = replace(b, when=ItemTime(date(2026, 10, 20)))
    rig.sync(a, b_same)
    rig.sync(a, cancelled(b_same))  # the twin is withdrawn: its event and task must follow
    ev = rig.google.events[make_event_id(b.uid, 0)]
    assert ev["summary"].startswith("Abgesagt: ") and "reminders" in ev and ev["reminders"]["overrides"] == []
    assert len([t for t in rig.google.open_tasks if f"schulsync-id: {b.uid}" in t["notes"]]) == 0
    assert len([t for t in rig.google.open_tasks if f"schulsync-id: {a.uid}" in t["notes"]]) == 1  # the other twin is untouched


def test_a_new_duplicate_of_a_known_entry_is_still_not_created():
    rig = Rig()
    a = S.homework("1", title="Uebung", due=date(2026, 10, 20))
    twin = S.homework("2", title="Uebung", due=date(2026, 10, 20))
    rig.sync(a)
    eng = rig.sync(a, twin)
    assert len(rig.google.events) == 1 and rig.store.get(twin.uid) is None
    assert any(n.code == "duplicate_in_source" and n.uid == twin.uid for n in eng.notices)


# =============================================================================== 7.8 events by project property


def test_a_failing_property_lookup_creates_nothing_and_is_retried():
    """The lookup is a read before the insert. If it fails nothing may be created on suspicion; the next run creates the
    event exactly once."""
    rig = Rig()
    rig.google.fail_next("find_events_by_uid", NetworkError)
    a = S.assignment()
    eng = rig.sync(a)
    assert rig.google.events == {} and rig.google.writes("insert_event") == []
    assert [c.status for c in eng.changes if c.target == CAL] == [ChangeStatus.FAILED] and rig.st(a).dirty
    rig.write_again()
    assert len(rig.google.events) == 1 and not rig.st(a).dirty


def test_the_normal_first_creation_still_inserts_with_the_deterministic_id():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    assert list(rig.google.events) == [make_event_id(a.uid, 0)] and rig.google.calls["find_events_by_uid"] == 1


def test_an_event_found_by_its_property_is_adopted_and_updated_not_duplicated_when_the_state_is_gone():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    rig.store = StateStore(":memory:")
    eng = rig.sync(replace(a, title="Laborprotokoll 2 (neu)"))
    assert len(rig.google.events) == 1 and rig.google.writes("insert_event")[1:] == []
    (change,) = [c for c in eng.changes if c.target == CAL]
    assert change.action == Action.ADOPT and change.status == ChangeStatus.APPLIED
    assert rig.google.events[make_event_id(a.uid, 0)]["summary"].endswith("(neu)")


def test_an_adopted_restored_event_continues_the_generation_so_a_second_restore_works():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    rig.google.user_delete_event(make_event_id(a.uid, 0))
    rig.sync(replace(a, title="Laborprotokoll 2 (b)"))
    eng = rig.engine()
    eng.restore(None, NOW)
    eng.write_phase([EDU], NOW)  # generation 1
    rig.store = StateStore(":memory:")
    rig.sync(replace(a, title="Laborprotokoll 2 (c)"))  # found by its property, adopted
    assert rig.st(a).event_gen == 1 and rig.st(a).event_id == make_event_id(a.uid, 1)
    rig.google.user_delete_event(make_event_id(a.uid, 1))  # the user deletes the restored event again
    rig.sync(replace(a, title="Laborprotokoll 2 (d)"))
    assert rig.st(a).event_state == EventState.USER_DELETED and events_with_uid(rig, a.uid) == []
    eng = rig.engine()
    assert eng.restore([a.uid], NOW).restored == [a.uid]
    eng.write_phase([EDU], NOW)
    assert [e["id"] for e in events_with_uid(rig, a.uid)] == [make_event_id(a.uid, 2)]  # not stuck on the deleted id


def test_an_event_that_belongs_to_another_item_is_never_adopted_by_property():
    """Identity adoption hands an event to the new uid while Google still carries the OLD uid: when the old uid returns as a
    new item the lookup finds that very event but its id belongs to another item's state."""

    def hw(d):
        return replace(S.homework("x", title="Hausuebung", due=d, course="Mathe"), source_id=f"hw:Hausuebung|{d.isoformat()}", id_stable=False)

    rig = Rig()
    first, moved = hw(date(2026, 10, 16)), hw(date(2026, 10, 19))
    rig.sync(first)
    rig.sync(moved, write=False)
    rig.sync(first, moved)
    assert len(rig.google.events) == 2


def test_two_live_events_with_one_project_property_adopt_the_first_and_warn():
    rig = Rig()
    a = S.assignment()
    for eid in ("ss" + "a" * 40, "ss" + "b" * 40):
        rig.google.insert_event(
            {
                "id": eid,
                "summary": "alt",
                "start": {"date": "2026-10-21"},
                "end": {"date": "2026-10-22"},
                "extendedProperties": {"private": {PROP_UID: a.uid}},
            }
        )
    eng = rig.sync(a)
    assert rig.st(a).event_id == "ss" + "a" * 40 and len(rig.google.events) == 2
    assert any(n.code == "duplicate_event_property" and n.severity == Severity.WARNING for n in eng.notices)


# =============================================================================== 6.2 / 7.10 tombstones of tasks


def _task_of(rig: Rig, item):
    (t,) = [t for t in rig.google.tasks if f"schulsync-id: {item.uid}" in (t.get("notes") or "")]
    return t


def test_a_hidden_and_deleted_task_is_done_after_a_lost_database():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    t = _task_of(rig, a)
    rig.google.user_complete_task(t["id"])
    rig.google.user_clear_completed()
    rig.google.user_delete_task(t["id"])
    rig.store = StateStore(":memory:")
    eng = rig.sync(a)
    assert rig.google.open_tasks == [] and rig.st(a).task_state == TaskState.DONE and len(rig.google.tasks) == 1
    assert rig.google.writes("patch_task") == [] and [c.action for c in eng.changes if c.target == TSK] == [Action.ADOPT]


def test_a_task_the_user_deleted_stays_deleted_across_runs_after_a_lost_database_and_is_reported():
    rig = Rig()
    h = S.homework(due=date(2026, 10, 20))
    rig.sync(h)
    rig.google.user_delete_task(_task_of(rig, h)["id"])
    rig.store = StateStore(":memory:")
    eng = rig.sync(h)
    assert any(n.code == "user_deleted_task" for n in eng.notices) and rig.st(h).task_state == TaskState.USER_DELETED
    for _ in range(2):
        rig.sync(h)
    assert live_marker_tasks(rig, h.uid) == [] and len(rig.google.tasks) == 1
    assert [s.uid for s in rig.store.all_items() if s.task_state == TaskState.USER_DELETED] == [h.uid]  # `status` lists it
    eng = rig.engine()
    assert eng.restore([h.uid], NOW).restored == [h.uid]  # an explicit restore is possible
    eng.write_phase([WU], NOW)
    assert len(live_marker_tasks(rig, h.uid)) == 1


def test_the_removal_of_a_cancelled_task_leaves_a_trace_and_the_marker_stays_readable():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    task_id = _task_of(rig, a)["id"]
    rig.sync(cancelled(a))
    deleted = rig.google.task(task_id)
    assert deleted["deleted"] and task_removed_by_us(deleted, a.uid) and task_marker_uid(deleted) == a.uid
    assert deleted["notes"].splitlines()[0] == textblocks.task_removed_line(a.uid)
    assert [w[0] for w in rig.google.write_log if w[0] in ("patch_task", "delete_task")][-2:] == ["patch_task", "delete_task"]
    assert all("status" not in w[2] for w in rig.google.write_log if w[0] == "patch_task")  # a patch never touches the status


def test_a_removal_by_schulsync_followed_by_a_reactivation_after_a_lost_database_creates_the_task_again():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    rig.sync(cancelled(a))
    rig.store = StateStore(":memory:")
    rig.sync(a)  # reactivated, local state gone: the trace says it was SchulSync's own removal
    assert len(live_marker_tasks(rig, a.uid)) == 1 and rig.st(a).task_state == TaskState.OPEN


def test_a_removal_without_trace_reads_as_a_deletion_by_the_user():
    """The trace is best effort: if its patch fails the deleted task has none and the conservative reading applies."""
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    rig.google.fail_next("patch_task", NetworkError)
    rig.sync(cancelled(a))
    assert rig.google.open_tasks == [] and not task_removed_by_us(rig.google.tasks[0], a.uid)
    rig.store = StateStore(":memory:")
    rig.sync(a)
    assert live_marker_tasks(rig, a.uid) == [] and rig.st(a).task_state == TaskState.USER_DELETED


def test_notes_at_the_api_limit_get_no_trace_and_the_task_is_still_removed():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    t = _task_of(rig, a)
    rig.google.user_edit_task_notes(t["id"], t["notes"] + "x" * (8192 - len(t["notes"])))
    rig.sync(cancelled(a))
    assert rig.google.open_tasks == [] and not task_removed_by_us(rig.google.task(t["id"]), a.uid)


def test_a_task_the_user_completed_while_the_cancellation_was_planned_is_neither_traced_nor_deleted():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    rig.google.user_complete_task(_task_of(rig, a)["id"])
    rig.google.reset_write_log()
    rig.sync(cancelled(a))
    assert rig.google.writes("patch_task") == [] and rig.google.writes("delete_task") == []
    assert not rig.google.tasks[0].get("deleted") and rig.st(a).task_state == TaskState.DONE


def test_the_trace_is_recognised_in_the_dry_run_of_a_preview_without_writing():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    rig.google.reset_write_log()
    eng = rig.sync(cancelled(a), engine=rig.engine(preview=True, gateway=DryRunGateway(rig.google)))
    assert rig.google.write_log == [] and {c.status for c in eng.changes} == {ChangeStatus.PLANNED}
    assert {c.action for c in eng.changes if c.target == TSK} == {Action.REMOVE_TASK}


def test_has_removal_trace_only_for_the_own_uid():
    notes = f"{textblocks.task_removed_line('u1')}\n{textblocks.task_marker_line('u1')}"
    assert textblocks.has_removal_trace(notes, "u1") and not textblocks.has_removal_trace(notes, "u2")
    assert not textblocks.has_removal_trace(None, "u1") and not textblocks.has_removal_trace("", "u1")
    assert textblocks.has_removal_trace(notes.replace("\n", "<br>"), "u1")


# =============================================================================== 6.9 restore is not undone


def test_a_restore_that_could_not_write_yet_stays_a_restore_on_the_next_run():
    """The restored task cannot be created now (the list is down): the old tombstone must not turn the restore into
    'deleted by the user' again when the list is back."""
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    rig.google.user_delete_task(_task_of(rig, a)["id"])
    rig.sync(replace(a, title="Laborprotokoll 2 (neu)"))
    assert rig.st(a).task_state == TaskState.USER_DELETED
    rig.google.tasklist_exists = False
    eng = rig.engine()
    assert eng.restore([a.uid], NOW).restored == [a.uid]
    eng.write_phase([EDU], NOW)
    assert rig.st(a).task_state == TaskState.REMOVED and rig.st(a).task_removed_reason == "restored" and rig.st(a).dirty
    rig.google.tasklist_exists = True
    eng2 = rig.write_again()
    assert len(live_marker_tasks(rig, a.uid)) == 1 and rig.st(a).task_state == TaskState.OPEN and not rig.st(a).dirty
    assert [c.action for c in eng2.changes if c.target == TSK] == [Action.RESTORE]  # labelled as the restore it is


def test_after_a_restore_the_task_is_created_once_and_a_second_user_deletion_is_respected_again():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    rig.google.user_delete_task(_task_of(rig, a)["id"])
    rig.sync(replace(a, title="Laborprotokoll 2 (neu)"))
    eng = rig.engine()
    eng.restore(None, NOW)
    eng.write_phase([EDU], NOW)
    (new,) = live_marker_tasks(rig, a.uid)
    rig.google.user_delete_task(new["id"])
    rig.sync(replace(a, title="Laborprotokoll 2 (nochmals)"))
    assert rig.st(a).task_state == TaskState.USER_DELETED and live_marker_tasks(rig, a.uid) == []


# =============================================================================== 6.7 location


def _managed(*lines: str) -> str:
    return "\n".join([textblocks.BEGIN_TEXT, *lines, textblocks.END_TEXT])


@pytest.mark.parametrize(
    "rec, expected",
    [
        ({}, True),  # nothing to protect
        ({"location": ""}, True),
        ({"location": "R 2.14", "description": _managed("Raum: R 2.14")}, True),  # SchulSync wrote it
        ({"location": " R   2.14 ", "description": _managed("Raum: R 2.14")}, True),
        ({"location": "Labor 3", "description": _managed("Raum: R 2.14")}, False),  # the user changed it
        ({"location": "Labor 3", "description": _managed("Kurs: AM")}, False),  # no room was ever written
        ({"location": "Labor 3"}, False),
        ({"location": "R 2.14", "description": "Raum: R 2.14"}, False),  # the line is outside the managed block
        ({"location": "R 2.14", "description": _managed("Raum: R 2.14").replace("\n", "<br>")}, True),  # rewritten by the editor
        ({"location": "R&D", "description": _managed("Raum: R&amp;D")}, True),
    ],
)
def test_wrote_location_tells_the_rooms_schulsync_copied_from_the_users_own(rec, expected):
    assert _wrote_location(rec) is expected


def test_managed_lines_reads_the_block_in_every_shape_and_nothing_outside():
    plain = f"Meine Notiz\n{_managed('Frist: Mi.', 'Raum: A1')}\nNach dem Block"
    assert textblocks.managed_lines(plain) == ["Frist: Mi.", "Raum: A1"]
    assert textblocks.managed_lines(plain.replace("\n", "<br>")) == ["Frist: Mi.", "Raum: A1"]
    assert textblocks.managed_lines("") == [] and textblocks.managed_lines(None) == []
    assert textblocks.managed_lines(f"{textblocks.BEGIN_TEXT}\nRaum: A1") == []  # no complete block


def test_the_room_of_the_source_is_copied_replaced_and_cleared_only_while_it_is_our_own_location():
    rig = Rig()
    x = S.exam()  # room R 2.14
    rig.sync(x)
    (eid,) = rig.google.events
    assert rig.google.events[eid]["location"] == "R 2.14"
    rig.sync(replace(x, room="R 3.01"))  # the room changed at school: overwritten (automatically copied data)
    assert rig.google.events[eid]["location"] == "R 3.01"
    rig.sync(replace(x, room=None, title="Schularbeit AM (Raum offen)"))  # no room any more: our own location is cleared
    assert "location" not in rig.google.events[eid] and rig.google.events[eid]["summary"].endswith("(Raum offen)")


def test_a_location_typed_over_the_room_of_the_source_is_replaced_by_the_next_room_and_kept_without_one():
    rig = Rig()
    x = S.exam()
    rig.sync(x)
    (eid,) = rig.google.events
    rig.google.patch_event(eid, {"location": "Aula (mein Ort)"})  # the user overwrites the copied room
    rig.sync(replace(x, room=None, title="Schularbeit AM (a)"))
    assert rig.google.events[eid]["location"] == "Aula (mein Ort)"  # not ours any more: kept
    rig.sync(replace(x, room="R 9.99", title="Schularbeit AM (b)"))
    assert rig.google.events[eid]["location"] == "R 9.99"  # the source names a room again: automatic data wins


def test_the_users_location_survives_a_cancellation_and_a_reactivation():
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    (eid,) = rig.google.events
    rig.google.patch_event(eid, {"location": "Labor 3 (mein Ort)"})
    rig.sync(cancelled(a))
    assert rig.google.events[eid]["summary"].startswith("Abgesagt: ") and rig.google.events[eid]["location"] == "Labor 3 (mein Ort)"
    rig.sync(a)
    assert not rig.google.events[eid]["summary"].startswith("Abgesagt: ") and rig.google.events[eid]["location"] == "Labor 3 (mein Ort)"


# =============================================================================== 8.6 the retry report names what was taken over


def _taken_over(report) -> list:
    return [n for n in report.notices if n.code == NOTICE_TAKEN_OVER]


def test_a_source_that_is_not_fetched_again_lists_what_google_already_holds(world: World):
    world.load_standard()
    fail_adapter(world.webuntis, NetworkError("Keine Verbindung (Test)."), times=500)
    first = world.start(at=at(2026, 10, 14, 8, 0))
    assert _taken_over(first.report) == []  # fetched in this very start: its changes are listed instead
    second = world.start(at=at(2026, 10, 14, 9, 0))
    rows = _taken_over(second.report)
    assert [(n.source.value, n.title, n.message) for n in rows] == [("eduvidual", ASSIGNMENT.title, "Kalender und Aufgabe")]
    html = read_report(second)
    done = html[html.index('id="uebernommen"') : html.index('id="ausstehend"')]
    assert "Schon früher übernommen" in done and ASSIGNMENT.title in done and "In diesem Start wurde nichts neu übernommen" in done
    notices = html[html.index('id="hinweise"') : html.index('id="naechste-schritte"')]
    assert ASSIGNMENT.title not in notices  # not mixed into the notices
    assert "Keine." not in done


def test_the_taken_over_list_marks_cancelled_and_done_entries_and_skips_past_ones(world: World):
    world.load_standard()
    fail_adapter(world.webuntis, NetworkError("Keine Verbindung (Test)."), times=500)
    world.start(at=at(2026, 10, 14, 8, 0))
    with world.store() as st:
        done = st.get(ASSIGNMENT.uid)
    world.google.user_complete_task([t for t in world.google.tasks][0]["id"])
    out = world.start(at=at(2026, 10, 14, 9, 0))  # Eduvidual is done for today: not fetched, the task state is the old one
    assert [n.message for n in _taken_over(out.report)] == ["Kalender und Aufgabe"] and done is not None
    # a day later the deadline (21.10. 23:59) is still ahead; two weeks later it is fully past and not listed
    later = world.start(at=at(2026, 10, 30, 9, 0))
    assert [n for n in _taken_over(later.report) if n.uid == ASSIGNMENT.uid] == []


def test_a_fetched_source_does_not_get_the_taken_over_list_and_neither_do_preview_or_restore(world: World):
    world.load_standard()
    first = world.start(at=at(2026, 10, 14, 8, 0))
    assert _taken_over(first.report) == []
    forced = world.start(RunMode.MANUAL, force=True, at=at(2026, 10, 14, 9, 0))
    assert _taken_over(forced.report) == []
    preview = world.preview(at=at(2026, 10, 14, 10, 0))
    assert _taken_over(preview.report) == []
    html = read_report(preview)
    assert "Schon früher übernommen" not in html


def test_a_successful_silent_start_still_writes_no_report(world: World):
    world.load_standard()
    world.start(at=at(2026, 10, 14, 8, 0))
    quiet = world.start(at=at(2026, 10, 14, 9, 0))
    assert quiet.silent and quiet.report is None and world.rec.opened == []


# =============================================================================== 8.16 failure report retention outside the runner


def _failure_env(paths, config_keep_days: int | None = None):
    paths.ensure()
    if config_keep_days is not None:
        paths.config.write_text(f"[report]\nkeep_days = {config_keep_days}\n", encoding="utf-8")
    return CliEnv(
        say=lambda text="": None,
        ask=lambda prompt: "",
        ask_secret=lambda prompt: "",
        clock=lambda: datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA),
        paths=paths,
        open_report=lambda path: True,
    )


def test_the_failure_report_prunes_with_the_configured_retention_and_keeps_todays_report(paths):
    paths.ensure()
    for day in ("2026-10-13", "2026-10-09", "2026-10-08", "2026-09-01"):
        (paths.reports / f"bericht-{day}.html").write_text("alt", encoding="utf-8")
    env = _failure_env(paths)
    paths.config.write_text("[retry]\nrun_timeout_seconds = 0\n", encoding="utf-8")  # broken on purpose: no valid config
    try:
        assert main(["run", "--auto"], env=env) == 2
    finally:
        shutdown_logging()
    assert (paths.reports / "bericht-2026-10-14.html").is_file()
    assert (paths.reports / "bericht-2026-10-13.html").is_file()  # the default is 30 days
    assert not (paths.reports / "bericht-2026-09-01.html").exists()


def test_the_failure_report_uses_the_keep_days_of_a_valid_configuration(paths, monkeypatch):
    paths.ensure()
    for day in ("2026-10-13", "2026-10-09", "2026-10-08"):
        (paths.reports / f"bericht-{day}.html").write_text("alt", encoding="utf-8")
    # a VALID configuration (keep_days = 3) and a run that dies before it can report itself (here: the runner raises)
    env = _failure_env(paths, config_keep_days=3)

    def boom(*args, **kwargs):
        raise TargetUnavailableError("Die Prozesssperre ist nicht nutzbar (Test).", hint="Datenordner prüfen.")

    monkeypatch.setattr(runner, "run", boom)
    try:
        code = main(["run", "--auto"], env=env)
    finally:
        shutdown_logging()
    assert code != 0
    assert (paths.reports / "bericht-2026-10-14.html").is_file()
    assert (paths.reports / "bericht-2026-10-13.html").is_file()
    assert not (paths.reports / "bericht-2026-10-09.html").exists() and not (paths.reports / "bericht-2026-10-08.html").exists()


# =============================================================================== 3.3c read-only guard of WebUntis


def test_the_allow_list_of_webuntis_methods_is_exactly_the_three_read_methods():
    assert READ_ONLY_RPC_METHODS == {"authenticate", "getSchoolyears", "getTimetable"}


def test_every_json_rpc_method_the_adapter_source_names_is_on_the_allow_list():
    """A static scan of the adapter: a method added later without extending the allow-list (and so without a decision
    whether it only reads) is found here, before it is ever sent."""
    source = Path(wu_module.__file__).read_text(encoding="utf-8")
    named = set(re.findall(r'_rpc\(\s*"(\w+)"', source))
    assert named and named <= READ_ONLY_RPC_METHODS
    assert not re.findall(r"""["'](?:set|save|update|delete|create|add|remove|mark)\w*["']\s*,\s*\{""", source)


@pytest.mark.parametrize("method", ["setHomeworkCompleted", "deleteExam", "createHomework", "authenticate2", "", "GETTIMETABLE"])
def test_forbidden_rpc_methods_are_refused_before_any_request(method):
    http = FakeHttp()
    adapter = WebUntisAdapter(CFG, PW, http_factory=lambda: http)
    with pytest.raises(ForbiddenMethodError) as ei:
        adapter._rpc(method, {}, "x")
    assert http.requests == [] and ei.value.code == "internal"


# =============================================================================== 3.4f structure change heuristic (WebUntis)


def test_a_single_entry_without_a_date_stays_a_notice_but_two_without_any_date_field_are_a_structure_change():
    expect_field_somewhere([{"id": 1}], ("dueDate",), "$.x")  # one entry: cannot be told from a legitimately undated one
    expect_field_somewhere([], ("dueDate",), "$.x")
    expect_field_somewhere([{"id": 1, "dueDate": 0}, {"id": 2, "dueDate": 0}], ("dueDate",), "$.x")  # present with 0 = undated
    expect_field_somewhere([{"id": 1}, {"id": 2, "dueDate": 20261016}], ("dueDate",), "$.x")  # one carries it: fine
    with pytest.raises(SchemaError) as ei:
        expect_field_somewhere([{"id": 1}, {"id": 2}], ("dueDate",), "$.data.homeworks")
    assert "$.data.homeworks[*].dueDate" in ei.value.detail and "hunter2" not in ei.value.message


def test_the_alternative_exam_date_field_is_accepted():
    expect_field_somewhere([{"id": 1, "startDateTime": "2026-10-21T08:00:00"}, {"id": 2, "startDateTime": "2026-10-22T08:00:00"}], ("examDate", "startDateTime"), "$.x")


def test_a_single_homework_without_the_date_field_is_still_a_visible_warning_not_an_ok_silence():
    w = WuWorld()
    w.homework = lambda r: json_response(
        {"data": {"homeworks": [{"id": 1, "lessonId": 3301, "date": 20261012, "text": "HA 1", "remark": "", "completed": False, "attachments": []}], "lessons": [{"id": 3301, "subject": "AM"}]}}
    )
    res = run_wu(w)
    assert res.categories[Category.HOMEWORK].status == CategoryStatus.OK
    assert [n.code for n in res.notices if n.code == "no_date"] == ["no_date"]


def test_a_renamed_field_names_the_json_path_and_no_data_value():
    w = WuWorld()
    w.homework = lambda r: json_response(
        {"data": {"homeworks": [{"id": i, "lessonId": 3301, "text": f"Geheimtext {i}", "due": 20261016} for i in (1, 2)], "lessons": []}}
    )
    oc = run_wu(w).categories[Category.HOMEWORK]
    assert oc.status == CategoryStatus.FAILED and oc.error.code == "schema"
    assert "dueDate" in oc.error.detail and "Geheimtext" not in (oc.error.detail + oc.error.message)


# =============================================================================== 3.2c summer: no school year is said, not silent


def test_a_window_outside_every_school_year_says_so_in_a_notice():
    w = WuWorld()
    res = run_wu(w, now=datetime(2026, 7, 20, 8, 0, tzinfo=VIENNA))
    c = res.categories[Category.CANCELLATIONS]
    assert c.status == CategoryStatus.OK and c.item_count == 0
    (n,) = [n for n in res.notices if n.code == "no_school_year"]
    assert n.severity == Severity.INFO and "Schuljahr" in n.message and "nicht geprüft" in n.message


def test_a_window_inside_a_school_year_has_no_such_notice():
    assert [n for n in run_wu(WuWorld()).notices if n.code == "no_school_year"] == []
