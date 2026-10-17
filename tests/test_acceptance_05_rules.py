"""Acceptance tests for ANFORDERUNGEN section 5 (exact calendar and task rules).

Section 10 bullet 6 (3-/7-day lead, late discovery) and bullet 7 (DST, missing times).
Everything is checked from the outside: what ends up in Google Calendar "Schule" and Google Tasks "Schule".

Sample run: Wed 14.10.2026 08:00 Europe/Vienna.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta

import pytest

from schulsync import textblocks
from schulsync.google_types import APP_PROP, PROP_KIND, PROP_SOURCE, PROP_UID, TASK_MARKER_PREFIX
from schulsync.models import Kind, Notice, Severity, Source
from schulsync.timeutil import UTC, VIENNA
from tests.support import samples
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    EXAM,
    HOMEWORK,
    LESSON,
    World,
    at,
    description_of,
    due_of,
    event_for,
    event_span,
    exam_start_only,
    notice_codes,
    same_instant,
    task_for,
    tasks_for,
    world,  # noqa: F401  (fixture)
)

D = date
ONE_MINUTE = timedelta(minutes=1)
STANDARD = (ASSIGNMENT, HOMEWORK, EXAM, LESSON)


def _adapter(world: World, item):
    return world.eduvidual if item.source == Source.EDUVIDUAL else world.webuntis


def _run_one(world: World, item, *, now: datetime | None = None):
    _adapter(world, item).set_items([item])
    return world.start(at=now)


# --------------------------------------------------------------------------- the table of section 5, row by row


def test_eduvidual_assignment_with_a_time_is_a_one_minute_deadline_marker_and_a_task_three_days_before(world: World):
    _run_one(world, ASSIGNMENT)

    ev = event_for(world.google, ASSIGNMENT.uid)
    assert ev["summary"].startswith("Abgabe:") and ASSIGNMENT.title in ev["summary"]
    start, end = event_span(ev)
    assert same_instant(start, at(2026, 10, 21, 23, 59))  # the real deadline
    assert end.astimezone(UTC) - start.astimezone(UTC) == ONE_MINUTE  # only a display length
    assert ev["start"]["timeZone"] == ev["end"]["timeZone"] == "Europe/Vienna"
    assert "21.10.2026 23:59" in description_of(ev)  # the minute does not hide the real deadline

    task = task_for(world.google, ASSIGNMENT.uid)
    assert task["title"] == f"Bearbeiten: {ASSIGNMENT.title}"
    assert task["status"] == "needsAction"
    assert due_of(task) == D(2026, 10, 18)  # work start, not the deadline
    assert "21.10.2026 23:59" in task["notes"]  # the real deadline incl. time is in the task text


def test_webuntis_homework_without_a_time_is_an_all_day_event_with_exclusive_end_and_a_task(world: World):
    item = samples.homework(hid="900", due=D(2026, 10, 30))
    _run_one(world, item)

    ev = event_for(world.google, item.uid)
    assert ev["summary"].startswith("Abgabe:") and item.title in ev["summary"]
    assert ev["start"] == {"date": "2026-10-30"} and ev["end"] == {"date": "2026-10-31"}
    task = task_for(world.google, item.uid)
    assert task["title"] == f"Bearbeiten: {item.title}" and due_of(task) == D(2026, 10, 27)


def test_exam_keeps_its_real_start_and_end_and_gets_a_learning_task_seven_days_before(world: World):
    _run_one(world, EXAM)

    ev = event_for(world.google, EXAM.uid)
    assert EXAM.title in ev["summary"]
    start, end = event_span(ev)
    assert same_instant(start, at(2026, 10, 28, 8, 0)) and same_instant(end, at(2026, 10, 28, 9, 40))
    task = task_for(world.google, EXAM.uid)
    assert task["title"] == f"Lernen: {EXAM.title}"
    assert due_of(task) == D(2026, 10, 21)
    assert "28.10.2026 08:00" in task["notes"]


def test_exam_with_only_a_known_start_is_a_one_minute_marker_that_says_the_end_is_unknown(world: World):
    item = exam_start_only(at(2026, 10, 22, 9, 0))
    _run_one(world, item)

    ev = event_for(world.google, item.uid)
    start, end = event_span(ev)
    assert same_instant(start, at(2026, 10, 22, 9, 0)) and end.astimezone(UTC) - start.astimezone(UTC) == ONE_MINUTE
    assert "Endzeit unbekannt" in description_of(ev)
    assert task_for(world.google, item.uid)["title"] == f"Lernen: {item.title}"
    assert due_of(task_for(world.google, item.uid)) == D(2026, 10, 15)


def test_cancelled_lesson_is_an_entfall_event_with_exact_times_and_no_task(world: World):
    _run_one(world, LESSON)

    ev = event_for(world.google, LESSON.uid)
    assert ev["summary"].startswith("Entfall:") and LESSON.title in ev["summary"]
    start, end = event_span(ev)
    assert same_instant(start, at(2026, 10, 15, 10, 15)) and same_instant(end, at(2026, 10, 15, 11, 55))
    assert world.google.tasks == []  # an Entfall has no task
    assert world.google.writes("insert_task") == []
    text = description_of(ev)
    assert LESSON.teacher in text and LESSON.room in text  # available teacher/room data is copied


# --------------------------------------------------------------------------- reminders, markers, sections


def _standard_run(world: World):
    world.load_standard()
    out = world.start()
    assert out.exit_code == 0
    return out


def test_every_calendar_category_has_exactly_one_popup_reminder_24_hours_before(world: World):
    _standard_run(world)
    for item in STANDARD:
        ev = event_for(world.google, item.uid)
        assert ev["reminders"]["useDefault"] is False, item.uid
        assert ev["reminders"]["overrides"] == [{"method": "popup", "minutes": 1440}], item.uid


def test_calendar_entries_carry_the_private_project_properties(world: World):
    _standard_run(world)
    for item in STANDARD:
        private = event_for(world.google, item.uid)["extendedProperties"]["private"]
        assert private[APP_PROP] == "1" and private[PROP_UID] == item.uid
        assert private[PROP_SOURCE] == item.source.value and private[PROP_KIND] == item.kind.value


def test_descriptions_and_task_notes_have_the_automatic_and_the_personal_section_and_tasks_the_marker(world: World):
    _standard_run(world)
    texts = [description_of(event_for(world.google, i.uid)) for i in STANDARD]
    texts += [task_for(world.google, i.uid)["notes"] for i in (ASSIGNMENT, HOMEWORK, EXAM)]
    for text in texts:
        begin, end, personal = (text.find(m) for m in (textblocks.BEGIN_TEXT, textblocks.END_TEXT, textblocks.PERSONAL_HEADER))
        assert 0 <= begin < end < personal, text
    for item in (ASSIGNMENT, HOMEWORK, EXAM):
        assert f"{TASK_MARKER_PREFIX}{item.uid}" in task_for(world.google, item.uid)["notes"]


# --------------------------------------------------------------------------- 3-/7-day lead and the "today" rule

_LEAD_CASES = [
    # normal lead: calendar days before the deadline date
    ("assignment-3-days", samples.assignment(cmid="11", due=at(2026, 10, 21, 23, 59)), D(2026, 10, 18)),
    ("assignment-date-only", samples.assignment(cmid="12", due=D(2026, 10, 30)), D(2026, 10, 27)),
    ("homework-3-days", samples.homework(hid="11", due=D(2026, 10, 30)), D(2026, 10, 27)),
    ("homework-with-time", samples.homework(hid="12", due=at(2026, 10, 29, 14, 0)), D(2026, 10, 26)),
    ("exam-7-days", samples.exam(eid="11"), D(2026, 10, 21)),
    ("exam-date-only", samples.exam(eid="12", start=D(2026, 11, 3), end=None), D(2026, 10, 27)),
    # lead exactly reaches today: still the computed date
    ("assignment-lead-ends-today", samples.assignment(cmid="13", due=at(2026, 10, 17, 12, 0)), D(2026, 10, 14)),
    ("exam-lead-ends-today", samples.exam(eid="13", start=at(2026, 10, 21, 8, 0), end=at(2026, 10, 21, 9, 0)), D(2026, 10, 14)),
    ("assignment-one-day-more", samples.assignment(cmid="14", due=at(2026, 10, 18, 12, 0)), D(2026, 10, 15)),
    # late discovery: work start already in the past while the deadline is still ahead -> today
    ("homework-late", samples.homework(hid="13", due=D(2026, 10, 16)), D(2026, 10, 14)),
    ("assignment-tomorrow", samples.assignment(cmid="15", due=at(2026, 10, 15, 12, 0)), D(2026, 10, 14)),
    ("assignment-today-evening", samples.assignment(cmid="16", due=at(2026, 10, 14, 23, 59)), D(2026, 10, 14)),
    ("exam-in-5-days", samples.exam(eid="14", start=at(2026, 10, 19, 8, 0), end=at(2026, 10, 19, 9, 40)), D(2026, 10, 14)),
    ("exam-tomorrow", samples.exam(eid="15", start=at(2026, 10, 15, 8, 0), end=at(2026, 10, 15, 9, 40)), D(2026, 10, 14)),
]


@pytest.mark.parametrize(("item", "expected_due"), [pytest.param(i, d, id=n) for n, i, d in _LEAD_CASES])
def test_task_date_is_the_deadline_minus_calendar_days_but_never_before_today(world: World, item, expected_due: date):
    _run_one(world, item)
    task = task_for(world.google, item.uid)
    prefix = "Lernen: " if item.kind == Kind.EXAM else "Bearbeiten: "
    assert task["title"] == prefix + item.title
    assert due_of(task) == expected_due
    assert task["due"] == f"{expected_due.isoformat()}T00:00:00.000Z"  # Tasks knows a date only


_DST_LEAD_CASES = [
    # spring: 3 * 24 h before Mon 30.03. 00:30 CEST would be Thu 26.03. (23:30 CET) - three CALENDAR days is Fri 27.03.
    (at(2026, 3, 20, 8, 0), samples.assignment(cmid="21", due=at(2026, 3, 30, 0, 30)), D(2026, 3, 27)),
    # spring: 7 * 24 h before Mon 30.03. 00:30 would be Sun 22.03. (23:30 CET) - seven calendar days is Mon 23.03.
    (at(2026, 3, 20, 8, 0), samples.exam(eid="21", start=at(2026, 3, 30, 0, 30), end=at(2026, 3, 30, 1, 15)), D(2026, 3, 23)),
    # autumn: 3 * 24 h before Sun 25.10. 23:30 CET would be Fri 23.10. (00:30 CEST) - three calendar days is Thu 22.10.
    (at(2026, 10, 20, 8, 0), samples.assignment(cmid="22", due=at(2026, 10, 25, 23, 30)), D(2026, 10, 22)),
]


@pytest.mark.parametrize(
    ("now", "item", "expected_due"),
    [pytest.param(n, i, d, id=f"{i.kind.value}-{i.when.start:%d.%m.}") for n, i, d in _DST_LEAD_CASES],
)
def test_lead_time_counts_calendar_days_not_hours_across_a_time_change(world: World, now, item, expected_due: date):
    _run_one(world, item, now=now)
    assert due_of(task_for(world.google, item.uid)) == expected_due


def test_lead_times_and_reminder_come_from_the_configuration(world: World):
    world.configure(rules={"assignment_lead_days": 5, "exam_lead_days": 10, "reminder_minutes": 60})
    world.eduvidual.set_items([ASSIGNMENT])
    world.webuntis.set_items([EXAM])
    world.start()
    assert due_of(task_for(world.google, ASSIGNMENT.uid)) == D(2026, 10, 16)  # 21.10. - 5
    assert due_of(task_for(world.google, EXAM.uid)) == D(2026, 10, 18)  # 28.10. - 10
    assert event_for(world.google, EXAM.uid)["reminders"]["overrides"] == [{"method": "popup", "minutes": 60}]


# --------------------------------------------------------------------------- time zones, DST, all-day


@pytest.mark.parametrize(
    ("now", "deadline"),
    [
        pytest.param(at(2026, 3, 20, 8, 0), at(2026, 3, 29, 1, 30), id="spring-before-the-change-CET"),
        pytest.param(at(2026, 3, 20, 8, 0), at(2026, 3, 29, 3, 30), id="spring-after-the-change-CEST"),
        pytest.param(at(2026, 10, 20, 8, 0), at(2026, 10, 25, 1, 30), id="autumn-before-the-change-CEST"),
        pytest.param(at(2026, 10, 20, 8, 0), at(2026, 10, 25, 3, 30), id="autumn-after-the-change-CET"),
    ],
)
def test_times_around_a_time_change_are_sent_as_the_exact_instant_with_the_vienna_zone(world: World, now, deadline):
    item = samples.assignment(cmid="31", due=deadline)
    _run_one(world, item, now=now)
    ev = event_for(world.google, item.uid)
    start, _end = event_span(ev)  # raises if the offset is missing
    assert same_instant(start, deadline)
    assert ev["start"]["timeZone"] == "Europe/Vienna"


@pytest.mark.parametrize("fold", [0, 1], ids=["first-02:59-CEST", "second-02:59-CET"])
def test_the_one_minute_marker_in_the_repeated_autumn_hour_lasts_one_real_minute(world: World, fold: int):
    deadline = datetime(2026, 10, 25, 2, 59, tzinfo=VIENNA, fold=fold)
    item = samples.assignment(cmid="32", due=deadline)
    _run_one(world, item, now=at(2026, 10, 20, 8, 0))
    start, end = event_span(event_for(world.google, item.uid))
    assert same_instant(start, deadline)
    assert end.astimezone(UTC) - start.astimezone(UTC) == ONE_MINUTE


@pytest.mark.parametrize(
    ("now", "day", "exclusive_end"),
    [
        pytest.param(at(2026, 10, 20, 8, 0), D(2026, 10, 25), "2026-10-26", id="25-hour-day"),
        pytest.param(at(2026, 3, 20, 8, 0), D(2026, 3, 29), "2026-03-30", id="23-hour-day"),
        pytest.param(at(2026, 12, 20, 8, 0), D(2026, 12, 31), "2027-01-01", id="year-end"),
        pytest.param(at(2027, 2, 20, 8, 0), D(2027, 2, 28), "2027-03-01", id="february-non-leap"),
    ],
)
def test_all_day_events_have_the_correct_exclusive_end_date(world: World, now, day: date, exclusive_end: str):
    item = samples.homework(hid="41", due=day)
    _run_one(world, item, now=now)
    ev = event_for(world.google, item.uid)
    assert ev["start"] == {"date": day.isoformat()} and ev["end"] == {"date": exclusive_end}


def test_a_date_without_time_is_not_turned_into_a_timed_event_in_any_category(world: World):
    items = [
        samples.assignment(cmid="51", due=D(2026, 10, 22)),
        samples.homework(hid="51", due=D(2026, 10, 22)),
        samples.exam(eid="51", start=D(2026, 10, 22), end=None),
    ]
    world.eduvidual.set_items([items[0]])
    world.webuntis.set_items(items[1:])
    world.start()
    for item in items:
        ev = event_for(world.google, item.uid)
        assert ev["start"] == {"date": "2026-10-22"} and ev["end"] == {"date": "2026-10-23"}, item.uid


# --------------------------------------------------------------------------- copy texts unaltered, no invention


def test_title_course_description_teacher_room_and_link_are_copied_unaltered(world: World):
    item = dataclasses.replace(
        ASSIGNMENT,
        source_id="assign:40999",
        title='Übung „Schaltungen“ & Co: Größe < 5 "ok"',
        course="Elektronik & Messtechnik 3AHEL",
        description="Zeile 1: Messwerte äöüß\nZeile 2: Protokoll als PDF",
        url="https://www.eduvidual.at/mod/assign/view.php?id=40999",
        teacher="Dipl.-Ing. Beispiel",
        room="E 1.02",
    )
    _run_one(world, item)

    ev = event_for(world.google, item.uid)
    text = description_of(ev)
    assert item.title in ev["summary"]
    for part in (item.course, "Zeile 1: Messwerte äöüß", "Zeile 2: Protokoll als PDF", item.url, item.teacher, item.room):
        assert part in text, part
    task = task_for(world.google, item.uid)
    assert task["title"] == f"Bearbeiten: {item.title}"
    for part in (item.course, "Zeile 1: Messwerte äöüß", item.url):
        assert part in task["notes"], part


def test_unsafe_or_secret_bearing_links_are_not_copied_but_the_entry_is_still_created(world: World):
    cases = {
        "81": "javascript:alert(1)",
        "82": "https://www.eduvidual.at/pluginfile.php/55/mod_assign/intro/blatt.pdf?token=ABCSECRET123&forcedownload=1",
        "83": "https://schueler:geheim-dummy@www.eduvidual.at/mod/assign/view.php?id=83",
    }
    items = [
        dataclasses.replace(ASSIGNMENT, source_id=f"assign:{k}", title=f"Laborprotokoll {k}", url=v)  # distinct titles: no duplicates
        for k, v in cases.items()
    ]
    world.eduvidual.set_items(items)
    world.start()
    for item in items:
        # the entry exists although its link is refused (the title proves it was not dropped with the link)
        assert item.title in event_for(world.google, item.uid)["summary"]
        text = description_of(event_for(world.google, item.uid)) + task_for(world.google, item.uid)["notes"]
        for forbidden in ("javascript:", "ABCSECRET123", "geheim-dummy"):
            assert forbidden not in text, (item.url, forbidden)


def test_harmless_links_are_copied_unchanged_and_a_secret_parameter_is_removed_from_an_otherwise_good_link(world: World):
    """The positive controls of the test above: a check that only looks for absent substrings would also pass if every link
    were dropped. A plain link is kept verbatim; from a link with a secret parameter only that parameter goes."""
    plain = "https://www.eduvidual.at/mod/assign/view.php?id=84&action=view"
    mixed = "https://www.eduvidual.at/mod/assign/view.php?id=85&sesskey=ZZSECRET99&action=view"
    items = [
        dataclasses.replace(ASSIGNMENT, source_id="assign:84", title="Laborprotokoll 84", url=plain),
        dataclasses.replace(ASSIGNMENT, source_id="assign:85", title="Laborprotokoll 85", url=mixed),
    ]
    world.eduvidual.set_items(items)
    world.start()

    for item, expected in zip(items, (plain, "https://www.eduvidual.at/mod/assign/view.php?id=85&action=view"), strict=True):
        assert f"Link: {expected}" in description_of(event_for(world.google, item.uid)), item.url
        assert f"Link: {expected}" in task_for(world.google, item.uid)["notes"], item.url
        assert "ZZSECRET99" not in description_of(event_for(world.google, item.uid)) + task_for(world.google, item.uid)["notes"]


def test_missing_optional_details_are_left_out_not_invented(world: World):
    item = dataclasses.replace(
        ASSIGNMENT, source_id="assign:60", course=None, description=None, url=None, teacher=None, room=None, notes=()
    )
    _run_one(world, item)
    text = description_of(event_for(world.google, item.uid)) + task_for(world.google, item.uid)["notes"]
    assert "None" not in text and "null" not in text.lower()


def test_incomplete_data_is_flagged_in_the_entry_and_reported_not_invented(world: World):
    note = "Titel in der Quelle abgeschnitten; Detailansicht nicht lesbar."
    item = dataclasses.replace(ASSIGNMENT, source_id="assign:61", title="Laborprotokoll 3: Frequenzgang des Tief", notes=(note,))
    world.eduvidual.set_notices(
        [Notice(Severity.WARNING, "incomplete_data", "Titel konnte nicht vollständig gelesen werden.", Source.EDUVIDUAL, title=item.title)]
    )
    out = _run_one(world, item)

    assert event_for(world.google, item.uid)["summary"].endswith(item.title)  # kept as read, not completed by guessing
    assert note in description_of(event_for(world.google, item.uid))
    assert "incomplete_data" in notice_codes(out.report)


def test_a_task_is_created_once_at_discovery_for_every_task_kind_and_never_for_a_lesson(world: World):
    _standard_run(world)
    assert sorted(t["title"] for t in world.google.tasks) == sorted(
        [f"Bearbeiten: {ASSIGNMENT.title}", f"Bearbeiten: {HOMEWORK.title}", f"Lernen: {EXAM.title}"]
    )
    assert tasks_for(world.google, LESSON.uid) == []
