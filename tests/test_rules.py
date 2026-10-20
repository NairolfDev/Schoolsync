"""Tests for rules.py: window/verdict, lead time, desired event/task text, decision tables 4.1 and 4.2.

All data is synthetic (fantasy names, plausible Austrian HTL subjects). Tests prove the rules, not the
real Eduvidual/WebUntis behaviour.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from schulsync import rules
from schulsync import textblocks as tb
from schulsync.config import RulesConfig
from schulsync.google_types import APP_PROP, PROP_KIND, PROP_SOURCE, PROP_UID
from schulsync.models import (
    Action,
    EventState,
    ItemState,
    ItemStatus,
    ItemTime,
    Kind,
    Source,
    SourceItem,
    TaskState,
)
from schulsync.rules import EventAction, TaskAction
from schulsync.timeutil import UTC, VIENNA, fetch_window, local_midnight

CFG = RulesConfig(assignment_lead_days=3, assignment_calendar=True)  # the 3-day / calendar behaviour; new default: see test_assignment_tasks_only.py
NOW = datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA)  # Wednesday
TODAY = NOW.date()


def dt(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=VIENNA)


def mk(kind=Kind.HOMEWORK, start=None, end=None, **kw) -> SourceItem:
    source = Source.EDUVIDUAL if kind == Kind.ASSIGNMENT else Source.WEBUNTIS
    if start is None:
        start = dt(2026, 10, 20, 23, 59) if kind in (Kind.ASSIGNMENT, Kind.HOMEWORK) else dt(2026, 10, 22, 8, 0)
    kw.setdefault("source_id", "1001")
    kw.setdefault("title", "Übungsblatt 3: Rekursion")
    return SourceItem(source=source, kind=kind, when=ItemTime(start, end), **kw)


def mk_state(item: SourceItem, **kw) -> ItemState:
    kw.setdefault("item_hash", item.content_hash())
    kw.setdefault("cancelled", item.status == ItemStatus.CANCELLED)
    kw.setdefault("first_seen_at", NOW)
    kw.setdefault("last_seen_at", NOW)
    return ItemState(item=item, **kw)


def active_event_state(item: SourceItem, *, applied_cancelled=False, **kw) -> ItemState:
    applied = rules.build_desired_event(item, CFG, cancelled=applied_cancelled)
    return mk_state(
        item,
        event_id="ss123",
        event_state=EventState.ACTIVE,
        event_applied_hash=applied.content_hash(),
        applied_cancelled=applied_cancelled,
        **kw,
    )


def open_task_state(item: SourceItem, due: date, *, cfg: RulesConfig = CFG, **kw) -> ItemState:
    desired = rules.build_desired_task(item, cfg, due)
    return mk_state(
        item,
        task_id="t1",
        task_state=TaskState.OPEN,
        task_applied_hash=desired.content_hash(),
        task_base_deadline=rules.deadline_iso(item),
        task_due=due,
        **kw,
    )


# =========================================================================== lead / deadline / past


def test_lead_days_per_kind_and_custom_config():
    assert rules.lead_days(Kind.ASSIGNMENT, CFG) == 3
    assert rules.lead_days(Kind.HOMEWORK, CFG) == 3
    assert rules.lead_days(Kind.EXAM, CFG) == 7
    assert rules.lead_days(Kind.CANCELLATION, CFG) is None
    c = RulesConfig(assignment_lead_days=2, homework_lead_days=4, exam_lead_days=10)
    assert [rules.lead_days(k, c) for k in (Kind.ASSIGNMENT, Kind.HOMEWORK, Kind.EXAM)] == [2, 4, 10]


def test_deadline_of_returns_vienna_datetime_or_date():
    from datetime import timezone

    item = mk(start=datetime(2026, 10, 20, 21, 59, tzinfo=timezone.utc))
    assert rules.deadline_of(item) == dt(2026, 10, 20, 23, 59)
    assert rules.deadline_of(item).utcoffset() == timedelta(hours=2)
    d = mk(start=date(2026, 10, 20))
    assert rules.deadline_of(d) == date(2026, 10, 20)
    assert rules.deadline_iso(item) == "2026-10-20T23:59:00+02:00"
    assert rules.deadline_iso(d) == "2026-10-20"


@pytest.mark.parametrize(
    "kind,start,end,past",
    [
        # deadline kinds: datetime < now
        (Kind.HOMEWORK, dt(2026, 10, 14, 7, 59), None, True),
        (Kind.HOMEWORK, dt(2026, 10, 14, 8, 0), None, False),  # exactly now: not past
        (Kind.ASSIGNMENT, dt(2026, 10, 14, 8, 1), None, False),
        # deadline kinds: date < today
        (Kind.HOMEWORK, date(2026, 10, 13), None, True),
        (Kind.HOMEWORK, date(2026, 10, 14), None, False),  # date-only today stays alive all day
        (Kind.ASSIGNMENT, date(2026, 10, 15), None, False),
        # exam/cancellation with known end: end <= now
        (Kind.EXAM, dt(2026, 10, 14, 6, 0), dt(2026, 10, 14, 8, 0), True),
        (Kind.EXAM, dt(2026, 10, 14, 6, 0), dt(2026, 10, 14, 8, 1), False),
        (Kind.CANCELLATION, dt(2026, 10, 14, 7, 0), dt(2026, 10, 14, 7, 50), True),
        (Kind.CANCELLATION, dt(2026, 10, 14, 9, 0), dt(2026, 10, 14, 9, 50), False),
        # exam/cancellation start only: start < now
        (Kind.EXAM, dt(2026, 10, 14, 7, 59), None, True),
        (Kind.EXAM, dt(2026, 10, 14, 8, 0), None, False),
        (Kind.CANCELLATION, dt(2026, 10, 14, 7, 0), None, True),
        # date only
        (Kind.EXAM, date(2026, 10, 13), None, True),
        (Kind.EXAM, date(2026, 10, 14), None, False),
    ],
)
def test_is_fully_past(kind, start, end, past):
    assert rules.is_fully_past(mk(kind, start, end), NOW) is past


def test_is_fully_past_rejects_naive_now():
    with pytest.raises(ValueError):
        rules.is_fully_past(mk(), datetime(2026, 10, 14, 8, 0))


def test_exam_with_end_not_after_start_is_treated_as_end_unknown():
    item = mk(Kind.EXAM, dt(2026, 10, 22, 8, 0), dt(2026, 10, 22, 8, 0))
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    assert ev.end == rules.add_real_minutes(dt(2026, 10, 22, 8, 0), 1)
    assert "Endzeit unbekannt" in ev.managed_text


# =========================================================================== compute_task_due


@pytest.mark.parametrize(
    "deadline,lead,today,expected",
    [
        (dt(2026, 10, 20, 23, 59), 3, date(2026, 10, 14), date(2026, 10, 17)),
        (date(2026, 10, 20), 3, date(2026, 10, 14), date(2026, 10, 17)),
        (dt(2026, 10, 26, 8, 0), 7, date(2026, 10, 14), date(2026, 10, 19)),  # across 25.10. (DST end)
        (date(2026, 10, 26), 7, date(2026, 10, 14), date(2026, 10, 19)),
        (date(2026, 3, 30), 3, date(2026, 3, 20), date(2026, 3, 27)),  # across 29.03. (DST start)
        (dt(2026, 3, 30, 0, 30), 3, date(2026, 3, 20), date(2026, 3, 27)),  # 72h would give 26.03.
        (dt(2026, 10, 26, 0, 30), 3, date(2026, 10, 1), date(2026, 10, 23)),
        (date(2026, 10, 15), 3, date(2026, 10, 14), date(2026, 10, 14)),  # late discovery -> today
        (date(2026, 10, 14), 7, date(2026, 10, 14), date(2026, 10, 14)),  # deadline today
        (date(2026, 10, 17), 3, date(2026, 10, 14), date(2026, 10, 14)),  # work start == today
        (date(2026, 10, 18), 3, date(2026, 10, 14), date(2026, 10, 15)),
        (date(2026, 1, 2), 3, date(2025, 12, 1), date(2025, 12, 30)),  # year boundary
    ],
)
def test_compute_task_due(deadline, lead, today, expected):
    assert rules.compute_task_due(deadline, lead, today) == expected


def test_compute_task_due_uses_vienna_calendar_date_of_utc_deadline():
    from datetime import timezone

    # 22:30 UTC on 19.10. is 00:30 on 20.10. in Vienna -> minus 3 = 17.10.
    assert rules.compute_task_due(datetime(2026, 10, 19, 22, 30, tzinfo=timezone.utc), 3, date(2026, 10, 1)) == date(
        2026, 10, 17
    )


# =========================================================================== new_item_verdict (window edges)


def _verdict(item, now, days=21):
    return rules.new_item_verdict(item, now, fetch_window(now, days))


def test_verdict_ok_expired_outside_cancelled():
    assert _verdict(mk(start=dt(2026, 10, 20, 12, 0)), NOW) == (True, "ok")
    assert _verdict(mk(start=dt(2026, 10, 14, 7, 0)), NOW) == (False, "expired")
    assert _verdict(mk(start=dt(2026, 12, 24, 12, 0)), NOW) == (False, "outside_window")
    assert _verdict(mk(start=dt(2026, 10, 20, 12, 0), status=ItemStatus.CANCELLED), NOW) == (False, "already_cancelled")


def test_verdict_cancelled_wins_over_expired():
    assert _verdict(mk(start=dt(2026, 10, 1, 12, 0), status=ItemStatus.CANCELLED), NOW) == (False, "already_cancelled")


def test_window_edges_timed_item_21_days():
    midnight = dt(2026, 10, 14, 0, 0)  # window start; "now" exactly at it so it is not expired
    assert _verdict(mk(start=midnight), midnight) == (True, "ok")  # exactly today 00:00 is in
    end = dt(2026, 11, 4, 0, 0)  # today + 21 days 00:00 exclusive
    assert _verdict(mk(start=end), NOW) == (False, "outside_window")
    assert _verdict(mk(start=dt(2026, 11, 3, 23, 59)), NOW) == (True, "ok")  # day+20 23:59
    assert _verdict(mk(start=dt(2026, 11, 4, 0, 1)), NOW) == (False, "outside_window")


def test_window_edges_date_only_item_21_days():
    assert _verdict(mk(start=date(2026, 11, 3)), NOW) == (True, "ok")  # day+20
    assert _verdict(mk(start=date(2026, 11, 4)), NOW) == (False, "outside_window")  # day+21
    assert _verdict(mk(start=date(2026, 10, 14)), NOW) == (True, "ok")  # today, date-only
    assert _verdict(mk(start=date(2026, 10, 13)), NOW) == (False, "expired")


def test_window_edges_around_dst_start_use_calendar_midnights():
    now = dt(2026, 3, 10, 8, 0)  # window end = 2026-03-31 00:00 (after the 29.03. switch)
    assert _verdict(mk(start=date(2026, 3, 30)), now) == (True, "ok")
    assert _verdict(mk(start=date(2026, 3, 31)), now) == (False, "outside_window")
    assert _verdict(mk(start=dt(2026, 3, 30, 23, 59)), now) == (True, "ok")
    assert _verdict(mk(start=dt(2026, 3, 31, 0, 0)), now) == (False, "outside_window")


def test_window_edges_around_dst_end():
    now = dt(2026, 10, 5, 8, 0)  # window end = 2026-10-26 00:00 (after the 25.10. switch)
    assert _verdict(mk(start=date(2026, 10, 25)), now) == (True, "ok")
    assert _verdict(mk(start=date(2026, 10, 26)), now) == (False, "outside_window")
    assert _verdict(mk(start=dt(2026, 10, 25, 23, 59)), now) == (True, "ok")


def test_exam_interval_overlapping_window_edges_counts_as_inside():
    # starts before the window end and ends after it
    straddle_end = mk(Kind.EXAM, dt(2026, 11, 3, 23, 0), dt(2026, 11, 4, 1, 0))
    assert _verdict(straddle_end, NOW) == (True, "ok")
    starts_at_window_end = mk(Kind.EXAM, dt(2026, 11, 4, 0, 0), dt(2026, 11, 4, 1, 0))
    assert _verdict(starts_at_window_end, NOW) == (False, "outside_window")
    # ended before now -> expired; still running at now (started before today 00:00) -> inside
    now = dt(2026, 10, 14, 0, 30)
    running = mk(Kind.EXAM, dt(2026, 10, 13, 23, 0), dt(2026, 10, 14, 1, 0))
    assert _verdict(running, now) == (True, "ok")


def test_expired_deadline_earlier_today_timed_vs_date_only():
    assert _verdict(mk(start=dt(2026, 10, 14, 7, 30)), NOW) == (False, "expired")
    assert _verdict(mk(start=date(2026, 10, 14)), NOW) == (True, "ok")


def test_window_days_is_configurable():
    assert _verdict(mk(start=date(2026, 10, 20)), NOW, days=5) == (False, "outside_window")
    assert _verdict(mk(start=date(2026, 10, 18)), NOW, days=5) == (True, "ok")


def test_cancellation_item_verdicts():
    lesson = mk(Kind.CANCELLATION, dt(2026, 10, 15, 9, 50), dt(2026, 10, 15, 10, 40))
    assert _verdict(lesson, NOW) == (True, "ok")
    past = mk(Kind.CANCELLATION, dt(2026, 10, 14, 6, 50), dt(2026, 10, 14, 7, 40))
    assert _verdict(past, NOW) == (False, "expired")


# =========================================================================== build_desired_event


def test_assignment_timed_is_one_minute_marker_with_real_deadline_in_text():
    item = mk(Kind.ASSIGNMENT, dt(2026, 10, 14, 23, 59), course="4AHITS Softwareentwicklung")
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    assert ev.summary == "Abgabe: Übungsblatt 3: Rekursion"
    assert ev.start == dt(2026, 10, 14, 23, 59)
    assert (ev.end.astimezone(UTC) - ev.start.astimezone(UTC)) == timedelta(minutes=1)
    assert not ev.all_day
    assert "Frist: Mi., 14.10.2026 23:59" in ev.managed_text
    assert "Kurs: 4AHITS Softwareentwicklung" in ev.managed_text
    assert ev.managed_text.splitlines()[-1] == "Quelle: Eduvidual"
    assert "Endzeit unbekannt" not in ev.managed_text


def test_one_minute_marker_is_real_minutes_across_dst_jump():
    item = mk(start=dt(2026, 3, 29, 1, 59, ))
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    assert ev.start == datetime(2026, 3, 29, 1, 59, tzinfo=VIENNA)
    assert ev.end.hour == 3 and ev.end.minute == 0  # wall clock jumps 02:00 -> 03:00
    assert ev.end.astimezone(UTC) - ev.start.astimezone(UTC) == timedelta(minutes=1)


@pytest.mark.parametrize(
    "d,next_day",
    [
        (date(2026, 10, 14), date(2026, 10, 15)),
        (date(2026, 10, 31), date(2026, 11, 1)),
        (date(2026, 12, 31), date(2027, 1, 1)),
        (date(2026, 3, 28), date(2026, 3, 29)),
        (date(2026, 3, 29), date(2026, 3, 30)),
        (date(2026, 10, 25), date(2026, 10, 26)),
    ],
)
def test_all_day_deadline_has_exclusive_end_date(d, next_day):
    ev = rules.build_desired_event(mk(start=d), CFG, cancelled=False)
    assert ev.all_day
    assert ev.start == d and ev.end == next_day
    assert "Frist: " in ev.managed_text and ":" not in ev.managed_text.splitlines()[0].split("2026")[-1]


def test_exam_with_real_start_and_end():
    item = mk(Kind.EXAM, dt(2026, 10, 22, 8, 0), dt(2026, 10, 22, 9, 40), subtype="Schularbeit", title="Mathematik 3. SA", room="B2.04", teacher="Mag. Muster")
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    assert ev.summary == "Schularbeit: Mathematik 3. SA"
    assert (ev.start, ev.end) == (dt(2026, 10, 22, 8, 0), dt(2026, 10, 22, 9, 40))
    assert "Termin: Do., 22.10.2026 08:00 bis 09:40" in ev.managed_text
    assert "Endzeit unbekannt" not in ev.managed_text
    assert "Lehrkraft: Mag. Muster" in ev.managed_text
    assert "Raum: B2.04" in ev.managed_text
    assert "Fach: " not in ev.managed_text  # no course given
    assert ev.location == "B2.04"
    assert ev.managed_text.splitlines()[-1] == "Quelle: WebUntis"


def test_exam_without_subtype_uses_pruefung_prefix():
    ev = rules.build_desired_event(mk(Kind.EXAM, subtype=None, title="Physik"), CFG, cancelled=False)
    assert ev.summary == "Prüfung: Physik"


def test_exam_multi_day_range_text():
    item = mk(Kind.EXAM, dt(2026, 10, 22, 22, 0), dt(2026, 10, 23, 1, 0))
    assert "Termin: Do., 22.10.2026 22:00 bis Fr., 23.10.2026 01:00" in rules.build_desired_event(item, CFG, cancelled=False).managed_text


def test_exam_with_only_start_is_marker_and_says_endzeit_unbekannt():
    item = mk(Kind.EXAM, dt(2026, 10, 22, 8, 0), None)
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    assert ev.end.astimezone(UTC) - ev.start.astimezone(UTC) == timedelta(minutes=1)
    assert "Endzeit unbekannt" in ev.managed_text


def test_endzeit_unbekannt_not_duplicated_when_adapter_already_noted_it():
    item = mk(Kind.EXAM, dt(2026, 10, 22, 8, 0), None, notes=("Endzeit unbekannt",))
    text = rules.build_desired_event(item, CFG, cancelled=False).managed_text
    assert text.count("Endzeit unbekannt") == 1


def test_exam_date_only_is_all_day_without_endzeit_hint():
    ev = rules.build_desired_event(mk(Kind.EXAM, date(2026, 10, 22)), CFG, cancelled=False)
    assert ev.all_day and ev.end == date(2026, 10, 23)
    assert "Endzeit unbekannt" not in ev.managed_text
    assert "Termin: Do., 22.10.2026" in ev.managed_text


def test_cancellation_event_uses_real_lesson_times():
    item = mk(Kind.CANCELLATION, dt(2026, 10, 15, 9, 50), dt(2026, 10, 15, 10, 40), title="Netzwerktechnik", course="NWT", room="E1.12")
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    assert ev.summary == "Entfall: Netzwerktechnik"
    assert (ev.start, ev.end) == (dt(2026, 10, 15, 9, 50), dt(2026, 10, 15, 10, 40))
    assert ev.popup_minutes == 1440
    assert "Fach: NWT" in ev.managed_text


def test_cancelled_event_has_prefix_no_popup_and_status_line():
    item = mk(Kind.EXAM, subtype="Test", title="Elektronik")
    ev = rules.build_desired_event(item, CFG, cancelled=True)
    assert ev.summary == "Abgesagt: Test: Elektronik"
    assert ev.popup_minutes is None
    assert ev.cancelled is True
    assert ev.managed_text.splitlines()[0].startswith("Status: ")
    assert "abgesagt" in ev.managed_text.splitlines()[0]
    normal = rules.build_desired_event(item, CFG, cancelled=False)
    assert normal.cancelled is False and normal.popup_minutes == 1440


def test_popup_follows_config():
    ev = rules.build_desired_event(mk(), RulesConfig(reminder_minutes=60), cancelled=False)
    assert ev.popup_minutes == 60
    assert rules.build_desired_event(mk(), RulesConfig(reminder_minutes=0), cancelled=False).popup_minutes is None


def test_private_props():
    item = mk(Kind.ASSIGNMENT, source_id="777")
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    assert ev.private_props == {
        APP_PROP: "1",
        PROP_UID: "eduvidual:assignment:777",
        PROP_SOURCE: "eduvidual",
        PROP_KIND: "assignment",
    }
    assert rules.build_desired_event(item, CFG, cancelled=True).private_props == ev.private_props


def test_text_contains_description_teacher_room_hints_and_link():
    item = mk(
        Kind.ASSIGNMENT,
        description="Bitte als PDF abgeben.\n\n\n  Programmcode   beilegen.  ",
        url="https://www.eduvidual.at/mod/assign/view.php?id=4711",
        notes=("Titel war abgeschnitten (unvollständig)",),
        teacher="Prof. Beispiel",
        room="Online",
    )
    text = rules.build_desired_event(item, CFG, cancelled=False).managed_text
    assert "Beschreibung:\nBitte als PDF abgeben.\n\nProgrammcode beilegen." in text
    assert "Link: https://www.eduvidual.at/mod/assign/view.php?id=4711" in text
    assert "Hinweis: Titel war abgeschnitten (unvollständig)" in text
    assert "Lehrkraft: Prof. Beispiel" in text and "Raum: Online" in text
    lines = text.splitlines()
    assert lines.index("Raum: Online") < lines.index("Beschreibung:") < lines.index(
        "Link: https://www.eduvidual.at/mod/assign/view.php?id=4711"
    ) < len(lines) - 1


def test_single_line_description_inline():
    text = rules.build_desired_event(mk(description="Kapitel 4 lesen"), CFG, cancelled=False).managed_text
    assert "Beschreibung: Kapitel 4 lesen" in text


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://files.example.org/x",
        "mailto:lehrer@example.org",
        "file:///C:/Users/x/secret.txt",
        "data:text/html,<b>x</b>",
        "//example.org/x",
        "/mod/assign/view.php?id=1",
        "https://",
        "https://exa mple.org/a b",
        "",
        "  ",
    ],
)
def test_links_other_than_http_https_are_dropped(url):
    text = rules.build_desired_event(mk(url=url), CFG, cancelled=False).managed_text
    assert "Link:" not in text
    assert "javascript" not in text


def test_http_and_https_links_are_kept():
    for url in ("http://schule.example.org/a", "HTTPS://Schule.example.org/a?x=1&y=2"):
        assert f"Link: {url}" in rules.build_desired_event(mk(url=url), CFG, cancelled=False).managed_text


def test_titles_unicode_whitespace_and_prefix():
    item = mk(title="  Übung\t7   –  Größe/Maße  ✓ 数学  ")
    assert rules.build_desired_event(item, CFG, cancelled=False).summary == "Abgabe: Übung 7 – Größe/Maße ✓ 数学"


def test_very_long_title_is_clipped_for_event_and_task():
    item = mk(title="Lang " * 600)
    ev = rules.build_desired_event(item, CFG, cancelled=True)
    assert len(ev.summary) <= 1024 and ev.summary.startswith("Abgesagt: Abgabe: Lang") and ev.summary.endswith("[… gekürzt]")
    task = rules.build_desired_task(item, CFG, date(2026, 10, 17))
    assert len(task.title) <= 1024 and task.title.startswith("Bearbeiten: Lang") and "gekürzt" in task.title


def test_very_long_description_is_clipped_before_merge():
    text = rules.build_desired_event(mk(description="x" * 20000), CFG, cancelled=False).managed_text
    assert len(text) < 3300 and "[… gekürzt]" in text and text.splitlines()[-1] == "Quelle: WebUntis"


def test_empty_title_gets_placeholder():
    assert rules.build_desired_event(mk(title="  "), CFG, cancelled=False).summary == "Abgabe: (ohne Titel)"


def test_content_hash_is_stable_and_sensitive():
    a = rules.build_desired_event(mk(), CFG, cancelled=False)
    b = rules.build_desired_event(mk(), CFG, cancelled=False)
    assert a.content_hash() == b.content_hash()
    assert a.content_hash() != rules.build_desired_event(mk(start=dt(2026, 10, 21, 23, 59)), CFG, cancelled=False).content_hash()
    assert a.content_hash() != rules.build_desired_event(mk(), CFG, cancelled=True).content_hash()


def test_managed_text_survives_a_merge_roundtrip_with_personal_notes():
    ev = rules.build_desired_event(mk(description="Zeile 1\nZeile 2"), CFG, cancelled=False)
    first = tb.merge_description(None, ev.managed_text)
    with_notes = first + "\nmeine Notiz"
    ev2 = rules.build_desired_event(mk(description="Zeile 1 neu"), CFG, cancelled=False)
    merged = tb.merge_description(with_notes, ev2.managed_text)
    assert merged.endswith("\nmeine Notiz") and "Zeile 1 neu" in merged and "Zeile 2" not in merged


# =========================================================================== build_desired_task


def test_task_text_names_real_deadline_workstart_and_lead():
    item = mk(Kind.ASSIGNMENT, dt(2026, 10, 15, 23, 59), course="4AHITS Softwareentwicklung", url="https://www.eduvidual.at/mod/assign/view.php?id=1")
    task = rules.build_desired_task(item, CFG, date(2026, 10, 14))
    assert task.title == "Bearbeiten: Übungsblatt 3: Rekursion"
    assert task.due == date(2026, 10, 14)
    lines = task.managed_text.splitlines()
    assert lines[0] == "Frist: Do., 15.10.2026 23:59"
    assert lines[1] == "Arbeitsbeginn: Mo., 12.10.2026 (3 Tage vor der Frist)"
    assert any(line.startswith("Aufgabendatum in Google Tasks: Mi., 14.10.2026") for line in lines)  # late discovery
    assert "Kurs: 4AHITS Softwareentwicklung" in lines
    assert "Link: https://www.eduvidual.at/mod/assign/view.php?id=1" in lines
    assert lines[-1] == "schulsync-id: eduvidual:assignment:1001"
    assert tb.find_task_marker(task.managed_text) == item.uid


def test_task_without_late_discovery_has_no_extra_line():
    item = mk(Kind.HOMEWORK, date(2026, 10, 20))
    task = rules.build_desired_task(item, CFG, date(2026, 10, 17))
    assert task.managed_text.splitlines()[:2] == ["Frist: Di., 20.10.2026", "Arbeitsbeginn: Sa., 17.10.2026 (3 Tage vor der Frist)"]
    assert "Aufgabendatum" not in task.managed_text


def test_exam_task_title_and_text():
    item = mk(Kind.EXAM, dt(2026, 10, 26, 8, 0), dt(2026, 10, 26, 9, 40), subtype="Schularbeit", title="Mathematik")
    task = rules.build_desired_task(item, CFG, date(2026, 10, 19))
    assert task.title == "Lernen: Mathematik"
    assert task.managed_text.splitlines()[0] == "Termin: Mo., 26.10.2026 08:00 bis 09:40"
    assert task.managed_text.splitlines()[1] == "Arbeitsbeginn: Mo., 19.10.2026 (7 Tage vor dem Termin)"


def test_task_lead_wording_singular_and_zero():
    one = rules.build_desired_task(mk(Kind.HOMEWORK, date(2026, 10, 20)), RulesConfig(homework_lead_days=1), date(2026, 10, 19))
    assert "(1 Tag vor der Frist)" in one.managed_text
    zero = rules.build_desired_task(mk(Kind.HOMEWORK, date(2026, 10, 20)), RulesConfig(homework_lead_days=0), date(2026, 10, 20))
    assert "(am Tag der Frist)" in zero.managed_text


def test_task_for_cancellation_is_a_programming_error():
    with pytest.raises(ValueError):
        rules.build_desired_task(mk(Kind.CANCELLATION), CFG, TODAY)


def test_task_hash_depends_on_due():
    item = mk()
    assert rules.build_desired_task(item, CFG, date(2026, 10, 17)).content_hash() != rules.build_desired_task(item, CFG, date(2026, 10, 18)).content_hash()


def test_task_notes_fit_limit_when_merged_and_marker_survives_clipping():
    item = mk(description="y" * 20000)
    task = rules.build_desired_task(item, CFG, date(2026, 10, 17))
    notes = tb.merge_description(None, task.managed_text, limit=tb.TASK_NOTES_LIMIT, html=False)
    assert len(notes) <= tb.TASK_NOTES_LIMIT
    assert tb.find_task_marker(notes) == item.uid


# =========================================================================== plan_item: event table 4.1


def test_event_user_deleted_is_skipped_regardless_of_everything():
    item = mk()
    for cancelled in (False, True):
        plan = rules.plan_item(mk_state(item, event_state=EventState.USER_DELETED, cancelled=cancelled), NOW, CFG)
        assert plan.event.action == EventAction.SKIP and plan.event.reason == "user_deleted"
        assert plan.event.change is None and plan.event.desired is None


def test_event_user_deleted_stays_skipped_on_reactivation_and_changed_content():
    state = mk_state(mk(start=dt(2026, 10, 21, 23, 59)), event_state=EventState.USER_DELETED, applied_cancelled=True, event_applied_hash="old")
    assert rules.plan_item(state, NOW, CFG).event.action == EventAction.SKIP


def test_event_none_cancelled_skip():
    plan = rules.plan_item(mk_state(mk(status=ItemStatus.CANCELLED)), NOW, CFG).event
    assert (plan.action, plan.reason, plan.desired) == (EventAction.SKIP, "already_cancelled", None)


def test_event_none_expired_skip():
    plan = rules.plan_item(mk_state(mk(start=dt(2026, 10, 14, 7, 0))), NOW, CFG).event
    assert (plan.action, plan.reason) == (EventAction.SKIP, "expired")


def test_event_none_cancelled_beats_expired():
    plan = rules.plan_item(mk_state(mk(start=dt(2026, 10, 1, 7, 0), status=ItemStatus.CANCELLED)), NOW, CFG).event
    assert plan.reason == "already_cancelled"


def test_event_none_create():
    item = mk(Kind.ASSIGNMENT)
    plan = rules.plan_item(mk_state(item), NOW, CFG).event
    assert plan.action == EventAction.CREATE and plan.change == Action.CREATE and plan.reason == "new"
    assert plan.desired == rules.build_desired_event(item, CFG, cancelled=False)


def test_event_none_create_ignores_window_for_known_items():
    far = mk(start=dt(2027, 3, 1, 12, 0))  # outside the 21-day window but already tracked: update paths must work
    assert rules.plan_item(mk_state(far), NOW, CFG).event.action == EventAction.CREATE


def test_event_active_unchanged():
    item = mk()
    plan = rules.plan_item(active_event_state(item), NOW, CFG).event
    assert (plan.action, plan.change, plan.reason) == (EventAction.NONE, None, "unchanged")


def test_event_active_unchanged_even_when_fully_past():
    item = mk(start=dt(2026, 10, 1, 12, 0))
    assert rules.plan_item(active_event_state(item), NOW, CFG).event.action == EventAction.NONE


def test_event_active_cancel():
    item = mk()
    state = replace(active_event_state(item), cancelled=True)
    plan = rules.plan_item(state, NOW, CFG).event
    assert (plan.action, plan.change, plan.reason) == (EventAction.UPDATE, Action.CANCEL, "cancelled")
    assert plan.desired.cancelled and plan.desired.popup_minutes is None and plan.desired.summary.startswith("Abgesagt: ")


def test_event_active_reactivate():
    item = mk()
    state = active_event_state(item, applied_cancelled=True)  # last written as cancelled, source says active again
    plan = rules.plan_item(replace(state, cancelled=False), NOW, CFG).event
    assert (plan.action, plan.change, plan.reason) == (EventAction.UPDATE, Action.REACTIVATE, "reactivated")
    assert not plan.desired.cancelled and plan.desired.popup_minutes == 1440


def test_event_active_update_moved_deadline():
    old = mk(start=dt(2026, 10, 20, 23, 59))
    state = replace(active_event_state(old), item=mk(start=dt(2026, 10, 22, 23, 59)))
    plan = rules.plan_item(state, NOW, CFG).event
    assert (plan.action, plan.change, plan.reason) == (EventAction.UPDATE, Action.UPDATE, "changed")
    assert plan.desired.start == dt(2026, 10, 22, 23, 59)


def test_event_active_cancelled_and_applied_cancelled_but_text_changed_is_plain_update():
    old = mk(title="Alt")
    state = replace(active_event_state(old, applied_cancelled=True), item=mk(title="Neu"), cancelled=True)
    plan = rules.plan_item(state, NOW, CFG).event
    assert (plan.action, plan.change) == (EventAction.UPDATE, Action.UPDATE)


def test_event_active_cancelled_and_applied_cancelled_unchanged_is_none():
    item = mk(status=ItemStatus.CANCELLED)
    assert rules.plan_item(active_event_state(item, applied_cancelled=True), NOW, CFG).event.action == EventAction.NONE


def test_event_active_missing_applied_hash_updates():
    state = replace(active_event_state(mk()), event_applied_hash=None)
    assert rules.plan_item(state, NOW, CFG).event.action == EventAction.UPDATE


def test_event_update_for_item_far_outside_window_still_planned():
    old = mk(start=dt(2027, 1, 20, 23, 59))
    state = replace(active_event_state(old), item=mk(start=dt(2027, 1, 22, 23, 59)))
    assert rules.plan_item(state, NOW, CFG).event.action == EventAction.UPDATE


# =========================================================================== plan_item: task table 4.2


def test_task_done_is_never_touched_even_if_deadline_changes_or_cancelled():
    old = mk(start=dt(2026, 10, 20, 23, 59))
    new = mk(start=dt(2026, 10, 25, 23, 59))
    for cancelled in (False, True):
        state = mk_state(new, task_state=TaskState.DONE, task_base_deadline=rules.deadline_iso(old), task_due=date(2026, 10, 17), task_applied_hash="x", cancelled=cancelled)
        plan = rules.plan_item(state, NOW, CFG).task
        assert (plan.action, plan.reason, plan.desired) == (TaskAction.NONE, "done_untouched", None)


def test_task_user_deleted_respected_even_on_reactivation_and_new_deadline():
    state = mk_state(mk(), task_state=TaskState.USER_DELETED, task_removed_reason="user")
    plan = rules.plan_item(state, NOW, CFG).task
    assert (plan.action, plan.reason) == (TaskAction.NONE, "user_deleted")
    state = mk_state(mk(status=ItemStatus.CANCELLED), task_state=TaskState.USER_DELETED)
    assert rules.plan_item(state, NOW, CFG).task.action == TaskAction.NONE


def test_task_removed_and_cancelled_stays_none():
    state = mk_state(mk(status=ItemStatus.CANCELLED), task_state=TaskState.REMOVED, task_removed_reason="cancelled")
    plan = rules.plan_item(state, NOW, CFG).task
    assert (plan.action, plan.reason) == (TaskAction.NONE, "removed_cancelled")


def test_task_removed_and_reactivated_is_created_with_fresh_due():
    # work start would have been 17.10.; deadline 20.10. -> fresh due is max(17.10., today)
    state = mk_state(mk(start=dt(2026, 10, 20, 23, 59)), task_state=TaskState.REMOVED, task_removed_reason="cancelled", task_due=date(2026, 10, 5))
    plan = rules.plan_item(state, NOW, CFG).task
    assert plan.action == TaskAction.CREATE and plan.reason == "reactivated"
    assert plan.due == date(2026, 10, 17) and plan.update_due is True
    assert plan.base_deadline == "2026-10-20T23:59:00+02:00"
    # late reactivation: due becomes today
    late = mk_state(mk(start=dt(2026, 10, 15, 23, 59)), task_state=TaskState.REMOVED, task_due=date(2026, 10, 5))
    p2 = rules.plan_item(late, NOW, CFG).task
    assert p2.action == TaskAction.CREATE and p2.due == TODAY


def test_task_removed_reactivated_but_fully_past_stays_none():
    state = mk_state(mk(start=dt(2026, 10, 13, 12, 0)), task_state=TaskState.REMOVED)
    assert rules.plan_item(state, NOW, CFG).task.action == TaskAction.NONE


def test_task_none_cancelled_or_past_creates_nothing():
    cancelled = rules.plan_item(mk_state(mk(status=ItemStatus.CANCELLED)), NOW, CFG).task
    assert (cancelled.action, cancelled.reason) == (TaskAction.NONE, "already_cancelled")
    past = rules.plan_item(mk_state(mk(start=dt(2026, 10, 14, 7, 59))), NOW, CFG).task
    assert (past.action, past.reason) == (TaskAction.NONE, "expired")


@pytest.mark.parametrize(
    "kind,start,expected_due,title_prefix",
    [
        (Kind.ASSIGNMENT, dt(2026, 10, 20, 23, 59), date(2026, 10, 17), "Bearbeiten: "),
        (Kind.HOMEWORK, date(2026, 10, 20), date(2026, 10, 17), "Bearbeiten: "),
        (Kind.EXAM, dt(2026, 10, 26, 8, 0), date(2026, 10, 19), "Lernen: "),  # 7 days across DST end
        (Kind.EXAM, date(2026, 10, 26), date(2026, 10, 19), "Lernen: "),
    ],
)
def test_task_none_create_with_lead(kind, start, expected_due, title_prefix):
    plan = rules.plan_item(mk_state(mk(kind, start)), NOW, CFG).task
    assert plan.action == TaskAction.CREATE and plan.reason == "new" and plan.update_due is True
    assert plan.due == expected_due == plan.desired.due
    assert plan.desired.title.startswith(title_prefix)
    assert plan.base_deadline == rules.deadline_iso(mk(kind, start))


def test_task_lead_across_dst_start_homework_due_march_30():
    now = dt(2026, 3, 20, 8, 0)
    plan = rules.plan_item(mk_state(mk(Kind.HOMEWORK, date(2026, 3, 30))), now, CFG).task
    assert plan.due == date(2026, 3, 27)
    timed = rules.plan_item(mk_state(mk(Kind.HOMEWORK, dt(2026, 3, 30, 0, 30))), now, CFG).task
    assert timed.due == date(2026, 3, 27)  # NOT 26.03. (72 hours would be wrong)


def test_task_lead_across_dst_end_exam_oct_26():
    plan = rules.plan_item(mk_state(mk(Kind.EXAM, dt(2026, 10, 26, 8, 0), dt(2026, 10, 26, 9, 40))), NOW, CFG).task
    assert plan.due == date(2026, 10, 19)


def test_task_late_discovery_sets_due_to_today():
    plan = rules.plan_item(mk_state(mk(Kind.HOMEWORK, dt(2026, 10, 15, 23, 59))), NOW, CFG).task  # work start 12.10.
    assert plan.action == TaskAction.CREATE and plan.due == TODAY
    assert "Arbeitsbeginn: Mo., 12.10.2026" in plan.desired.managed_text  # nominal start stays visible


def test_task_deadline_today_late_is_created_for_today():
    plan = rules.plan_item(mk_state(mk(Kind.HOMEWORK, dt(2026, 10, 14, 23, 59))), NOW, CFG).task
    assert plan.action == TaskAction.CREATE and plan.due == TODAY


def test_task_open_cancelled_is_removed():
    item = mk(status=ItemStatus.CANCELLED)
    plan = rules.plan_item(open_task_state(item, date(2026, 10, 17)), NOW, CFG).task
    assert (plan.action, plan.reason, plan.desired, plan.due) == (TaskAction.REMOVE, "cancelled", None, None)


def test_task_open_deadline_moved_updates_with_new_due():
    old = mk(start=dt(2026, 10, 20, 23, 59))
    state = replace(open_task_state(old, date(2026, 10, 17)), item=mk(start=dt(2026, 10, 27, 23, 59)))
    plan = rules.plan_item(state, NOW, CFG).task
    assert plan.action == TaskAction.UPDATE and plan.reason == "deadline_changed"
    assert plan.update_due is True and plan.due == date(2026, 10, 24) == plan.desired.due
    assert plan.base_deadline == "2026-10-27T23:59:00+01:00"  # CET after the switch
    assert "Frist: Di., 27.10.2026 23:59" in plan.desired.managed_text


def test_task_open_deadline_moved_to_sooner_with_past_work_start_uses_today():
    old = mk(start=dt(2026, 10, 25, 12, 0))
    state = replace(open_task_state(old, date(2026, 10, 22)), item=mk(start=dt(2026, 10, 15, 12, 0)))
    plan = rules.plan_item(state, NOW, CFG).task
    assert plan.action == TaskAction.UPDATE and plan.update_due and plan.due == TODAY


def test_task_open_deadline_moved_into_the_past_keeps_due():
    old = mk(start=dt(2026, 10, 20, 23, 59))
    state = replace(open_task_state(old, date(2026, 10, 17)), item=mk(start=dt(2026, 10, 10, 12, 0)))
    plan = rules.plan_item(state, NOW, CFG).task
    assert plan.action == TaskAction.UPDATE and plan.reason == "deadline_changed"
    assert plan.update_due is False and plan.due == date(2026, 10, 17)
    assert plan.desired.due == date(2026, 10, 17)
    assert plan.base_deadline == "2026-10-10T12:00:00+02:00"


def test_task_open_time_only_change_on_same_day_is_a_deadline_change():
    old = mk(start=dt(2026, 10, 20, 12, 0))
    state = replace(open_task_state(old, date(2026, 10, 17)), item=mk(start=dt(2026, 10, 20, 23, 59)))
    plan = rules.plan_item(state, NOW, CFG).task
    assert plan.action == TaskAction.UPDATE and plan.update_due and plan.due == date(2026, 10, 17)
    assert "23:59" in plan.desired.managed_text


def test_task_open_same_deadline_text_changed_updates_only_text():
    old = mk(title="Alt")
    state = replace(open_task_state(old, date(2026, 10, 17)), item=mk(title="Neuer Titel"))
    plan = rules.plan_item(state, NOW, CFG).task
    assert plan.action == TaskAction.UPDATE and plan.reason == "text_changed"
    assert plan.update_due is False and plan.due == date(2026, 10, 17)
    assert plan.desired.title == "Bearbeiten: Neuer Titel"


def test_task_open_unchanged_is_none():
    plan = rules.plan_item(open_task_state(mk(), date(2026, 10, 17)), NOW, CFG).task
    assert (plan.action, plan.reason, plan.desired) == (TaskAction.NONE, "unchanged", None)


@pytest.mark.parametrize("days_later", [0, 1, 2, 5, 40])
def test_task_overdue_unchanged_is_not_pushed_to_today(days_later):
    # Discovered late on 14.10. (due set to today); every later day the same OPEN task must stay NONE.
    item = mk(Kind.HOMEWORK, dt(2026, 10, 15, 23, 59))
    created = rules.plan_item(mk_state(item), NOW, CFG).task
    state = open_task_state(item, created.due)
    later = NOW + timedelta(days=days_later)
    assert rules.plan_item(state, later, CFG).task.action == TaskAction.NONE


def test_task_open_same_deadline_task_due_in_past_is_none():
    item = mk(Kind.HOMEWORK, dt(2026, 10, 20, 23, 59))
    state = open_task_state(item, date(2026, 10, 12))  # due long gone, deadline still ahead
    assert rules.plan_item(state, NOW, CFG).task.action == TaskAction.NONE
    expired = open_task_state(mk(Kind.HOMEWORK, dt(2026, 10, 10, 12, 0)), date(2026, 10, 7))
    assert rules.plan_item(expired, NOW, CFG).task.action == TaskAction.NONE  # overdue task untouched


def test_task_open_with_unknown_base_deadline_does_not_move_due():
    item = mk()
    state = replace(open_task_state(item, date(2026, 10, 17)), task_base_deadline=None)
    assert rules.plan_item(state, NOW, CFG).task.action == TaskAction.NONE
    changed = replace(state, item=mk(title="Neu"))
    plan = rules.plan_item(changed, NOW, CFG).task
    assert plan.action == TaskAction.UPDATE and plan.update_due is False and plan.base_deadline == rules.deadline_iso(changed.item)


def test_task_config_lead_change_updates_text_only():
    item = mk()
    state = open_task_state(item, date(2026, 10, 17))
    plan = rules.plan_item(state, NOW, RulesConfig(homework_lead_days=5)).task
    assert plan.action == TaskAction.UPDATE and plan.update_due is False and plan.due == date(2026, 10, 17)


# =========================================================================== cancellation kind / combined


def test_cancellation_has_event_but_never_a_task():
    item = mk(Kind.CANCELLATION, dt(2026, 10, 15, 9, 50), dt(2026, 10, 15, 10, 40))
    plan = rules.plan_item(mk_state(item), NOW, CFG)
    assert plan.event.action == EventAction.CREATE
    assert plan.task.action == TaskAction.NONE and plan.task.reason == "no_task_for_kind"
    state = mk_state(item, task_state=TaskState.OPEN)  # even inconsistent state never yields a task action
    assert rules.plan_item(state, NOW, CFG).task.action == TaskAction.NONE


def test_cancel_flow_event_update_and_task_removed_only_when_open():
    item = mk(Kind.HOMEWORK, status=ItemStatus.CANCELLED)
    open_state = replace(active_event_state(mk(Kind.HOMEWORK)), item=item, cancelled=True)
    open_state = replace(open_state, **{k: getattr(open_task_state(mk(Kind.HOMEWORK), date(2026, 10, 17)), k) for k in ("task_id", "task_state", "task_applied_hash", "task_base_deadline", "task_due")})
    plan = rules.plan_item(open_state, NOW, CFG)
    assert plan.event.change == Action.CANCEL and plan.task.action == TaskAction.REMOVE
    done = replace(open_state, task_state=TaskState.DONE)
    plan = rules.plan_item(done, NOW, CFG)
    assert plan.event.change == Action.CANCEL and plan.task.action == TaskAction.NONE


def test_reactivation_flow_event_reactivated_and_removed_task_recreated():
    item = mk(Kind.HOMEWORK)
    state = replace(
        active_event_state(item, applied_cancelled=True),
        task_state=TaskState.REMOVED,
        task_removed_reason="cancelled",
    )
    plan = rules.plan_item(state, NOW, CFG)
    assert plan.event.change == Action.REACTIVATE
    assert plan.task.action == TaskAction.CREATE


def test_plan_item_is_deterministic_and_does_not_mutate_state():
    state = open_task_state(mk(), date(2026, 10, 17))
    before = (state.task_due, state.task_applied_hash, state.event_state)
    a, b = rules.plan_item(state, NOW, CFG), rules.plan_item(state, NOW, CFG)
    assert a == b
    assert before == (state.task_due, state.task_applied_hash, state.event_state)


def test_local_midnight_helper_matches_window_start():  # guards the contract this module relies on
    assert fetch_window(NOW).start == local_midnight(TODAY)
    assert time(0, 0) == fetch_window(NOW).start.timetz().replace(tzinfo=None)


@pytest.mark.parametrize("name", ["sig", "ticket", "sid", "session", "pw", "credential", "jsessionid", "SIG"])
def test_short_secret_parameter_names_are_removed_as_whole_names_only(name):
    """Signed or session-bearing links must not reach a calendar that may be shared; `assignment=5`, `design=1` and
    `signal=3` contain `sig` but are no secrets and stay."""
    url = f"https://www.eduvidual.at/mod/assign/view.php?id=5&{name}=GEHEIM77&assignment=5&design=1&signal=3"
    text = rules.build_desired_event(mk(url=url), CFG, cancelled=False).managed_text
    assert "GEHEIM77" not in text
    assert "Link: https://www.eduvidual.at/mod/assign/view.php?id=5&assignment=5&design=1&signal=3\n" in text


def test_secret_query_params_and_fragment_are_stripped_other_params_kept():
    url = "https://www.eduvidual.at/mod/assign/view.php?id=5&sesskey=abcd1234&action=view#access_token=zzzz1234"
    text = rules.build_desired_event(mk(url=url), CFG, cancelled=False).managed_text
    assert "Link: https://www.eduvidual.at/mod/assign/view.php?id=5&action=view\n" in text
    assert "abcd1234" not in text and "zzzz1234" not in text
