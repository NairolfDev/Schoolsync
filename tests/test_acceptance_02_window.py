"""Acceptance tests for ANFORDERUNGEN section 2 (check window) and the "nicht neu anlegen" rules of section 5.

Window: today 00:00 inclusive until today + 21 calendar days 00:00 exclusive, both Europe/Vienna.
Already expired deadlines / completely past events are never created anew.
Section 10 bullet 7 (21-day window edges, DST).

Time of the sample runs: Wed 14.10.2026 08:00 -> window [14.10. 00:00, 04.11. 00:00).
Chosen readings (most protective of the user's data):
- A deadline exactly at the current instant is not yet expired (a deadline is only expired AFTER it).
- An event that is still running (end after now) is still created; one that ended exactly now is over.
- An exam/lesson belongs to the window if it starts inside it (it may end after the window end).
"""

from __future__ import annotations

from datetime import date

import pytest

from schulsync.models import Notice, Severity, Source
from tests.support import samples
from tests.support.acceptance_harness import (
    NOW,
    World,
    at,
    cancelled,
    event_for,
    lesson_between,
    read_report,
    same_instant,
    skipped,
    world,  # noqa: F401  (fixture)
)

D = date

# (id, item, expectation): "created" or the skip reason the report must name
_NEW_ITEM_CASES = [
    # --- deadlines with a time (Eduvidual assignments)
    ("assignment-deadline-is-now", samples.assignment(cmid="1", due=at(2026, 10, 14, 8, 0)), "created"),
    ("assignment-expired-one-minute-ago", samples.assignment(cmid="2", due=at(2026, 10, 14, 7, 59)), "expired"),
    ("assignment-expired-yesterday", samples.assignment(cmid="3", due=at(2026, 10, 13, 23, 59)), "expired"),
    ("assignment-last-minute-of-window", samples.assignment(cmid="4", due=at(2026, 11, 3, 23, 59)), "created"),
    ("assignment-exactly-at-window-end", samples.assignment(cmid="5", due=at(2026, 11, 4, 0, 0)), "outside_window"),
    # --- date-only homework (a date stands for the whole day)
    ("homework-due-today", samples.homework(hid="1", due=D(2026, 10, 14)), "created"),
    ("homework-due-yesterday", samples.homework(hid="2", due=D(2026, 10, 13)), "expired"),
    ("homework-last-day-of-window", samples.homework(hid="3", due=D(2026, 11, 3)), "created"),
    ("homework-first-day-after-window", samples.homework(hid="4", due=D(2026, 11, 4)), "outside_window"),
    # --- exams
    ("exam-ended-exactly-now", samples.exam(eid="1", start=at(2026, 10, 14, 7, 15), end=at(2026, 10, 14, 8, 0)), "expired"),
    ("exam-still-running", samples.exam(eid="2", start=at(2026, 10, 14, 7, 30), end=at(2026, 10, 14, 8, 15)), "created"),
    ("exam-later-today", samples.exam(eid="3", start=at(2026, 10, 14, 9, 0), end=at(2026, 10, 14, 9, 45)), "created"),
    ("exam-starts-at-window-end", samples.exam(eid="4", start=at(2026, 11, 4, 0, 0), end=at(2026, 11, 4, 0, 45)), "outside_window"),
    ("exam-last-evening-in-window", samples.exam(eid="5", start=at(2026, 11, 3, 22, 0), end=at(2026, 11, 3, 22, 45)), "created"),
    ("exam-date-only-today", samples.exam(eid="6", start=D(2026, 10, 14), end=None), "created"),
    ("exam-date-only-yesterday", samples.exam(eid="7", start=D(2026, 10, 13), end=None), "expired"),
    # --- cancelled lessons
    ("lesson-ended-exactly-now", lesson_between(at(2026, 10, 14, 7, 15), at(2026, 10, 14, 8, 0), lesson="1"), "expired"),
    ("lesson-in-progress", lesson_between(at(2026, 10, 14, 7, 30), at(2026, 10, 14, 8, 15), lesson="2"), "created"),
    ("lesson-yesterday", lesson_between(at(2026, 10, 13, 10, 15), at(2026, 10, 13, 11, 55), lesson="3"), "expired"),
    ("lesson-straddling-window-end", lesson_between(at(2026, 11, 3, 23, 30), at(2026, 11, 4, 0, 30), lesson="4"), "created"),
    ("lesson-after-window", lesson_between(at(2026, 11, 4, 8, 0), at(2026, 11, 4, 8, 45), lesson="5"), "outside_window"),
    ("lesson-next-week", lesson_between(at(2026, 10, 20, 9, 0), at(2026, 10, 20, 9, 45), lesson="6"), "created"),
    # --- an assignment that is already cancelled when first seen has nothing to cancel
    ("assignment-already-cancelled", cancelled(samples.assignment(cmid="6")), "already_cancelled"),
]


