"""Acceptance tests for ANFORDERUNGEN section 6 (completion, cancellations, personal changes).

Section 10 bullets 4 (deadline shift, cancellation, reactivation), 5 (done tasks and personal notes stay),
6 (unchanged overdue tasks), plus the user-deleted entries + explicit restore of section 6/7.

Every test plays several days: day 1 = Wed 14.10.2026 (first run), later days as named.
"""

from __future__ import annotations

from datetime import date

import pytest

from schulsync import textblocks
from schulsync.models import EventState, Source, TaskState
from schulsync.google_types import make_event_id
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    EXAM,
    HOMEWORK,
    LESSON,
    World,
    at,
    cancelled,
    description_of,
    due_of,
    event_for,
    event_span,
    events_for,
    is_cancelled_event,
    moved,
    notice_codes,
    open_tasks_for,
    renamed,
    same_instant,
    task_for,
    tasks_for,
    world,  # noqa: F401  (fixture)
    write_ops,
)
from tests.support.fake_google import FakeGoogle

D = date
EDU = Source.EDUVIDUAL
UID = ASSIGNMENT.uid
DAY1, DAY2, DAY3, DAY4 = (at(2026, 10, d, 8, 0) for d in (14, 15, 16, 17))
SHIFTED = at(2026, 10, 24, 18, 0)  # Saturday evening instead of Wednesday 21.10. 23:59
TASK_WRITES = ("insert_task", "patch_task", "delete_task")


def _first_day(world: World, *items) -> None:
    world.eduvidual.set_items([i for i in items if i.source == EDU])
    world.webuntis.set_items([i for i in items if i.source != EDU])
    assert world.start(at=DAY1).exit_code == 0
    world.google.reset_write_log()


def _set_edu(world: World, item) -> None:
    world.eduvidual.set_items([item])


# --------------------------------------------------------------------------- deadline shift


def test_a_deadline_shift_updates_the_same_event_and_the_open_task_without_a_new_identity(world: World):
    _first_day(world, ASSIGNMENT)
    ev0, task0 = event_for(world.google, UID), task_for(world.google, UID)
    _set_edu(world, moved(ASSIGNMENT, SHIFTED))

    out = world.start(at=DAY2)

    ev, task = event_for(world.google, UID), task_for(world.google, UID)  # still exactly one each
    assert ev["id"] == ev0["id"] and task["id"] == task0["id"]
    start, _ = event_span(ev)
    assert same_instant(start, SHIFTED)
    assert "24.10.2026 18:00" in description_of(ev) and "21.10.2026 23:59" not in description_of(ev)
    assert due_of(task) == D(2026, 10, 21)  # 24.10. minus 3 calendar days
    assert "24.10.2026 18:00" in task["notes"] and "21.10.2026 23:59" not in task["notes"]
    assert task["title"] == task0["title"] and task["status"] == "needsAction"
    assert world.google.writes("insert_event") == [] and world.google.writes("insert_task") == []
    res = out.report.sources[EDU]
    assert res.calendar.updated == 1 and res.tasks.updated == 1


def test_a_known_open_entry_is_followed_even_when_the_new_date_is_far_outside_the_window(world: World):
    _first_day(world, ASSIGNMENT)
    far = moved(ASSIGNMENT, at(2026, 12, 15, 12, 0))  # about two months away, window is 21 days
    _set_edu(world, far)

    world.start(at=DAY2)

    assert any(t.uid == UID for t in world.eduvidual.calls[-1][1]), "the adapter must be told to keep checking this entry"
    start, _ = event_span(event_for(world.google, UID))
    assert same_instant(start, far.when.start)
    assert due_of(task_for(world.google, UID)) == D(2026, 12, 12)


def test_a_shift_whose_new_work_start_is_already_past_sets_the_task_date_to_the_day_of_the_change(world: World):
    far = moved(ASSIGNMENT, at(2026, 10, 30, 12, 0))  # task 27.10.
    _first_day(world, far)
    assert due_of(task_for(world.google, UID)) == D(2026, 10, 27)
    _set_edu(world, moved(ASSIGNMENT, at(2026, 10, 18, 12, 0)))  # lead date would be 15.10.

    world.start(at=DAY4)  # Saturday 17.10.: the change is discovered today

    assert due_of(task_for(world.google, UID)) == D(2026, 10, 17)
    assert len(world.google.tasks) == 1


