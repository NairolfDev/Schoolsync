"""Acceptance tests for ANFORDERUNGEN section 4 (Google targets) as far as behaviour is concerned.

Section 10 bullet 9: calendar "Schule" unavailable -> no fallback to the primary calendar; also the task list,
and an expired Google login. The affected writes are deferred, the error is reported, and later runs
catch up without duplicates.

Reading chosen where the text leaves room: when the target check fails the engine keeps ALL writes of that
run pending (design: one check for both targets). The tests therefore only assert what is certain for the
calendar failure: no event is written anywhere; and for the task-list failure: no task is written.
"""

from __future__ import annotations

import pytest

from schulsync.errors import GoogleAuthError, NetworkError
from schulsync.models import ChangeStatus, OverallStatus, Target
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    EXAM,
    HOMEWORK,
    LESSON,
    TODAY,
    World,
    at,
    events_for,
    read_report,
    tasks_for,
    world,  # noqa: F401  (fixture)
)
from tests.support.fake_google import FakeGoogle

STANDARD = (ASSIGNMENT, HOMEWORK, EXAM, LESSON)
DAY1 = at(2026, 10, 14, 8, 0)
LATER = at(2026, 10, 14, 9, 30)


def _assert_everything_exists_exactly_once(g: FakeGoogle) -> None:
    for item in STANDARD:
        assert len(events_for(g, item.uid)) == 1, item.uid
        assert len(tasks_for(g, item.uid)) == (0 if item.uid == LESSON.uid else 1), item.uid
    assert len(g.events) == 4 and len(g.tasks) == 3
    assert g.misrouted_writes() == [] and g.primary_events == {}


@pytest.mark.parametrize("problem", ["calendar-missing", "calendar-is-primary"])
def test_an_unavailable_calendar_schule_is_never_replaced_by_the_primary_calendar(paths, problem: str):
    g = FakeGoogle(calendar_exists=problem != "calendar-missing", calendar_is_primary=problem == "calendar-is-primary")
    w = World(paths, google=g)
    w.load_standard()

    out = w.start(at=DAY1)

    assert g.misrouted_writes() == [] and g.primary_events == {} and g.events == {}
    assert g.writes("insert_event") == [] and g.writes("patch_event") == []  # nothing, nowhere
    assert out.exit_code == 1 and out.report.status in (OverallStatus.PARTIAL, OverallStatus.FAILED)
    assert out.report.calendar_target.ok is False or out.report.calendar_target.error is not None  # the error is reported
    assert any(c.target == Target.CALENDAR and c.status == ChangeStatus.DEFERRED for c in out.report.changes)  # deferred
    # ANFORDERUNGEN 4: only the AFFECTED writes wait. The reachable task list is written, nothing of the tasks is deferred.
    assert len(g.tasks) == 3 and {c.target for c in out.report.changes if c.status == ChangeStatus.DEFERRED} == {Target.CALENDAR}
    assert out.report.tasks_target.ok is True and out.report.calendar_target.ok is False
    assert out.report.pending_writes >= 1
    assert w.rec.opened == [out.report_path]
    html = read_report(out)
    assert "nie auf einen anderen Kalender" in html  # the report says what was NOT done
    with w.store() as st:
        assert st.pending_count() >= 1 and not st.day_done(TODAY, ASSIGNMENT.source)

    # the user repairs the calendar; the next start of the same day catches up, without duplicates
    g.calendar_exists, g.calendar_is_primary = True, False
    again = w.start(at=LATER)

    assert again.exit_code == 0 and not again.silent
    _assert_everything_exists_exactly_once(g)
    assert w.start(at=at(2026, 10, 14, 17, 30)).silent


def test_a_missing_task_list_defers_the_task_writes_and_never_uses_another_list(paths):
    g = FakeGoogle(tasklist_exists=False)
    w = World(paths, google=g)
    w.load_standard()

    out = w.start(at=DAY1)

    assert g.tasks == [] and g.writes("insert_task") == []
    assert g.misrouted_writes() == [] and g.primary_events == {}
    assert out.exit_code == 1 and out.report.status in (OverallStatus.PARTIAL, OverallStatus.FAILED)
    assert out.report.tasks_target.ok is False or out.report.tasks_target.error is not None
    assert any(c.target == Target.TASKS and c.status == ChangeStatus.DEFERRED for c in out.report.changes)
    # ... and only those: the calendar "Schule" is reachable, so its four entries are written, none of them deferred
    assert len(g.events) == 4 and {c.target for c in out.report.changes if c.status == ChangeStatus.DEFERRED} == {Target.TASKS}
    assert out.report.calendar_target.ok is True and out.report.tasks_target.ok is False
    assert w.rec.opened == [out.report_path]

    g.tasklist_exists = True
    again = w.start(at=LATER)

    assert again.exit_code == 0
    _assert_everything_exists_exactly_once(g)


def test_an_expired_google_login_is_reported_with_the_renewal_steps_and_nothing_is_written(world: World):
    world.load_standard()
    world.gateway_error = GoogleAuthError(
        "Die Google-Anmeldung ist abgelaufen oder widerrufen (Test).", hint="Bitte 'schulsync setup' erneut ausführen."
    )

    out = world.start(at=DAY1)

    assert out.exit_code == 1 and out.report.status in (OverallStatus.PARTIAL, OverallStatus.FAILED)
    assert world.google.write_log == []
    assert world.rec.opened == [out.report_path]
    html = read_report(out)
    assert "Google-Anmeldung erneuern" in html  # understandable next step
    assert "7 Tagen" in html  # the Testing-status expiry is explained, no unlimited validity is promised
    with world.store() as st:
        assert not st.day_done(TODAY, ASSIGNMENT.source)

    world.gateway_error = None  # re-login done
    again = world.start(at=LATER)

    assert again.exit_code == 0
    _assert_everything_exists_exactly_once(world.google)


def test_a_short_google_outage_before_the_first_write_is_retried_a_few_times(world: World):
    world.load_standard()
    world.configure(retry={"delays_seconds": (5.0, 20.0, 45.0)})
    world.google.fail_next("check_targets", NetworkError("Keine Verbindung zu Google (Test)."), times=2)

    out = world.start(at=DAY1)

    assert out.exit_code == 0 and world.rec.opened == []
    assert world.google.calls["check_targets"] >= 3
    assert 0 < len(world.rec.sleeps) <= world.config.retry.max_attempts - 1  # few and bounded
    _assert_everything_exists_exactly_once(world.google)


def test_a_normal_run_writes_only_into_the_dedicated_calendar_and_list(world: World):
    world.load_standard()
    world.start(at=DAY1)
    assert world.google.misrouted_writes() == [] and world.google.primary_events == {}
    assert len(world.google.events) == 4