def _adapter(world: World, item):
    return world.eduvidual if item.source == Source.EDUVIDUAL else world.webuntis


@pytest.mark.parametrize(("item", "expected"), [pytest.param(i, e, id=n) for n, i, e in _NEW_ITEM_CASES])
def test_a_new_item_is_created_only_inside_the_window_and_only_if_not_over(world: World, item, expected: str):
    _adapter(world, item).set_items([item])

    out = world.start()

    assert out.exit_code == 0
    if expected == "created":
        event_for(world.google, item.uid)
        return
    assert world.google.events == {} and world.google.tasks == []  # neither calendar entry nor task
    assert world.google.write_log == []
    assert skipped(out.report, item.source, expected) >= 1, f"report must name the skip reason {expected!r}"


@pytest.mark.parametrize(
    ("today", "item", "expected"),
    [
        # summer time ends 25.10.2026: window of 05.10. ends 26.10. 00:00 (CET); "21 * 24 h" would end an hour early
        pytest.param(at(2026, 10, 5, 8, 0), samples.exam(eid="d1", start=at(2026, 10, 25, 23, 30), end=at(2026, 10, 25, 23, 59)), "created", id="autumn-inside"),
        pytest.param(at(2026, 10, 5, 8, 0), samples.exam(eid="d2", start=at(2026, 10, 26, 0, 0), end=at(2026, 10, 26, 0, 45)), "outside_window", id="autumn-first-minute-outside"),
        # summer time starts 29.03.2026: window of 09.03. ends 30.03. 00:00 (CEST); "21 * 24 h" would end an hour late
        pytest.param(at(2026, 3, 9, 8, 0), samples.exam(eid="d3", start=at(2026, 3, 29, 23, 30), end=at(2026, 3, 29, 23, 59)), "created", id="spring-inside"),
        pytest.param(at(2026, 3, 9, 8, 0), samples.exam(eid="d4", start=at(2026, 3, 30, 0, 30), end=at(2026, 3, 30, 1, 15)), "outside_window", id="spring-half-past-midnight-outside"),
    ],
)
def test_the_window_end_is_midnight_in_vienna_also_across_a_time_change(world: World, today, item, expected: str):
    world.webuntis.set_items([item])
    out = world.start(at=today)
    assert out.exit_code == 0
    if expected == "created":
        event_for(world.google, item.uid)
    else:
        assert world.google.events == {}
        assert skipped(out.report, Source.WEBUNTIS, "outside_window") >= 1


def test_adapters_are_asked_for_exactly_the_window_of_that_day(world: World):
    world.start()
    window, _tracked, now = world.webuntis.calls[0]
    assert same_instant(window.start, at(2026, 10, 14, 0, 0)) and same_instant(window.end, at(2026, 11, 4, 0, 0))
    assert now == NOW
    assert world.eduvidual.calls[0][0] == window


def test_the_window_across_the_autumn_time_change_is_a_calendar_window(world: World):
    world.start(at=at(2026, 10, 5, 8, 0))
    window = world.webuntis.calls[0][0]
    assert same_instant(window.start, at(2026, 10, 5, 0, 0)) and same_instant(window.end, at(2026, 10, 26, 0, 0))


def test_the_window_slides_with_the_day_so_an_item_outside_today_is_picked_up_tomorrow(world: World):
    item = samples.assignment(cmid="77", due=at(2026, 11, 4, 12, 0))
    world.eduvidual.set_items([item])
    world.start(at=at(2026, 10, 14, 8, 0))
    assert world.google.events == {}  # 04.11. is outside the window of 14.10.

    world.start(at=at(2026, 10, 15, 8, 0))  # window now ends 05.11. 00:00

    event_for(world.google, item.uid)


def test_an_item_without_a_date_is_not_created_but_listed_in_the_report(world: World):
    notice = Notice(
        Severity.WARNING, "no_date", "Eintrag ohne Datum übersprungen.", Source.EDUVIDUAL, title="Projektwoche Referat"
    )
    world.eduvidual.set_notices([notice])

    out = world.start()

    assert world.google.events == {} and world.google.tasks == []
    assert any(n.code == "no_date" and n.title == "Projektwoche Referat" for n in out.report.notices)
    # the user sees it: the rendered report names the entry and says that it was left out (a notice that never reaches the HTML
    # would not be "im Bericht aufgefuehrt"). That the REAL adapters produce this notice instead of an entry is pinned in
    # test_eduvidual / test_webuntis and, through runner and engine, in test_integration_stack.
    html = read_report(out)
    assert "Projektwoche Referat" in html and "Eintrag ohne Datum übersprungen." in html
