"""Eduvidual submission status: a submitted assignment ticks its Google task off; a submission the teacher hands back
(new attempt, not submitted) gets a SECOND task while the first one stays finished. Unknown status changes nothing."""

from __future__ import annotations

from dataclasses import replace

from schulsync.models import TaskState
from tests.support import samples as S
from tests.support.engine_harness import Rig


def _open(rig: Rig) -> list[dict]:
    return [t for t in rig.google.tasks if t.get("status") != "completed" and not t.get("deleted")]


def _by_id(rig: Rig, task_id: str) -> dict:
    return next(t for t in rig.google.tasks if t["id"] == task_id)


def test_submitted_assignment_ticks_the_open_task_off_and_keeps_the_event():
    rig = Rig()
    a = S.assignment(cmid="1")
    rig.sync(replace(a, submitted=False, attempt=0))
    (task,) = _open(rig)
    events = dict(rig.google.events)
    rig.sync(replace(a, submitted=True, attempt=0))
    assert _by_id(rig, task["id"])["status"] == "completed" and _open(rig) == []
    assert rig.st(a).task_state == TaskState.DONE
    assert rig.google.events == events  # the calendar entry stays as it is
    rig.sync(replace(a, submitted=True, attempt=0))  # nothing more happens afterwards
    assert len(rig.google.tasks) == 1


def test_unknown_or_draft_status_never_ticks_anything_off():
    rig = Rig()
    a = S.assignment(cmid="1")
    rig.sync(replace(a, submitted=None))
    rig.sync(replace(a, submitted=False, attempt=0))
    assert len(_open(rig)) == 1


def test_already_submitted_on_discovery_creates_no_task_but_the_event():
    rig = Rig()
    a = replace(S.assignment(cmid="1"), submitted=True, attempt=0)
    rig.sync(a)
    assert rig.google.tasks == [] and len(rig.google.events) == 1


def test_handed_back_for_revision_creates_a_second_task_and_keeps_the_first_done():
    rig = Rig()
    a = S.assignment(cmid="1")
    rig.sync(replace(a, submitted=False, attempt=0))
    rig.sync(replace(a, submitted=True, attempt=0))
    (first,) = rig.google.tasks
    rig.sync(replace(a, submitted=False, attempt=1))  # teacher reopened: new attempt
    second = _open(rig)
    assert len(second) == 1 and second[0]["id"] != first["id"] and _by_id(rig, first["id"])["status"] == "completed"
    assert len(rig.google.events) == 1  # no second calendar entry
    rig.sync(replace(a, submitted=False, attempt=1))  # stable: no third task
    assert len(rig.google.tasks) == 2
    rig.sync(replace(a, submitted=True, attempt=1))  # resubmitted: the second one is ticked off as well
    assert _open(rig) == []


def test_a_task_the_user_ticked_off_is_not_reopened_by_a_new_unsubmitted_state():
    rig = Rig()
    a = S.assignment(cmid="1")
    rig.sync(replace(a, submitted=False, attempt=0))
    (task,) = rig.google.tasks
    rig.google.user_complete_task(task["id"])
    rig.sync(replace(a, submitted=False, attempt=0, title="Laborprotokoll 2 (neu)"))  # same attempt: stays done
    assert _open(rig) == [] and len(rig.google.tasks) == 1
