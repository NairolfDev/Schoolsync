"""Self-check of the acceptance harness helpers (no engine involved except the optional Deps wiring test)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from schulsync.errors import NetworkError, OutcomeUnknownError
from schulsync.google_types import PROP_UID, TASK_MARKER_PREFIX
from schulsync.models import Source
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    ListFailsAfterInsertGoogle,
    ProcessKilled,
    Recorder,
    World,
    due_of,
    event_for,
    event_span,
    events_for,
    exam_start_only,
    lesson_between,
    make_config,
    moved,
    open_tasks_for,
    same_instant,
    task_for,
    tasks_for,
    when_of,
    world,  # noqa: F401  (fixture)
    at,
)
from tests.support.fake_google import FakeGoogle

UID = ASSIGNMENT.uid


def _event_body(eid: str, uid: str | None) -> dict:
    body = {
        "id": eid,
        "summary": "Abgabe: Test",
        "start": {"dateTime": "2026-10-21T23:59:00+02:00", "timeZone": "Europe/Vienna"},
        "end": {"dateTime": "2026-10-22T00:00:00+02:00", "timeZone": "Europe/Vienna"},
    }
    if uid:
        body["extendedProperties"] = {"private": {PROP_UID: uid}}
    return body


def test_event_helpers_find_only_live_events_with_the_uid():
    g = FakeGoogle()
    g.insert_event(_event_body("abcde01", UID))
    g.insert_event(_event_body("abcde02", "webuntis:exam:exam:1"))
    g.insert_event(_event_body("abcde03", None))
    assert [e["id"] for e in events_for(g, UID)] == ["abcde01"]
    assert event_for(g, UID)["id"] == "abcde01"
    g.user_delete_event("abcde01")
    assert events_for(g, UID) == []
    with pytest.raises(AssertionError):
        event_for(g, UID)


def test_event_span_parses_timed_and_all_day():
    ev = _event_body("abcde01", UID)
    start, end = event_span(ev)
    assert same_instant(start, at(2026, 10, 21, 23, 59)) and same_instant(end, at(2026, 10, 22, 0, 0))
    assert when_of({"date": "2026-10-16"}) == date(2026, 10, 16)
    with pytest.raises(AssertionError):
        when_of({"dateTime": "2026-10-16T10:00:00"})  # no offset: never acceptable towards Google


def test_task_helpers_use_the_marker_and_see_every_state():
    g = FakeGoogle()
    g.insert_task({"title": "Bearbeiten: X", "notes": f"Text\n{TASK_MARKER_PREFIX}{UID}", "due": "2026-10-18T00:00:00.000Z"})
    g.insert_task({"title": "fremd", "notes": "keine Marke", "due": "2026-10-18T00:00:00.000Z"})
    t = task_for(g, UID)
    assert due_of(t) == date(2026, 10, 18)
    assert open_tasks_for(g, UID) == [t]
    g.user_complete_task(t["id"])
    assert open_tasks_for(g, UID) == [] and len(tasks_for(g, UID)) == 1
    g.user_delete_task(t["id"])
    assert len(tasks_for(g, UID)) == 1 and open_tasks_for(g, UID) == []


def test_item_builders():
    lesson = lesson_between(at(2026, 11, 3, 23, 30), at(2026, 11, 4, 0, 30))
    assert lesson.when.end == at(2026, 11, 4, 0, 30) and lesson.uid.startswith("webuntis:cancellation:")
    assert exam_start_only(at(2026, 10, 20, 9, 0)).when.end is None
    assert moved(ASSIGNMENT, at(2026, 10, 30, 12, 0)).uid == ASSIGNMENT.uid


def test_list_fails_after_a_lost_insert_response():
    g = ListFailsAfterInsertGoogle()
    g.fail_next("insert_task", OutcomeUnknownError, applied=True)
    with pytest.raises(OutcomeUnknownError):
        g.insert_task({"title": "t", "notes": f"{TASK_MARKER_PREFIX}{UID}"})
    with pytest.raises(NetworkError):
        g.list_tasks()
    g.heal_list()
    assert len(g.list_tasks()) == 1  # the lost write was applied


def test_process_killed_escapes_ordinary_exception_handlers():
    g = FakeGoogle()
    g.fail_next("insert_event", ProcessKilled, applied=True)
    try:
        try:
            g.insert_event(_event_body("abcde01", UID))
        except Exception:  # noqa: BLE001 - the point: this must NOT catch it
            pytest.fail("ProcessKilled must not be an Exception")
    except ProcessKilled:
        pass
    assert len(g.events) == 1  # applied before the "crash"


def test_make_config_is_a_finished_setup():
    cfg = make_config(retry={"max_attempts": 2}, report={"open_on": "never"})
    assert cfg.google.calendar_id and cfg.google.tasklist_id
    assert cfg.retry.max_attempts == 2 and cfg.report.open_on == "never"
    assert cfg.enabled_sources == ["eduvidual", "webuntis"]


def test_world_clock_and_recorder_hooks(world: World):
    pytest.importorskip("schulsync.runner")
    deps = world.deps()
    t0 = world.clock.monotonic()
    deps.sleep(5)
    assert world.rec.sleeps == [5.0] and world.clock.monotonic() == t0 + 5
    world.rec.network_script = [False, True]
    assert [deps.network_ready(), deps.network_ready(), deps.network_ready()] == [False, True, True]
    assert world.rec.network_checks == 3
    assert deps.open_report(world.paths.reports / "x.html") is True
    world.rec.open_result = False
    assert deps.open_report(world.paths.reports / "y.html") is False
    assert [p.name for p in world.rec.opened] == ["x.html", "y.html"]
    assert deps.clock() == world.clock.now and isinstance(deps.clock(), datetime)
    assert set(deps.adapter_factories) == {Source.EDUVIDUAL, Source.WEBUNTIS}
    assert deps.adapter_factories[Source.EDUVIDUAL]() is world.eduvidual
    assert deps.gateway_factory() is world.google and world.gateway_builds == 1
    r = Recorder()
    r.sleeps.append(1.0)
    r.clear()
    assert r.sleeps == []
