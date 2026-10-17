"""Acceptance tests for ANFORDERUNGEN section 7 (synchronisation and data storage).

Section 10 bullets 1 (twice the same data -> no duplicates), 3 (partial failures per source/target, process
abort after a Google write) and 8 (incomplete fetch never deletes).

Days: DAY1 = Wed 14.10.2026 08:00, DAY2 = 15.10., DAY3 = 16.10.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from schulsync import textblocks
from schulsync.google_types import APP_PROP, PROP_KIND, PROP_SOURCE, PROP_UID, TASK_MARKER_PREFIX
from schulsync.errors import BadRequestError, ConfigError, ErrorInfo, OutcomeUnknownError
from schulsync.lock import ProcessLock
from schulsync.models import (
    Category,
    CategoryStatus,
    ChangeStatus,
    EventState,
    Kind,
    OverallStatus,
    RunMode,
    Source,
    SourceStatus,
    Target,
    TaskState,
)
from tests.support import samples
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    EXAM,
    HOMEWORK,
    LESSON,
    TODAY,
    ListFailsAfterInsertGoogle,
    ProcessKilled,
    World,
    at,
    cancelled,
    due_of,
    event_for,
    event_span,
    events_for,
    fail_adapter,
    is_cancelled_event,
    moved,
    notice_codes,
    open_tasks_for,
    read_report,
    renamed,
    same_instant,
    snapshot,
    task_for,
    tasks_for,
    world,  # noqa: F401  (fixture)
    write_ops,
)

D = date
EDU, WEB = Source.EDUVIDUAL, Source.WEBUNTIS
STANDARD = (ASSIGNMENT, HOMEWORK, EXAM, LESSON)
DAY1, DAY2, DAY3 = (at(2026, 10, d, 8, 0) for d in (14, 15, 16))
SHIFTED = at(2026, 10, 24, 18, 0)
UID = ASSIGNMENT.uid


# --------------------------------------------------------------------------- twice the same data


@pytest.mark.parametrize("how", ["next day", "forced start the same day", "manual start the same day"])
def test_the_same_data_twice_creates_no_duplicates_and_writes_nothing(world: World, how: str):
    world.load_standard()
    assert world.start(at=DAY1).exit_code == 0
    snap = snapshot(world.google)
    assert len(world.google.events) == 4 and len(world.google.tasks) == 3
    world.google.reset_write_log()

    if how == "next day":
        out = world.start(at=DAY2)
    elif how == "forced start the same day":
        out = world.start(at=at(2026, 10, 14, 12, 0), force=True)
    else:
        out = world.start(RunMode.MANUAL, at=at(2026, 10, 14, 12, 0))

    assert out.exit_code == 0
    assert world.google.write_log == [] and snapshot(world.google) == snap
    for res in out.report.sources.values():
        assert res.calendar.new == 0 and res.tasks.new == 0 and res.calendar.updated == 0 and res.tasks.updated == 0


def test_a_new_start_on_the_same_data_after_the_database_was_lost_still_creates_no_duplicates(world: World):
    world.load_standard()
    world.start(at=DAY1)
    done_task = task_for(world.google, UID)
    world.google.user_complete_task(done_task["id"])
    world.wipe_state()  # the local mapping is gone; Google still holds everything

    out = world.start(at=DAY2)

    assert out.exit_code == 0
    assert len(world.google.events) == 4 and len(world.google.tasks) == 3  # recognised by id/marker, nothing doubled
    assert open_tasks_for(world.google, UID) == []  # the completed task is still the completed task
    assert world.google.task(done_task["id"])["status"] == "completed"
    for item in (HOMEWORK, EXAM):
        assert len(open_tasks_for(world.google, item.uid)) == 1
    with world.store() as st:
        assert st.get(UID).event_id == event_for(world.google, UID)["id"]
        assert st.get(UID).task_id == done_task["id"]


def test_after_the_database_was_lost_a_task_the_user_deleted_never_leads_to_two_open_tasks(world: World):
    """ANFORDERUNGEN 6/7: 'eine von mir bewusst geloeschte Task bleibt respektiert', 'auch geloeschte Task-Zustaende bei der
    Zuordnung beruecksichtigen'. SchulSync leaves a trace in the notes before it removes a task itself after a cancellation;
    a deleted marker task WITHOUT that trace was deleted by the user. So with the DB gone the task is neither created again
    nor adopted, the deletion is reported and recorded (`status` lists it), and it stays that way. (Until the audit this test
    only required '<= 1 open task', which holds whether the deletion is respected or silently undone.)"""
    world.load_standard()
    world.start(at=DAY1)
    deleted = task_for(world.google, HOMEWORK.uid)["id"]
    world.google.user_delete_task(deleted)
    world.wipe_state()
    world.google.reset_write_log()

    day2 = world.start(at=DAY2)
    world.start(at=DAY3)  # and again, later

    assert open_tasks_for(world.google, HOMEWORK.uid) == []
    assert len(tasks_for(world.google, HOMEWORK.uid)) == 1 and world.google.task(deleted)["deleted"]  # still only the old, deleted one
    assert world.google.writes("insert_task") == []  # nothing was created at all: the other tasks were adopted by their marker
    assert "user_deleted_task" in notice_codes(day2.report)
    assert world.item_state(HOMEWORK.uid).task_state == TaskState.USER_DELETED
    assert len(events_for(world.google, HOMEWORK.uid)) == 1


def test_an_event_the_user_deleted_is_not_revived_even_after_the_database_was_lost(world: World):
    world.load_standard()
    world.start(at=DAY1)
    world.google.user_delete_event(event_for(world.google, LESSON.uid)["id"])
    world.wipe_state()

    world.start(at=DAY2)

    assert events_for(world.google, LESSON.uid) == []
    assert len(world.google.events) == 3


# --------------------------------------------------------------------------- local database content


def test_the_local_state_records_source_ids_target_ids_status_and_last_successful_fetch(world: World):
    world.load_standard()
    world.start(at=DAY1)

    with world.store() as st:
        for item in STANDARD:
            s = st.get(item.uid)
            assert (s.item.source, s.item.source_id, s.item.kind) == (item.source, item.source_id, item.kind)
            assert s.event_state == EventState.ACTIVE and s.event_id == event_for(world.google, item.uid)["id"]
            assert not s.dirty and not s.cancelled
            if item.kind == Kind.CANCELLATION:
                assert s.task_state == TaskState.NONE and s.task_id is None
            else:
                task = task_for(world.google, item.uid)
                assert s.task_state == TaskState.OPEN and s.task_id == task["id"] and s.task_due == due_of(task)
        assert st.pending_count() == 0 and st.open_intents() == []
        for src in (EDU, WEB):
            assert st.day_done(TODAY, src) and st.last_complete_fetch(src) is not None
        run = st.last_run()
        assert run.day == TODAY and run.status == OverallStatus.SUCCESS and run.mode == RunMode.AUTO


def test_a_relevant_change_of_the_source_is_recorded_in_the_local_state_and_an_irrelevant_one_is_not(world: World):
    """ANFORDERUNGEN 7: the database stores 'relevante Inhaltsaenderungen'. After a moved deadline and a new title the row
    carries the new content, a new content hash and the new applied hashes; the identity (uid, first sighting, target ids) is
    unchanged. A later run with the same data changes none of it."""
    world.eduvidual.set_items([ASSIGNMENT])
    world.start(at=DAY1)
    before = world.item_state(UID)
    new_item = renamed(moved(ASSIGNMENT, SHIFTED), "Laborprotokoll 2 (Version B)")
    world.eduvidual.set_items([new_item])

    world.start(at=DAY2)

    after = world.item_state(UID)
    assert after.item.title == "Laborprotokoll 2 (Version B)" and after.item.when == new_item.when
    assert after.item_hash != before.item_hash and after.event_applied_hash != before.event_applied_hash
    assert after.task_applied_hash != before.task_applied_hash and after.task_base_deadline != before.task_base_deadline
    assert after.task_due == due_of(task_for(world.google, UID)) != before.task_due
    assert (after.event_id, after.task_id, after.first_seen_at) == (before.event_id, before.task_id, before.first_seen_at)
    assert after.last_seen_at > before.last_seen_at and not after.dirty

    world.start(at=DAY3)  # the same data again
    again = world.item_state(UID)
    assert (again.item_hash, again.event_applied_hash, again.task_applied_hash, again.task_base_deadline, again.task_due) == (
        after.item_hash, after.event_applied_hash, after.task_applied_hash, after.task_base_deadline, after.task_due
    )


def test_a_changed_title_with_the_same_source_id_is_the_same_entry(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.start(at=DAY1)
    ev_id = event_for(world.google, UID)["id"]
    world.eduvidual.set_items([renamed(ASSIGNMENT, "Laborprotokoll 2: Operationsverstärker (Version B)")])

    world.start(at=DAY2)

    ev = event_for(world.google, UID)
    assert ev["id"] == ev_id and "Version B" in ev["summary"] and len(world.google.events) == 1
    assert task_for(world.google, UID)["title"].endswith("(Version B)") and len(world.google.tasks) == 1


# --------------------------------------------------------------------------- identity without stable ids


def _derived(hid: str, due: date, title: str = "Übung Kapitel 7"):
    """A homework whose id was derived from its content by the adapter (id_stable=False)."""
    return dataclasses.replace(samples.homework(hid=hid, title=title, due=due), id_stable=False)


def test_without_a_stable_id_a_unique_content_match_is_followed_and_reported_as_uncertain(world: World):
    world.webuntis.set_items([_derived("d-aaa", D(2026, 10, 20))])
    world.start(at=DAY1)
    world.webuntis.set_items([_derived("d-bbb", D(2026, 10, 22))])  # same text, new derived id, moved date

    out = world.start(at=DAY2)

    assert len(world.google.events) == 1 and len(world.google.tasks) == 1  # moved, not duplicated
    (ev,) = world.google.events.values()
    assert ev["start"] == {"date": "2026-10-22"} and not is_cancelled_event(ev)
    assert "identity_uncertain" in notice_codes(out.report)


def test_without_a_stable_id_an_ambiguous_match_is_never_merged_or_cancelled_and_is_reported(world: World):
    world.webuntis.set_items([_derived("d-1", D(2026, 10, 20)), _derived("d-2", D(2026, 10, 21))])
    world.start(at=DAY1)
    before = world.google.events
    world.webuntis.set_items([_derived("d-3", D(2026, 10, 22))])  # two candidates gone, one new: which is which?

    out = world.start(at=DAY2)

    now = world.google.events
    for eid, ev in before.items():
        assert now[eid] == ev, "an old entry must not be modified on a guess"
    assert not any(is_cancelled_event(e) for e in now.values())
    assert any(e["start"] == {"date": "2026-10-22"} for e in now.values())  # the new entry is not lost either
    assert "identity_uncertain" in notice_codes(out.report)


def test_with_stable_ids_entries_are_never_merged_by_title(world: World):
    a = samples.homework(hid="1", title="Übung", due=D(2026, 10, 20))
    b = samples.homework(hid="2", title="Übung", due=D(2026, 10, 22))
    world.webuntis.set_items([a, b])
    world.start(at=DAY1)
    assert len(world.google.events) == 2
    c = samples.homework(hid="3", title="Übung", due=D(2026, 10, 21))
    world.webuntis.set_items([b, c])  # a vanished from the list, a same-titled c appeared

    out = world.start(at=DAY2)

    assert len(world.google.events) == 3 and not any(is_cancelled_event(e) for e in world.google.events.values())
    assert event_for(world.google, a.uid)["start"] == {"date": "2026-10-20"}  # a untouched
    assert "identity_uncertain" not in notice_codes(out.report)


def test_identical_content_within_one_source_is_created_only_once(world: World):
    world.webuntis.set_items([samples.homework(hid="1001"), samples.homework(hid="1002")])  # same title, course and date

    out = world.start(at=DAY1)

    assert len(world.google.events) == 1 and len(world.google.tasks) == 1
    assert "duplicate_in_source" in notice_codes(out.report)
    world.start(at=DAY2)
    assert len(world.google.events) == 1 and len(world.google.tasks) == 1


def test_similar_entries_of_the_two_sources_are_not_merged(world: World):
    task_in_eduvidual = samples.assignment(cmid="1", title="Beispiele 12 bis 15 rechnen", course="Angewandte Mathematik", due=D(2026, 10, 16))
    homework_in_webuntis = samples.homework(hid="1")  # same title, same course, same day
    world.eduvidual.set_items([task_in_eduvidual])
    world.webuntis.set_items([homework_in_webuntis])

    world.start(at=DAY1)

    assert len(world.google.events) == 2 and len(world.google.tasks) == 2
    event_for(world.google, task_in_eduvidual.uid)
    event_for(world.google, homework_in_webuntis.uid)


# --------------------------------------------------------------------------- only entries of this project are touched


def _foreign_event(eid: str, summary: str, start: str, end: str) -> dict:
    return {
        "id": eid,
        "summary": summary,
        "description": "Mein eigener Termin",
        "start": {"dateTime": start, "timeZone": "Europe/Vienna"},
        "end": {"dateTime": end, "timeZone": "Europe/Vienna"},
    }


def test_foreign_calendar_entries_and_tasks_are_never_changed_adopted_or_deleted(world: World):
    g = world.google
    g.insert_event(_foreign_event("fremd0000001", "Zahnarzt", "2026-10-20T15:00:00+02:00", "2026-10-20T16:00:00+02:00"))
    g.insert_event(  # same title and time as our entry, but not ours (no project marker)
        _foreign_event("fremd0000002", f"Abgabe: {ASSIGNMENT.title}", "2026-10-21T23:59:00+02:00", "2026-10-22T00:00:00+02:00")
    )
    own_look_alike = g.insert_task({"title": f"Bearbeiten: {ASSIGNMENT.title}", "notes": "Meine eigene Aufgabe", "due": "2026-10-18T00:00:00.000Z"})
    other_task = g.insert_task({"title": "Mathe wiederholen", "notes": "privat", "due": "2026-10-19T00:00:00.000Z"})
    g.reset_write_log()
    foreign_events = {k: v for k, v in g.events.items()}
    foreign_tasks = {t["id"]: t for t in (own_look_alike, other_task)}
    world.load_standard()

    world.start(at=DAY1)
    world.eduvidual.set_items([cancelled(ASSIGNMENT)])  # cancellation: our task goes, the look-alike must stay
    world.webuntis.set_items([moved(HOMEWORK, D(2026, 10, 30)), EXAM, LESSON])
    world.start(at=DAY2)

    for eid, ev in foreign_events.items():
        assert g.events[eid] == ev
    for tid, task in foreign_tasks.items():
        current = g.task(tid)
        assert current["status"] == "needsAction" and not current.get("deleted")
        assert current["title"] == task["title"] and current["notes"] == task["notes"] and current["due"] == task["due"]
    assert len(g.events) == 2 + 4 and len(g.tasks) == 2 + 3
    assert len(events_for(g, UID)) == 1 and len(tasks_for(g, UID)) == 1
    foreign_ids = set(foreign_events) | set(foreign_tasks)
    assert all(wid not in foreign_ids for _op, wid, _body in g.write_log if wid is not None)


def test_entries_with_a_project_marker_of_another_uid_are_never_taken_over_or_changed_either(world: World):
    """ANFORDERUNGEN 7: 'Ausschliesslich eindeutig diesem Projekt zugehoerige Eintraege veraendern'. The test above has foreign
    entries WITHOUT a marker. Here an event carries our private properties and a task carries our marker line, but for a uid
    that belongs to no entry of this run (an old entry, another installation): it is not ours to change, adopt or delete, also
    not after the local database was lost and the engine searches Google by project identifier."""
    g = world.google
    stranger_uid = "eduvidual:assignment:assign:99999"
    stranger = _foreign_event("fremd0000009", "Abgabe: Fremder Auftrag", "2026-10-22T10:00:00+02:00", "2026-10-22T10:01:00+02:00")
    stranger["extendedProperties"] = {"private": {APP_PROP: "1", PROP_UID: stranger_uid, PROP_SOURCE: "eduvidual", PROP_KIND: "assignment"}}
    g.insert_event(stranger)
    marked = g.insert_task({"title": "Bearbeiten: Fremder Auftrag", "notes": f"Notiz\n{TASK_MARKER_PREFIX}{stranger_uid}", "due": "2026-10-19T00:00:00.000Z"})
    foreign_event, foreign_task = dict(g.events["fremd0000009"]), g.task(marked["id"])
    g.reset_write_log()
    world.load_standard()

    world.start(at=DAY1)
    world.wipe_state()  # nothing is known locally any more: everything is matched through Google
    world.eduvidual.set_items([cancelled(ASSIGNMENT)])
    world.start(at=DAY2)

    assert g.events["fremd0000009"] == foreign_event and g.task(marked["id"]) == foreign_task
    assert all(wid not in ("fremd0000009", marked["id"]) for _op, wid, _body in g.write_log if wid is not None)
    assert len(events_for(g, UID)) == 1 and len(events_for(g, stranger_uid)) == 1
    assert len(tasks_for(g, stranger_uid)) == 1


# --------------------------------------------------------------------------- sources are independent


@pytest.mark.parametrize("failing", [EDU, WEB], ids=["eduvidual-fails", "webuntis-fails"])
def test_a_failing_source_neither_removes_nor_blocks_the_other(world: World, failing: Source):
    world.load_standard()
    world.start(at=DAY1)
    edu_item, web_item = moved(ASSIGNMENT, SHIFTED), moved(HOMEWORK, D(2026, 10, 30))
    world.eduvidual.set_items([edu_item])
    world.webuntis.set_items([web_item, EXAM, LESSON])
    fail_adapter(world.adapters[failing])
    failing_uids = [ASSIGNMENT.uid] if failing == EDU else [HOMEWORK.uid, EXAM.uid, LESSON.uid]
    healthy_uid = HOMEWORK.uid if failing == EDU else ASSIGNMENT.uid
    before = {u: event_for(world.google, u) for u in failing_uids}
    tasks_before = {u: task_for(world.google, u) for u in failing_uids if u != LESSON.uid}
    world.google.reset_write_log()

    out = world.start(at=DAY2)

    other = WEB if failing == EDU else EDU
    assert out.exit_code == 1 and out.report.status == OverallStatus.PARTIAL
    assert out.report.sources[failing].status == SourceStatus.FAILED
    assert out.report.sources[other].status == SourceStatus.SUCCESS
    for uid in failing_uids:  # nothing of the failing source was cancelled, deleted or changed
        assert event_for(world.google, uid) == before[uid]
    for uid, task in tasks_before.items():
        assert task_for(world.google, uid) == task
    start, _ = event_span(event_for(world.google, healthy_uid))  # the healthy source's change went through
    assert same_instant(start, SHIFTED) if failing == WEB else start == D(2026, 10, 30)
    with world.store() as st:
        assert st.day_done(DAY2.date(), other) and not st.day_done(DAY2.date(), failing)


def test_a_source_that_cannot_even_be_built_fails_alone(world: World):
    world.load_standard()
    world.factory_errors[EDU] = ConfigError("Das Passwort für Eduvidual fehlt (Test).", hint="Bitte 'schulsync setup' ausführen.")

    out = world.start()

    assert out.exit_code in (1, 2)
    assert out.report.sources[EDU].status == SourceStatus.FAILED and out.report.sources[WEB].status == SourceStatus.SUCCESS
    assert len(world.google.events) == 3 and events_for(world.google, UID) == []
    assert "schulsync setup" in read_report(out)


# --------------------------------------------------------------------------- incomplete fetch never deletes or cancels


@pytest.mark.parametrize("status", [CategoryStatus.FAILED, CategoryStatus.INCOMPLETE])
def test_an_incomplete_fetch_never_cancels_or_deletes_and_is_not_a_day_success(world: World, status: CategoryStatus):
    world.load_standard()
    world.start(at=DAY1)
    with world.store() as st:
        first_success = st.last_complete_fetch(WEB)
    snap = snapshot(world.google)
    world.google.reset_write_log()
    world.webuntis.fail_category(Category.EXAMS, ErrorInfo("incomplete", "Seite 2 der Prüfungsliste fehlt (Test)."), status=status)

    out = world.start(at=DAY2)  # the exam is missing from this answer

    assert world.google.write_log == [] and snapshot(world.google) == snap  # exam event + task stay exactly as they were
    res = out.report.sources[WEB]
    assert out.exit_code == 1 and res.status in (SourceStatus.PARTIAL, SourceStatus.FAILED)
    assert res.categories[Category.EXAMS].status == status
    assert res.stale and res.last_success_at == first_success  # reported as outdated, with the last real success
    with world.store() as st:
        assert not st.day_done(DAY2.date(), WEB)
        assert st.last_complete_fetch(WEB) == first_success  # not advanced by an incomplete fetch


def test_a_failed_login_never_cancels_or_deletes_anything(world: World):
    world.load_standard()
    world.start(at=DAY1)
    snap = snapshot(world.google)
    world.webuntis.login_fails(ErrorInfo("auth", "Anmeldung abgelehnt (Test)."))
    world.eduvidual.login_fails(ErrorInfo("auth", "Anmeldung abgelehnt (Test)."))
    world.google.reset_write_log()

    out = world.start(at=DAY2)

    assert out.exit_code == 1 and world.google.write_log == [] and snapshot(world.google) == snap


def test_complete_categories_are_still_updated_when_another_category_is_incomplete(world: World):
    world.load_standard()
    world.start(at=DAY1)
    exam_before = event_for(world.google, EXAM.uid)
    world.webuntis.set_items([moved(HOMEWORK, D(2026, 10, 30)), EXAM, LESSON])
    world.webuntis.fail_category(Category.EXAMS, ErrorInfo("incomplete", "Seite 2 fehlt (Test)."), status=CategoryStatus.INCOMPLETE)

    world.start(at=DAY2)

    assert event_for(world.google, HOMEWORK.uid)["start"] == {"date": "2026-10-30"}  # homework list was complete
    assert due_of(task_for(world.google, HOMEWORK.uid)) == D(2026, 10, 27)
    assert event_for(world.google, EXAM.uid) == exam_before and not is_cancelled_event(event_for(world.google, EXAM.uid))


@pytest.mark.parametrize("item", STANDARD, ids=[i.kind.value for i in STANDARD])
@pytest.mark.parametrize("later", [DAY2, at(2026, 11, 20, 8, 0)], ids=["next day", "five weeks later"])
def test_missing_from_a_complete_list_or_beyond_the_window_is_not_a_cancellation(world: World, item, later):
    world.load_standard()
    world.start(at=DAY1)
    snap = snapshot(world.google)
    world.eduvidual.set_items([i for i in (ASSIGNMENT,) if i != item])
    world.webuntis.set_items([i for i in (HOMEWORK, EXAM, LESSON) if i != item])
    world.google.reset_write_log()

    world.start(at=later)

    assert world.google.write_log == [] and snapshot(world.google) == snap
    assert not is_cancelled_event(event_for(world.google, item.uid))
    if item.kind != Kind.CANCELLATION:
        assert len(open_tasks_for(world.google, item.uid)) == 1


# --------------------------------------------------------------------------- the process dies after a Google write


def _second_start_finishes_the_job(world: World, at_time=None):
    out = world.start(at=at_time or at(2026, 10, 14, 9, 0))  # next Windows logon the same day
    assert out.exit_code == 0 and not out.silent
    return out


def test_killed_right_after_google_created_an_event_the_next_start_adopts_it_without_a_duplicate(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.google.fail_next("insert_event", ProcessKilled, applied=True)
    world.start_killed(at=DAY1)
    assert len(world.google.events) == 1  # Google has it; the local state may not know yet

    _second_start_finishes_the_job(world)

    assert len(events_for(world.google, UID)) == 1 and len(world.google.events) == 1
    assert len(world.google.writes("insert_event")) == 1
    assert len(tasks_for(world.google, UID)) == 1
    with world.store() as st:
        assert st.pending_count() == 0 and st.get(UID).event_id == event_for(world.google, UID)["id"]
    assert world.start(at=at(2026, 10, 14, 17, 0)).silent  # and the day is complete now


def test_killed_right_after_google_created_a_task_the_next_start_adopts_it_by_its_marker(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.google.fail_next("insert_task", ProcessKilled, applied=True)
    world.start_killed(at=DAY1)
    assert len(world.google.tasks) == 1

    _second_start_finishes_the_job(world)

    assert len(world.google.tasks) == 1 and len(world.google.writes("insert_task")) == 1
    with world.store() as st:
        assert st.get(UID).task_id == task_for(world.google, UID)["id"] and st.get(UID).task_state == TaskState.OPEN


def test_killed_right_after_google_applied_a_deadline_shift_the_next_start_completes_it_cleanly(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.start(at=DAY1)
    world.eduvidual.set_items([moved(ASSIGNMENT, SHIFTED)])
    world.google.fail_next("patch_event", ProcessKilled, applied=True)
    world.start_killed(at=DAY2)

    _second_start_finishes_the_job(world, at(2026, 10, 15, 9, 0))

    ev = event_for(world.google, UID)
    assert same_instant(event_span(ev)[0], SHIFTED) and ev["description"].count(textblocks.BEGIN_TEXT) == 1
    assert due_of(task_for(world.google, UID)) == D(2026, 10, 21) and len(world.google.tasks) == 1


def test_killed_right_after_google_removed_the_open_task_the_next_start_completes_the_cancellation(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.start(at=DAY1)
    world.eduvidual.set_items([cancelled(ASSIGNMENT)])
    world.google.fail_next("delete_task", ProcessKilled, applied=True)
    world.start_killed(at=DAY2)

    _second_start_finishes_the_job(world, at(2026, 10, 15, 9, 0))

    assert is_cancelled_event(event_for(world.google, UID))
    assert open_tasks_for(world.google, UID) == [] and len(world.google.tasks) == 1
    with world.store() as st:
        assert st.get(UID).task_state == TaskState.REMOVED and st.pending_count() == 0


def test_the_lock_is_free_again_after_a_killed_run(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.google.fail_next("insert_event", ProcessKilled, applied=True)
    world.start_killed(at=DAY1)
    lock = ProcessLock(world.paths.lock)
    assert lock.acquire()
    lock.release()


def test_a_lost_response_when_creating_an_event_never_leads_to_a_second_event(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.google.fail_next("insert_event", OutcomeUnknownError("Antwort verloren (Test)."), applied=True)
    world.start(at=DAY1)

    out = world.start(at=at(2026, 10, 14, 9, 0))

    assert out.exit_code == 0
    assert len(events_for(world.google, UID)) == 1 and len(world.google.events) == 1
    assert len(world.google.writes("insert_event")) == 1 and len(tasks_for(world.google, UID)) == 1


@pytest.mark.parametrize("applied", [True, False], ids=["task-was-created", "task-was-not-created"])
def test_an_unclear_task_insert_creates_no_second_task_on_suspicion(paths, applied: bool):
    g = ListFailsAfterInsertGoogle()  # after the unclear insert the reconciliation list cannot be loaded either
    w = World(paths, google=g)
    w.eduvidual.set_items([ASSIGNMENT])
    g.fail_next("insert_task", OutcomeUnknownError("Antwort verloren (Test)."), applied=applied)

    first = w.start(at=DAY1)

    assert g.calls["insert_task"] == 1  # no second attempt while the outcome is unknown
    assert first.exit_code == 1
    assert any(c.target == Target.TASKS and c.status != ChangeStatus.APPLIED for c in first.report.changes)
    assert len(g.tasks) == (1 if applied else 0)

    g.heal_list()
    second = w.start(at=at(2026, 10, 14, 9, 0))

    assert second.exit_code == 0
    assert len(tasks_for(g, UID)) == 1 and len(open_tasks_for(g, UID)) == 1  # exactly one task, either way
    assert w.item_state(UID).task_id == task_for(g, UID)["id"]


# --------------------------------------------------------------------------- the day only counts after the Google writes


def test_a_day_without_completed_google_writes_is_not_a_success_and_is_caught_up_later(world: World):
    world.load_standard()
    world.google.calendar_exists = False  # the calendar is unavailable at the first logon

    first = world.start(at=DAY1)

    assert first.exit_code == 1 and first.report.status in (OverallStatus.PARTIAL, OverallStatus.FAILED)
    with world.store() as st:
        assert not st.day_done(TODAY, EDU) and not st.day_done(TODAY, WEB)
    world.google.calendar_exists = True

    second = world.start(at=at(2026, 10, 14, 9, 30))

    assert second.exit_code == 0 and not second.silent
    assert len(world.google.events) == 4 and len(world.google.tasks) == 3
    assert [op for op in write_ops(world.google)].count("insert_event") == 4
    assert world.start(at=at(2026, 10, 14, 17, 30)).silent


def test_a_failing_google_target_does_not_stop_the_other_target_and_is_caught_up_later(world: World):
    world.load_standard()
    world.google.fail_always("insert_task", BadRequestError("Aufgabe abgelehnt (Test)."))  # events work, tasks do not

    out = world.start(at=DAY1)

    assert out.exit_code == 1 and out.report.status in (OverallStatus.PARTIAL, OverallStatus.FAILED)
    assert len(world.google.events) == 4 and world.google.tasks == []  # the calendar part is not held back
    assert sum(r.tasks.failed for r in out.report.sources.values()) >= 1
    assert sum(r.calendar.failed for r in out.report.sources.values()) == 0
    assert any(c.target == Target.TASKS and c.status == ChangeStatus.FAILED for c in out.report.changes)
    assert len(world.rec.opened) == 1

    world.google.clear_faults()
    again = world.start(at=at(2026, 10, 14, 9, 30))

    assert again.exit_code == 0
    assert len(world.google.tasks) == 3 and len(world.google.events) == 4
    assert len(world.google.writes("insert_event")) == 4  # the events were not written a second time
