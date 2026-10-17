"""Tests for the contract module timeutil.py (window edges, DST days, calendar-day arithmetic)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from schulsync import timeutil as tu
from schulsync.timeutil import UTC, VIENNA, Window


def dt(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=VIENNA)


def utc_seconds(a: datetime, b: datetime) -> float:
    """Real elapsed time (aware datetimes in the same tzinfo subtract by wall clock!)."""
    return (b.astimezone(UTC) - a.astimezone(UTC)).total_seconds()


# --------------------------------------------------------------------------- basics / naive rejection


def test_vienna_zone_and_now_vienna_is_aware():
    assert VIENNA.key == "Europe/Vienna"
    n = tu.now_vienna()
    assert n.tzinfo is not None and n.utcoffset() in (timedelta(hours=1), timedelta(hours=2))


def test_to_vienna_converts_and_rejects_naive():
    assert tu.to_vienna(datetime(2026, 7, 1, 10, 0, tzinfo=UTC)) == dt(2026, 7, 1, 12, 0)
    assert tu.to_vienna(datetime(2026, 1, 1, 10, 0, tzinfo=UTC)).hour == 11
    with pytest.raises(ValueError):
        tu.to_vienna(datetime(2026, 7, 1, 10, 0))


def test_today_vienna_naive_rejected():
    with pytest.raises(ValueError):
        tu.today_vienna(datetime(2026, 7, 1, 10, 0))


def test_window_overlaps_rejects_naive_datetime():
    w = tu.fetch_window(dt(2026, 10, 14, 8, 0))
    with pytest.raises(ValueError):
        w.overlaps(datetime(2026, 10, 15, 10, 0), None)


def test_window_contains_naive_datetime_is_an_error():
    w = tu.fetch_window(dt(2026, 10, 14, 8, 0))
    with pytest.raises((TypeError, ValueError)):  # comparison of naive and aware raises TypeError
        w.contains(datetime(2026, 10, 15, 10, 0))


def test_today_vienna_uses_vienna_day_not_utc_day():
    # 22:30 UTC on 14.10. is already 15.10. 00:30 in Vienna (CEST)
    assert tu.today_vienna(datetime(2026, 10, 14, 22, 30, tzinfo=UTC)) == date(2026, 10, 15)
    # winter: 23:30 UTC on 14.12. is 15.12. 00:30 (CET)
    assert tu.today_vienna(datetime(2026, 12, 14, 23, 30, tzinfo=UTC)) == date(2026, 12, 15)
    # 21:59 UTC on 14.10. is still 23:59 on 14.10.
    assert tu.today_vienna(datetime(2026, 10, 14, 21, 59, tzinfo=UTC)) == date(2026, 10, 14)
    # other zone input
    assert tu.today_vienna(datetime(2026, 10, 14, 18, 30, tzinfo=timezone(timedelta(hours=-8)))) == date(2026, 10, 15)


# --------------------------------------------------------------------------- midnight / DST days


def test_local_midnight_offsets_around_both_switches():
    assert tu.local_midnight(date(2026, 3, 29)).utcoffset() == timedelta(hours=1)  # still CET at 00:00
    assert tu.local_midnight(date(2026, 3, 30)).utcoffset() == timedelta(hours=2)
    assert tu.local_midnight(date(2026, 10, 25)).utcoffset() == timedelta(hours=2)  # still CEST at 00:00
    assert tu.local_midnight(date(2026, 10, 26)).utcoffset() == timedelta(hours=1)


def test_day_lengths_on_dst_days():
    def length(d: date) -> float:
        return utc_seconds(tu.local_midnight(d), tu.local_midnight(tu.plus_calendar_days(d, 1))) / 3600

    assert length(date(2026, 3, 29)) == 23  # spring forward
    assert length(date(2026, 10, 25)) == 25  # fall back
    assert length(date(2026, 10, 14)) == 24


# --------------------------------------------------------------------------- calendar-day arithmetic


@pytest.mark.parametrize(
    "d,n,expected",
    [
        (date(2026, 10, 26), 7, date(2026, 10, 19)),  # across the 25.10. switch
        (date(2026, 3, 30), 3, date(2026, 3, 27)),  # across the 29.03. switch
        (date(2026, 3, 1), 1, date(2026, 2, 28)),
        (date(2028, 3, 1), 1, date(2028, 2, 29)),  # leap year
        (date(2026, 1, 2), 3, date(2025, 12, 30)),
        (date(2026, 10, 14), 0, date(2026, 10, 14)),
    ],
)
def test_minus_calendar_days(d, n, expected):
    assert tu.minus_calendar_days(d, n) == expected
    assert tu.plus_calendar_days(expected, n) == d


def test_calendar_days_differ_from_fixed_hours_across_dst():
    deadline = dt(2026, 3, 30, 0, 30)
    by_hours = (deadline.astimezone(UTC) - timedelta(hours=72)).astimezone(VIENNA).date()
    assert by_hours == date(2026, 3, 26)  # the wrong way
    assert tu.minus_calendar_days(deadline.date(), 3) == date(2026, 3, 27)  # the right way


# --------------------------------------------------------------------------- make_local / add_real_minutes


def test_make_local_normal_time():
    assert tu.make_local(date(2026, 10, 14), time(23, 59)) == dt(2026, 10, 14, 23, 59)
    assert tu.make_local(date(2026, 10, 14), time(8, 0, tzinfo=UTC)).tzinfo is VIENNA  # tz of the time is replaced


def test_make_local_nonexistent_time_is_deterministic():
    # 02:30 does not exist on 29.03.2026; documented: interpreted with the offset before the switch.
    t = tu.make_local(date(2026, 3, 29), time(2, 30))
    assert t.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert t == tu.make_local(date(2026, 3, 29), time(2, 30))


def test_make_local_ambiguous_time_takes_first_occurrence():
    t = tu.make_local(date(2026, 10, 25), time(2, 30))
    assert t.utcoffset() == timedelta(hours=2)  # CEST, first occurrence (fold=0)


def test_add_real_minutes_spring_forward():
    start = dt(2026, 3, 29, 1, 59)
    end = tu.add_real_minutes(start, 1)
    assert (end.hour, end.minute) == (3, 0)  # wall clock jumps 02:00 -> 03:00
    assert end.utcoffset() == timedelta(hours=2)
    assert utc_seconds(start, end) == 60
    assert end.tzinfo is VIENNA


def test_add_real_minutes_fall_back():
    start = dt(2026, 10, 25, 2, 59)  # fold=0 -> CEST (first 02:59)
    end = tu.add_real_minutes(start, 1)
    assert (end.hour, end.minute) == (2, 0) and end.utcoffset() == timedelta(hours=1)  # second 02:00 (CET)
    assert utc_seconds(start, end) == 60
    # 120 real minutes from 01:30 CEST land on 02:30 CET
    later = tu.add_real_minutes(dt(2026, 10, 25, 1, 30), 120)
    assert (later.hour, later.minute) == (2, 30) and later.utcoffset() == timedelta(hours=1)


def test_add_real_minutes_plain_day_and_negative():
    assert tu.add_real_minutes(dt(2026, 10, 14, 23, 59), 1) == dt(2026, 10, 15, 0, 0)
    assert tu.add_real_minutes(dt(2026, 10, 14, 0, 0), -1) == dt(2026, 10, 13, 23, 59)


# --------------------------------------------------------------------------- fetch_window


def test_fetch_window_default_21_days_half_open():
    now = dt(2026, 10, 14, 8, 0)
    w = tu.fetch_window(now)
    assert w.start == dt(2026, 10, 14, 0, 0)
    assert w.end == dt(2026, 11, 4, 0, 0)
    # across the 25.10. switch the window is 21 days + 1 hour long in real time
    assert utc_seconds(w.start, w.end) == (21 * 24 + 1) * 3600


def test_fetch_window_days_parameter_and_spring_switch():
    w = tu.fetch_window(dt(2026, 3, 10, 8, 0), 21)
    assert w.end == dt(2026, 3, 31, 0, 0)
    assert utc_seconds(w.start, w.end) == (21 * 24 - 1) * 3600
    assert tu.fetch_window(dt(2026, 10, 14, 8, 0), 5).end == dt(2026, 10, 19, 0, 0)


def test_fetch_window_is_based_on_vienna_day():
    now = datetime(2026, 10, 14, 22, 30, tzinfo=UTC)  # = 15.10. 00:30 Vienna
    assert tu.fetch_window(now).start == dt(2026, 10, 15, 0, 0)


def test_fetch_window_without_now_starts_at_vienna_midnight():
    w = tu.fetch_window()
    assert w.start.timetz().replace(tzinfo=None) == time(0, 0) and w.end > w.start


# --------------------------------------------------------------------------- Window.contains


def _w() -> Window:
    return tu.fetch_window(dt(2026, 10, 14, 8, 0))  # [14.10. 00:00, 04.11. 00:00)


def test_contains_datetime_edges():
    w = _w()
    assert w.contains(dt(2026, 10, 14, 0, 0))  # start inclusive
    assert not w.contains(dt(2026, 10, 13, 23, 59))
    assert w.contains(dt(2026, 11, 3, 23, 59))
    assert not w.contains(dt(2026, 11, 4, 0, 0))  # end exclusive
    assert w.contains(datetime(2026, 11, 3, 22, 59, tzinfo=UTC))  # 23:59 Vienna (CET)
    assert not w.contains(datetime(2026, 11, 3, 23, 0, tzinfo=UTC))  # = 00:00 on 04.11. Vienna


def test_contains_date_means_start_of_day():
    w = _w()
    assert w.contains(date(2026, 10, 14))
    assert not w.contains(date(2026, 10, 13))
    assert w.contains(date(2026, 11, 3))  # day + 20
    assert not w.contains(date(2026, 11, 4))  # day + 21


# --------------------------------------------------------------------------- Window.overlaps


def test_overlaps_point_semantics_without_end():
    w = _w()
    assert w.overlaps(dt(2026, 10, 14, 0, 0), None)
    assert not w.overlaps(dt(2026, 10, 13, 23, 59), None)
    assert w.overlaps(dt(2026, 11, 3, 23, 59), None)
    assert not w.overlaps(dt(2026, 11, 4, 0, 0), None)


def test_overlaps_date_is_the_whole_day():
    w = _w()
    assert w.overlaps(date(2026, 10, 14), None)
    assert not w.overlaps(date(2026, 10, 13), None)  # day ends exactly at window start -> no overlap
    assert w.overlaps(date(2026, 11, 3), None)
    assert not w.overlaps(date(2026, 11, 4), None)


def test_overlaps_interval_straddling_edges():
    w = _w()
    assert w.overlaps(dt(2026, 10, 13, 23, 0), dt(2026, 10, 14, 1, 0))  # crosses window start
    assert w.overlaps(dt(2026, 11, 3, 23, 0), dt(2026, 11, 4, 1, 0))  # crosses window end
    assert w.overlaps(dt(2026, 10, 1, 0, 0), dt(2026, 12, 1, 0, 0))  # covers the whole window


def test_overlaps_interval_touching_edges_does_not_overlap():
    w = _w()
    assert not w.overlaps(dt(2026, 10, 13, 22, 0), dt(2026, 10, 14, 0, 0))  # ends exactly at start
    assert not w.overlaps(dt(2026, 11, 4, 0, 0), dt(2026, 11, 4, 1, 0))  # starts exactly at end


def test_overlaps_zero_or_negative_length_interval_is_a_point():
    w = _w()
    t = dt(2026, 10, 20, 12, 0)
    assert w.overlaps(t, t)
    assert w.overlaps(t, t - timedelta(hours=1))
    assert not w.overlaps(dt(2026, 11, 4, 0, 0), dt(2026, 11, 3, 0, 0))


def test_overlaps_handles_utc_inputs():
    w = _w()
    assert w.overlaps(datetime(2026, 10, 13, 22, 0, tzinfo=UTC), None)  # = 14.10. 00:00 Vienna
    assert not w.overlaps(datetime(2026, 10, 13, 21, 59, tzinfo=UTC), None)


def test_overlaps_date_start_with_end_ignores_end_documented_quirk():
    # `end` is ignored for a date start (the day is the interval). Callers must not rely on multi-day dates.
    w = _w()
    assert not w.overlaps(date(2026, 10, 13), dt(2026, 10, 20, 0, 0))


def test_slot_start_picks_latest_mark_including_dst_days():
    from schulsync.timeutil import slot_start

    times = ("06:00", "18:00")
    v = lambda *a: datetime(*a, tzinfo=VIENNA)
    assert slot_start(v(2026, 10, 14, 5, 59), times) == v(2026, 10, 14, 0, 0)
    assert slot_start(v(2026, 10, 14, 6, 0), times) == v(2026, 10, 14, 6, 0)
    assert slot_start(v(2026, 10, 14, 17, 59), times) == v(2026, 10, 14, 6, 0)
    assert slot_start(v(2026, 10, 25, 23, 0), times) == v(2026, 10, 25, 18, 0)  # day the clocks go back
