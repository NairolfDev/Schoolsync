"""Requirements audit 4 (ANFORDERUNGEN sections 6 and 7): tests that demonstrate gaps in sync.py / state.py / rules.py.

Every test in this file is expected to FAIL on the current code, for the reason stated in its docstring. They are
deterministic: FakeGoogle, injected time, no network. Engine level (the `Rig` of tests/support/engine_harness.py)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from schulsync.errors import OutcomeUnknownError
from schulsync.google_types import PROP_UID
from schulsync.models import EventState, ItemStatus, ItemTime, TaskState
from schulsync.state import StateStore
from tests.support import samples as S
from tests.support.engine_harness import EDU, NOW, WU, Rig


def _events_with_uid(rig: Rig, uid: str) -> list[dict]:
    return [e for e in rig.google.events.values() if (e.get("extendedProperties") or {}).get("private", {}).get(PROP_UID) == uid]


def _live_marker_tasks(rig: Rig, uid: str) -> list[dict]:
    return [t for t in rig.google.tasks if f"schulsync-id: {uid}" in (t.get("notes") or "") and not t.get("deleted")]


def _unstable_homework(d: date):
    """A homework whose id was derived from its content by the adapter (id_stable=False)."""
    h = S.homework("x", title="Hausuebung", due=d, course="Mathe")
    return replace(h, source_id=f"hw:Hausuebung|{d.isoformat()}", id_stable=False)


# ---------------------------------------------------------------------------------------------- 7.1 / 7.3 / 7.8


def test_event_of_an_adopted_unstable_item_is_found_by_its_project_property_after_the_database_was_lost():
    """7 (identity, private project properties, 'vor einem erneuten Anlegen anhand der Projektkennung abgleichen'):
    an item without a stable id moved and was adopted by content: its event keeps the id derived from the OLD uid
    while `schulsync_uid` now names the NEW uid. If the local database is lost, the engine only probes the id
    derived from the new uid, finds nothing and inserts a SECOND event with the same `schulsync_uid`.
    `GoogleGateway.find_events_by_uid` exists for exactly this lookup but the engine never calls it."""
    rig = Rig()
    first, moved_to = _unstable_homework(date(2026, 10, 16)), _unstable_homework(date(2026, 10, 19))
    rig.sync(first)
    rig.sync(moved_to)  # adopted: one event, id of `first`, property schulsync_uid = moved_to.uid
    assert len(rig.google.events) == 1 and len(_events_with_uid(rig, moved_to.uid)) == 1
    rig.store = StateStore(":memory:")  # the local mapping is gone, Google still holds everything

    rig.sync(moved_to)

    assert len(_events_with_uid(rig, moved_to.uid)) == 1, "a second event with the same project property was created"


def test_a_restored_event_is_found_by_its_project_property_after_the_database_was_lost():
    """7 (same lookup by project property): after an explicit restore the live event has the id of generation 1.
    With the database gone the engine probes generation 0, gets 409 for the reserved id of the deleted event, reads
    a tombstone and records 'deleted by the user' although a live event with our `schulsync_uid` exists: the live
    event is never found again, never updated (the shifted deadline stays stale in the calendar)."""
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    (old_id,) = rig.google.events
    rig.google.user_delete_event(old_id)
    rig.sync(replace(a, title="Laborprotokoll 2 (neu)"))  # the engine notices the deletion
    eng = rig.engine()
    eng.restore(None, NOW)
    eng.write_phase([EDU], NOW)
    assert len(_events_with_uid(rig, a.uid)) == 1  # the restored event (generation 1)
    rig.store = StateStore(":memory:")  # the local mapping is gone

    rig.sync(replace(a, title="Laborprotokoll 2 (nochmals geaendert)", when=ItemTime(S.at(2026, 10, 28, 23, 59))))

    (live,) = _events_with_uid(rig, a.uid)
    assert live["start"]["dateTime"].startswith("2026-10-28"), "the live event of the project was not found and stayed stale"
    assert rig.st(a).event_state == EventState.ACTIVE


# ---------------------------------------------------------------------------------------------- 6 (restore)


def test_a_task_restored_on_request_is_not_taken_for_deleted_again_after_an_unclear_create():
    """6 ('explizite Wiederherstellung ermoeglichen'): the user deleted the task, `restore` releases it, the create
    answers 'outcome unknown' (not applied, intent stays open). The next run reconciles the open intent: no LIVE marker
    task, but the old tombstone of the task the user deleted is still in the list, and for state NONE the recovery
    reads a tombstone as 'deleted by the user'. The restored task is never created: the explicit restore is undone."""
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    (task,) = rig.google.tasks
    rig.google.user_delete_task(task["id"])
    rig.sync(replace(a, title="Laborprotokoll 2 (neu)"))
    assert rig.st(a).task_state == TaskState.USER_DELETED
    rig.google.fail_next("insert_task", OutcomeUnknownError, applied=False)
    eng = rig.engine()
    assert eng.restore([a.uid], NOW).restored == [a.uid]
    eng.write_phase([EDU], NOW)  # unclear create, nothing was created
    assert _live_marker_tasks(rig, a.uid) == []

    rig.write_again()  # the next start

    assert len(_live_marker_tasks(rig, a.uid)) == 1, "the explicitly restored task was never created"
    assert rig.st(a).task_state == TaskState.OPEN


# ---------------------------------------------------------------------------------------------- 7 (deleted task states)


def test_after_a_lost_database_a_task_the_user_deleted_is_not_created_again():
    """7: 'Auch erledigte, ausgeblendete und geloeschte Task-Zustaende bei der Zuordnung beruecksichtigen, soweit von
    der API bereitgestellt.' The real gateway lists deleted tasks (showDeleted=True) and FakeGoogle does too. With the
    local state gone the engine ignores the deleted marker task (only completed/hidden ones are taken into account,
    ACCEPTANCE_NOTES A8) and creates a new open task: the deliberate deletion is silently undone, which 6 forbids
    ('von mir bewusst geloeschte Task bleibt respektiert'). Needs a trace that tells SchulSync's own cancellation
    removal (may re-appear after a reactivation) from a deletion by the user. The existing weak acceptance test
    only requires '<= 1 open task' and therefore pins nothing."""
    rig = Rig()
    h = S.homework(due=date(2026, 10, 20))
    rig.sync(h)
    (task,) = rig.google.tasks
    rig.google.user_delete_task(task["id"])
    rig.store = StateStore(":memory:")  # the local state is gone; Google still holds the deleted task

    rig.sync(h)

    assert _live_marker_tasks(rig, h.uid) == [], "the task the user deleted on purpose was created again"


def test_after_a_lost_database_a_task_the_user_completed_and_then_deleted_does_not_get_an_open_copy():
    """6 ('Einmal erledigte Tasks bleiben erledigt ... Keine neue offene Kopie') and 7 ('erledigte und geloeschte
    Task-Zustaende bei der Zuordnung beruecksichtigen'). A completed task that the user then deleted stays
    `completed` + `deleted` in the list (showDeleted). SchulSync only ever deletes OPEN tasks, so a deleted
    marker task that is completed can never be its own removal: no ambiguity. `_marker_matches` still drops every
    deleted task, so with the local state gone `_create_task` inserts a brand-new open copy of a finished task."""
    rig = Rig()
    a = S.assignment()
    rig.sync(a)
    (task,) = rig.google.tasks
    rig.google.user_complete_task(task["id"])
    rig.google.user_delete_task(task["id"])
    assert rig.google.task(task["id"])["status"] == "completed" and rig.google.task(task["id"])["deleted"]
    rig.store = StateStore(":memory:")  # the local state is gone

    rig.sync(a)

    assert rig.google.open_tasks == [], "a new open copy of a task the user had completed was created"


# ---------------------------------------------------------------------------------------------- 6 (personal changes)


def test_a_location_the_user_typed_into_the_event_survives_a_source_update():
    """6 ('Lies den aktuellen Google-Stand vor Aenderungen und bewahre Text ausserhalb des verwalteten Bereichs';
    'Persoenliche ... Angaben muessen erhalten bleiben'). The event body of every patch carries `location: null`
    when the source has no room, which DELETES whatever the user typed into the Location field. The location is
    text outside the managed description block and not 'automatisch kopiert' in that case (the source gave none).
    Interpretation note: the requirement names notes explicitly; the Location field is the nearest personal field.
    A fix has to leave the field alone unless SchulSync itself wrote it (room known)."""
    rig = Rig()
    a = S.assignment()  # an assignment has no room
    rig.sync(a)
    (eid,) = rig.google.events
    rig.google.patch_event(eid, {"location": "Labor 3 (mein Ort)"})  # the user edits the event in Google Calendar
    assert rig.google.events[eid]["location"] == "Labor 3 (mein Ort)"

    rig.sync(replace(a, title="Laborprotokoll 2 (korrigiert)"))

    ev = rig.google.events[eid]
    assert ev["summary"].endswith("(korrigiert)")  # the source change did arrive
    assert ev.get("location") == "Labor 3 (mein Ort)", "the user's own location was deleted by the update"


# ---------------------------------------------------------------------------------------------- 7 (identity vs. duplicates)


def test_a_known_entry_that_becomes_identical_to_another_known_entry_is_still_followed():
    """7: stable identity ('Eine Terminverschiebung darf keine neue Identitaet erzeugen') and 'Bekannte offene
    Eintraege muessen auf Aenderungen geprueft werden'. The duplicate filter of `ingest` sorts KNOWN uids first but
    still drops every later entry with the same title/course/start, also when that entry is already known and has
    its own event. After the teacher moved homework B onto the date of homework A, B is silently no longer updated:
    its calendar event and task keep the old date, with no notice and no change."""
    rig = Rig()
    a = S.homework("1", title="Uebung", due=date(2026, 10, 20))
    b = S.homework("2", title="Uebung", due=date(2026, 10, 22))
    rig.sync(a, b)
    assert len(rig.google.events) == 2

    b_moved = replace(b, when=ItemTime(date(2026, 10, 20)))  # same title, course and day as `a` now
    rig.sync(a, b_moved)

    assert rig.google.events[next(i for i, e in rig.google.events.items() if _owner(e) == b.uid)]["start"] == {"date": "2026-10-20"}


def _owner(ev: dict) -> str | None:
    return (ev.get("extendedProperties") or {}).get("private", {}).get(PROP_UID)