def test_overdue_tasks_are_not_moved_forward_every_day(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.webuntis.set_items([HOMEWORK])
    world.start(at=DAY1)
    due_a, due_h = due_of(task_for(world.google, UID)), due_of(task_for(world.google, HOMEWORK.uid))
    assert (due_a, due_h) == (D(2026, 10, 18), D(2026, 10, 14))  # the homework task got "today" once (late discovery)
    events = world.google.events

    for day in range(15, 24):  # the work-start dates and later the deadlines pass
        world.google.reset_write_log()
        world.start(at=at(2026, 10, day, 8, 0))
        assert world.google.write_log == [], f"unchanged data must write nothing on 2026-10-{day}"

    assert due_of(task_for(world.google, UID)) == due_a and due_of(task_for(world.google, HOMEWORK.uid)) == due_h
    assert world.google.events == events  # nothing deleted or touched after the deadlines passed


def test_overdue_exam_tasks_are_not_moved_forward_every_day_either(world: World):
    """The 7-day lead variant of the test above (an exam has a different lead and a time range, not a deadline)."""
    world.webuntis.set_items([EXAM])
    world.start(at=DAY1)
    due = due_of(task_for(world.google, EXAM.uid))
    assert due == D(2026, 10, 21)  # 28.10. minus 7 calendar days
    events = world.google.events

    for day in range(15, 31):  # the work-start date passes, then the exam itself, then the days after it
        world.google.reset_write_log()
        world.start(at=at(2026, 10, day, 8, 0))
        assert world.google.write_log == [], f"unchanged data must write nothing on 2026-10-{day}"

    assert due_of(task_for(world.google, EXAM.uid)) == due
    assert world.google.events == events


def test_no_write_of_any_scenario_ever_carries_a_status_or_a_field_the_api_does_not_offer_for_tasks(world: World):
    """ANFORDERUNGEN 6: 'Keine automatische Erledigung anhand eines Abgabestatus'. The builder test pins the patch body of ONE
    function; this one watches every task write the engine sends over a whole life of an entry (creation, deadline shift,
    rename, cancellation, reactivation) and a lesson that is cancelled: a new task is `needsAction`, a patch never names the
    status, and no task body ever has a key other than title / notes / due (no start time, no reminder: ANFORDERUNGEN 5)."""
    world.eduvidual.set_items([ASSIGNMENT])
    world.webuntis.set_items([HOMEWORK, EXAM, LESSON])
    world.start(at=DAY1)
    for day, item in ((DAY2, moved(ASSIGNMENT, SHIFTED)), (DAY3, renamed(ASSIGNMENT, "Laborprotokoll 2 (neu)")),
                      (DAY4, cancelled(ASSIGNMENT)), (at(2026, 10, 18, 8, 0), ASSIGNMENT)):
        _set_edu(world, item)
        world.start(at=day)

    inserts, patches = world.google.writes("insert_task"), world.google.writes("patch_task")
    assert len(inserts) >= 4 and len(patches) >= 2  # the scenario really exercised both (3 first creations + the reactivation)
    for _op, _id, body in inserts:
        assert body["status"] == "needsAction" and set(body) == {"title", "notes", "due", "status"}
    for _op, _id, body in patches:
        assert "status" not in body and set(body) <= {"title", "notes", "due"}


# --------------------------------------------------------------------------- done tasks stay done


def test_a_completed_task_stays_completed_and_untouched_when_the_school_changes_the_deadline_and_title(world: World):
    _first_day(world, ASSIGNMENT)
    task = task_for(world.google, UID)
    world.google.user_complete_task(task["id"])
    before = world.google.task(task["id"])
    _set_edu(world, renamed(moved(ASSIGNMENT, SHIFTED), "Laborprotokoll 2 (korrigierte Fassung)"))

    world.start(at=DAY2)

    assert world.google.task(task["id"]) == before  # byte for byte: status, title, due, notes
    assert len(world.google.tasks) == 1 and open_tasks_for(world.google, UID) == []  # no new open copy
    assert not any(op in TASK_WRITES for op in write_ops(world.google))
    ev = event_for(world.google, UID)
    assert "korrigierte Fassung" in ev["summary"] and same_instant(event_span(ev)[0], SHIFTED)  # the calendar follows


def test_a_task_the_user_completed_and_hid_is_still_recognised_as_done(world: World):
    _first_day(world, ASSIGNMENT)
    task = task_for(world.google, UID)
    world.google.user_complete_task(task["id"])
    world.google.user_clear_completed()  # the Tasks app hides completed tasks
    _set_edu(world, moved(ASSIGNMENT, SHIFTED))

    world.start(at=DAY2)

    assert len(world.google.tasks) == 1 and world.google.writes("insert_task") == []
    assert open_tasks_for(world.google, UID) == []
    assert world.item_state(UID).task_state == TaskState.DONE


def test_a_done_task_survives_cancellation_and_reactivation_and_no_new_open_copy_appears(world: World):
    _first_day(world, ASSIGNMENT)
    task = task_for(world.google, UID)
    world.google.user_complete_task(task["id"])
    done = world.google.task(task["id"])
    ev_id = event_for(world.google, UID)["id"]

    _set_edu(world, cancelled(ASSIGNMENT))
    world.start(at=DAY2)
    assert is_cancelled_event(event_for(world.google, UID))
    assert world.google.task(task["id"]) == done and not world.google.task(task["id"]).get("deleted")

    _set_edu(world, ASSIGNMENT)
    world.start(at=DAY3)
    ev = event_for(world.google, UID)
    assert ev["id"] == ev_id and not is_cancelled_event(ev)
    assert world.google.task(task["id"]) == done
    assert len(world.google.tasks) == 1 and world.google.writes("insert_task") == []


# --------------------------------------------------------------------------- cancellation


def test_a_confirmed_cancellation_marks_the_event_removes_the_reminder_and_deletes_only_the_open_task(world: World):
    _first_day(world, ASSIGNMENT)
    ev0 = event_for(world.google, UID)
    _set_edu(world, cancelled(ASSIGNMENT))

    out = world.start(at=DAY2)

    ev = event_for(world.google, UID)  # the entry stays, it is not deleted
    assert ev["id"] == ev0["id"] and ev["summary"].startswith("Abgesagt:") and ASSIGNMENT.title in ev["summary"]
    assert is_cancelled_event(ev)  # "Abgesagt:" title, no popup, and not the calendar default reminder either
    assert ev["start"] == ev0["start"] and ev["end"] == ev0["end"]  # still shows when it was
    assert open_tasks_for(world.google, UID) == []
    assert task_for(world.google, UID).get("deleted") is True
    assert out.report.sources[EDU].calendar.cancelled == 1
    state = world.item_state(UID)
    assert state.cancelled and state.applied_cancelled and state.task_state == TaskState.REMOVED  # the status is stored

    world.google.reset_write_log()
    world.start(at=DAY3)  # the next day repeats nothing
    assert world.google.write_log == []


def test_an_explicit_withdrawal_with_proof_cancels_the_entry_while_other_entries_stay(world: World):
    world.webuntis.set_items([HOMEWORK, LESSON])
    assert world.start(at=DAY1).exit_code == 0
    world.webuntis.set_items([LESSON])  # the withdrawn homework is no longer listed ...
    world.webuntis.set_withdrawn({HOMEWORK.uid: "Von der Lehrkraft zurückgenommen (Test)."})  # ... and the school says so

    world.start(at=DAY2)

    assert is_cancelled_event(event_for(world.google, HOMEWORK.uid))
    assert open_tasks_for(world.google, HOMEWORK.uid) == []
    assert not is_cancelled_event(event_for(world.google, LESSON.uid))  # the lesson is unaffected


# --------------------------------------------------------------------------- reactivation


def test_a_confirmed_reactivation_reuses_the_same_event_and_recreates_the_removed_open_task(world: World):
    _first_day(world, ASSIGNMENT)
    ev0 = event_for(world.google, UID)
    _set_edu(world, cancelled(ASSIGNMENT))
    world.start(at=DAY2)
    _set_edu(world, ASSIGNMENT)

    out = world.start(at=DAY3)

    ev = event_for(world.google, UID)
    assert ev["id"] == ev0["id"] and len(world.google.events) == 1 and world.google.deleted_event_ids == set()
    assert ev["summary"] == ev0["summary"]  # "Abgesagt:" is gone again
    assert ev["reminders"]["overrides"] == [{"method": "popup", "minutes": 1440}]  # reminder back
    (task,) = open_tasks_for(world.google, UID)
    assert task["title"] == f"Bearbeiten: {ASSIGNMENT.title}" and due_of(task) == D(2026, 10, 18)
    assert len(tasks_for(world.google, UID)) == 2  # the removed one stays removed, the new one is the only open task
    assert out.report.sources[EDU].calendar.reactivated == 1


def test_a_task_the_user_deleted_deliberately_is_not_recreated_on_reactivation(world: World):
    _first_day(world, ASSIGNMENT)
    world.google.user_delete_task(task_for(world.google, UID)["id"])
    _set_edu(world, cancelled(ASSIGNMENT))
    world.start(at=DAY2)
    _set_edu(world, ASSIGNMENT)

    world.start(at=DAY3)

    assert not is_cancelled_event(event_for(world.google, UID))
    assert open_tasks_for(world.google, UID) == [] and world.google.writes("insert_task") == []
    assert len(world.google.tasks) == 1


# --------------------------------------------------------------------------- personal notes


@pytest.mark.parametrize("where", ["below the block", "above the block"])
def test_personal_notes_in_event_and_task_survive_a_source_update(world: World, where: str):
    _first_day(world, ASSIGNMENT)
    ev, task = event_for(world.google, UID), task_for(world.google, UID)
    note_ev, note_task = "Meine Notiz: Frau Musterfrau wegen Aufbau fragen", "Zuerst Skript Kapitel 4 lesen"
    if where == "below the block":
        world.google.user_edit_event_description(ev["id"], description_of(ev) + note_ev)
        world.google.user_edit_task_notes(task["id"], task["notes"] + note_task)
    else:
        world.google.user_edit_event_description(ev["id"], note_ev + "\n" + description_of(ev))
        world.google.user_edit_task_notes(task["id"], note_task + "\n" + task["notes"])
    _set_edu(world, moved(ASSIGNMENT, SHIFTED))

    world.start(at=DAY2)

    text, notes = description_of(event_for(world.google, UID)), task_for(world.google, UID)["notes"]
    assert note_ev in text and note_task in notes  # personal text is untouched ...
    assert "24.10.2026 18:00" in text and "24.10.2026 18:00" in notes  # ... while the managed part is current
    for body in (text, notes):
        assert body.count(textblocks.BEGIN_TEXT) == 1 and body.count(textblocks.PERSONAL_HEADER) == 1  # no duplicate blocks
    if where == "above the block":
        assert text.find(note_ev) < text.find(textblocks.BEGIN_TEXT)  # it stays where the user put it


def test_personal_notes_survive_cancellation_and_reactivation(world: World):
    _first_day(world, ASSIGNMENT)
    ev = event_for(world.google, UID)
    note = "Persönlich: Abgabe mit Kollegen abstimmen"
    world.google.user_edit_event_description(ev["id"], description_of(ev) + note)

    for day, item in ((DAY2, cancelled(ASSIGNMENT)), (DAY3, ASSIGNMENT)):
        _set_edu(world, item)
        world.start(at=day)
        text = description_of(event_for(world.google, UID))
        assert note in text and text.count(textblocks.BEGIN_TEXT) == 1


# --------------------------------------------------------------------------- entries the user deleted on purpose


@pytest.mark.parametrize("deleted_event_reads_as_none", [False, True], ids=["tombstone", "404/410"])
def test_an_event_deleted_by_the_user_is_not_recreated_reported_and_restorable(paths, deleted_event_reads_as_none: bool):
    w = World(paths, google=FakeGoogle(deleted_event_get_returns_none=deleted_event_reads_as_none))
    _first_day(w, ASSIGNMENT)
    old_id = event_for(w.google, UID)["id"]
    w.google.user_delete_event(old_id)
    _set_edu(w, moved(ASSIGNMENT, SHIFTED))  # a change makes the engine look at the event

    out = w.start(at=DAY2)

    assert events_for(w.google, UID) == []
    assert w.google.writes("insert_event") == [] and w.google.writes("patch_event") == []  # neither revived nor recreated
    assert "user_deleted_event" in notice_codes(out.report)
    assert w.item_state(UID).event_state == EventState.USER_DELETED

    _set_edu(w, moved(ASSIGNMENT, at(2026, 10, 25, 9, 0)))
    w.start(at=DAY3)
    assert events_for(w.google, UID) == [] and w.google.writes("insert_event") == []  # not on every run

    restored = w.restore([UID])  # the explicit way back
    assert restored.exit_code == 0
    ev = event_for(w.google, UID)
    assert ev["id"] != old_id  # a deleted id stays reserved at Google
    assert same_instant(event_span(ev)[0], at(2026, 10, 25, 9, 0))  # built from the current source data
    assert textblocks.BEGIN_TEXT in description_of(ev) and ev["reminders"]["overrides"]
    assert w.item_state(UID).event_state == EventState.ACTIVE

    w.google.reset_write_log()
    w.restore([UID])  # restoring something that is not deleted changes nothing
    w.start(at=DAY4)
    assert w.google.write_log == [] and len(events_for(w.google, UID)) == 1


@pytest.mark.parametrize("purge", [False, True], ids=["deleted-flag", "vanished-completely"])
def test_a_task_deleted_by_the_user_is_not_recreated_reported_and_restorable(paths, purge: bool):
    w = World(paths)
    _first_day(w, ASSIGNMENT)
    w.google.user_delete_task(task_for(w.google, UID)["id"], purge=purge)
    _set_edu(w, moved(ASSIGNMENT, SHIFTED))

    out = w.start(at=DAY2)

    assert open_tasks_for(w.google, UID) == [] and w.google.writes("insert_task") == []
    assert "user_deleted_task" in notice_codes(out.report)
    assert same_instant(event_span(event_for(w.google, UID))[0], SHIFTED)  # the calendar part is independent
    assert w.item_state(UID).task_state == TaskState.USER_DELETED

    _set_edu(w, moved(ASSIGNMENT, at(2026, 10, 25, 9, 0)))
    w.start(at=DAY3)
    assert open_tasks_for(w.google, UID) == [] and w.google.writes("insert_task") == []

    assert w.restore([UID]).exit_code == 0
    (task,) = open_tasks_for(w.google, UID)
    assert task["title"] == f"Bearbeiten: {ASSIGNMENT.title}"
    assert w.item_state(UID).task_state == TaskState.OPEN


def test_restore_without_uids_restores_everything_the_user_deleted_and_unknown_uids_do_no_harm(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.webuntis.set_items([EXAM])
    world.start(at=DAY1)
    world.google.user_delete_event(event_for(world.google, UID)["id"])
    world.google.user_delete_task(task_for(world.google, EXAM.uid)["id"])
    world.eduvidual.set_items([moved(ASSIGNMENT, SHIFTED)])
    world.webuntis.set_items([moved(EXAM, at(2026, 10, 29, 8, 0), at(2026, 10, 29, 9, 40))])
    world.start(at=DAY2)
    assert events_for(world.google, UID) == [] and open_tasks_for(world.google, EXAM.uid) == []

    world.google.reset_write_log()
    unknown = world.restore(["eduvidual:assignment:assign:does-not-exist"])
    assert world.google.write_log == [] and unknown.console_text  # says something, writes nothing

    out = world.restore(None)

    assert out.exit_code == 0
    assert len(events_for(world.google, UID)) == 1 and len(open_tasks_for(world.google, EXAM.uid)) == 1
    assert len(world.google.events) == 2  # no duplicate of the entries that were not deleted
    assert make_event_id(UID) in world.google.deleted_event_ids
