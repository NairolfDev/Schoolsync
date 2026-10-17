"""Tests for the pure body builders / record readers (no network, no Google)."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from schulsync.google_bodies import (
    apply_patch,
    event_description,
    event_insert_body,
    event_is_deleted,
    event_patch_body,
    event_private_props,
    task_due_date,
    task_insert_body,
    task_is_completed,
    task_is_deleted,
    task_is_hidden,
    task_marker_uid,
    task_notes,
    task_patch_body,
)
from schulsync.google_types import TASK_MARKER_PREFIX, make_event_id
from schulsync.models import DesiredEvent, DesiredTask
from schulsync.timeutil import VIENNA, add_real_minutes

UID = "eduvidual:assignment:48213"
PROPS = {
    "schulsync": "1",
    "schulsync_uid": UID,
    "schulsync_source": "eduvidual",
    "schulsync_kind": "assignment",
}


def ev(start, end, **kw) -> DesiredEvent:
    base = dict(
        summary="Abgabe: Protokoll Messtechnik 3",
        managed_text="Frist: Freitag, 16.10.2026",
        start=start,
        end=end,
        location=None,
        popup_minutes=1440,
        private_props=PROPS,
    )
    base.update(kw)
    return DesiredEvent(**base)


ALL_DAY = ev(date(2026, 10, 16), date(2026, 10, 17))
TIMED = ev(datetime(2026, 10, 16, 23, 59, tzinfo=VIENNA), datetime(2026, 10, 17, 0, 0, tzinfo=VIENNA))


# --------------------------------------------------------------------------- event insert


def test_all_day_insert_uses_date_with_exclusive_end():
    body = event_insert_body(ALL_DAY, event_id=make_event_id(UID), description="Text")
    assert body["start"] == {"date": "2026-10-16"}
    assert body["end"] == {"date": "2026-10-17"}  # exclusive: day after
    assert body["id"] == make_event_id(UID)
    assert body["status"] == "confirmed"
    assert body["summary"] == "Abgabe: Protokoll Messtechnik 3"
    assert body["description"] == "Text"
    assert "location" not in body


def test_all_day_end_must_be_after_start():
    with pytest.raises(ValueError):
        event_insert_body(ev(date(2026, 10, 16), date(2026, 10, 16)), event_id="x" * 8, description="")


def test_timed_insert_has_offset_and_timezone():
    body = event_insert_body(TIMED, event_id="ss" + "a" * 8, description="")
    assert body["start"] == {"dateTime": "2026-10-16T23:59:00+02:00", "timeZone": "Europe/Vienna"}
    assert body["end"] == {"dateTime": "2026-10-17T00:00:00+02:00", "timeZone": "Europe/Vienna"}


@pytest.mark.parametrize(
    "when, offset",
    [
        (datetime(2026, 3, 28, 10, 0, tzinfo=VIENNA), "+01:00"),  # day before spring forward
        (datetime(2026, 3, 29, 10, 0, tzinfo=VIENNA), "+02:00"),  # spring forward day (02:00 -> 03:00)
        (datetime(2026, 10, 24, 10, 0, tzinfo=VIENNA), "+02:00"),  # day before fall back
        (datetime(2026, 10, 25, 10, 0, tzinfo=VIENNA), "+01:00"),  # fall back day (03:00 -> 02:00)
    ],
)
def test_timed_offsets_on_dst_days(when, offset):
    body = event_insert_body(ev(when, add_real_minutes(when, 45)), event_id="ss" + "b" * 8, description="")
    assert body["start"]["dateTime"].endswith(offset)
    assert body["end"]["dateTime"].endswith(offset)


def test_one_minute_marker_across_spring_forward_gap():
    start = datetime(2026, 3, 29, 1, 59, tzinfo=VIENNA)  # +01:00
    end = add_real_minutes(start, 1)  # really one minute later = 03:00 +02:00
    body = event_insert_body(ev(start, end), event_id="ss" + "c" * 8, description="")
    assert body["start"]["dateTime"] == "2026-03-29T01:59:00+01:00"
    assert body["end"]["dateTime"] == "2026-03-29T03:00:00+02:00"


def test_one_minute_marker_across_fall_back_overlap():
    start = datetime(2026, 10, 25, 2, 59, tzinfo=VIENNA, fold=0)  # first 02:59, +02:00
    end = add_real_minutes(start, 1)  # 02:00 +01:00 (second pass)
    body = event_insert_body(ev(start, end), event_id="ss" + "d" * 8, description="")
    assert body["start"]["dateTime"] == "2026-10-25T02:59:00+02:00"
    assert body["end"]["dateTime"] == "2026-10-25T02:00:00+01:00"


def test_timed_end_before_start_rejected():
    s = datetime(2026, 10, 16, 10, 0, tzinfo=VIENNA)
    with pytest.raises(ValueError):
        event_insert_body(ev(s, datetime(2026, 10, 16, 9, 0, tzinfo=VIENNA)), event_id="ss" + "e" * 8, description="")


def test_reminder_popup_and_cancelled_body_without_reminder():
    body = event_insert_body(ALL_DAY, event_id="ss" + "f" * 8, description="")
    assert body["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1440}]}
    cancelled = ev(
        ALL_DAY.start,
        ALL_DAY.end,
        summary="Abgesagt: Abgabe: Protokoll Messtechnik 3",
        popup_minutes=None,
        cancelled=True,
    )
    cb = event_insert_body(cancelled, event_id="ss" + "f" * 8, description="")
    assert cb["reminders"] == {"useDefault": False, "overrides": []}
    assert "popup" not in json.dumps(cb["reminders"])


def test_reminder_minutes_range_checked():
    with pytest.raises(ValueError):
        event_insert_body(ev(ALL_DAY.start, ALL_DAY.end, popup_minutes=40321), event_id="ss" + "g" * 8, description="")


def test_location_and_private_props():
    body = event_insert_body(ev(ALL_DAY.start, ALL_DAY.end, location="Raum E212"), event_id="ss" + "h" * 8, description="")
    assert body["location"] == "Raum E212"
    assert body["extendedProperties"] == {"private": PROPS}


def test_private_property_limits_enforced():
    with pytest.raises(ValueError):
        event_insert_body(ev(ALL_DAY.start, ALL_DAY.end, private_props={"k" * 45: "v"}), event_id="ss" + "i" * 8, description="")
    with pytest.raises(ValueError):
        event_insert_body(ev(ALL_DAY.start, ALL_DAY.end, private_props={"k": "v" * 1025}), event_id="ss" + "i" * 8, description="")


def test_bodies_are_json_serialisable():
    for d in (ALL_DAY, TIMED):
        json.dumps(event_insert_body(d, event_id="ss" + "j" * 8, description="ä ö ü"))
        json.dumps(event_patch_body(d, description="x"))
    json.dumps(task_insert_body(DesiredTask("t", "n", date(2026, 10, 13)), notes="n"))
    json.dumps(task_patch_body(DesiredTask("t", "n", date(2026, 10, 13)), notes="n", include_due=True))


# --------------------------------------------------------------------------- event patch


def test_patch_body_has_no_id_or_status_and_clears_location():
    body = event_patch_body(ALL_DAY, description="neu")
    assert "id" not in body and "status" not in body
    assert body["location"] is None  # null -> Google deletes the field
    assert body["description"] == "neu"
    assert body["extendedProperties"] == {"private": PROPS}


def test_patch_location_set_when_present():
    assert event_patch_body(ev(ALL_DAY.start, ALL_DAY.end, location="Aula"), description="")["location"] == "Aula"


def test_patch_to_timed_nulls_date():
    body = event_patch_body(TIMED, description="")
    assert body["start"] == {"dateTime": "2026-10-16T23:59:00+02:00", "timeZone": "Europe/Vienna", "date": None}
    assert body["end"]["date"] is None


def test_patch_to_all_day_nulls_datetime():
    body = event_patch_body(ALL_DAY, description="")
    assert body["start"] == {"date": "2026-10-16", "dateTime": None}
    assert body["end"] == {"date": "2026-10-17", "dateTime": None}


def test_patch_reminder_replaced_on_cancel_and_reactivate():
    cancelled = ev(ALL_DAY.start, ALL_DAY.end, popup_minutes=None, cancelled=True)
    assert event_patch_body(cancelled, description="")["reminders"] == {"useDefault": False, "overrides": []}
    assert event_patch_body(ALL_DAY, description="")["reminders"]["overrides"] == [{"method": "popup", "minutes": 1440}]


def test_apply_patch_switch_representation_yields_valid_start():
    existing = {"id": "x", "start": {"date": "2026-10-16"}, "end": {"date": "2026-10-17"}, "location": "Aula"}
    merged = apply_patch(existing, event_patch_body(TIMED, description="d"))
    assert merged["start"] == {"dateTime": "2026-10-16T23:59:00+02:00", "timeZone": "Europe/Vienna"}
    assert "location" not in merged and merged["id"] == "x"
    back = apply_patch(merged, event_patch_body(ALL_DAY, description="d"))
    assert back["start"]["date"] == "2026-10-16" and "dateTime" not in back["start"]


def test_apply_patch_merges_private_props_and_replaces_lists():
    existing = {"extendedProperties": {"private": {"mine": "1", "schulsync": "0"}}, "reminders": {"overrides": [{"minutes": 5}]}}
    merged = apply_patch(existing, event_patch_body(ALL_DAY, description=""))
    assert merged["extendedProperties"]["private"]["mine"] == "1"  # foreign key untouched
    assert merged["extendedProperties"]["private"]["schulsync"] == "1"
    assert merged["reminders"]["overrides"] == [{"method": "popup", "minutes": 1440}]


def test_apply_patch_does_not_mutate_inputs():
    base, patch = {"a": {"b": 1}}, {"a": {"b": None, "c": [1]}}
    assert apply_patch(base, patch) == {"a": {"c": [1]}}
    assert base == {"a": {"b": 1}} and patch == {"a": {"b": None, "c": [1]}}


# --------------------------------------------------------------------------- tasks


def test_task_insert_body_due_format_and_status():
    body = task_insert_body(DesiredTask("Bearbeiten: Protokoll", f"x\n{TASK_MARKER_PREFIX}{UID}", date(2026, 10, 13)), notes="N")
    assert body == {
        "title": "Bearbeiten: Protokoll",
        "notes": "N",
        "due": "2026-10-13T00:00:00.000Z",
        "status": "needsAction",
    }


def test_task_patch_never_touches_status_and_due_only_on_request():
    d = DesiredTask("Lernen: SA Mechanik", "m", date(2026, 10, 9))
    without = task_patch_body(d, notes="n", include_due=False)
    assert without == {"title": "Lernen: SA Mechanik", "notes": "n"}
    with_due = task_patch_body(d, notes="n", include_due=True)
    # EXACT: any extra field (a start time, a reminder, a status) invented by a later change must fail here (ANFORDERUNGEN 5:
    # "Erfinde kein zusaetzliches Startzeit- oder Erinnerungsfeld", 6: the user alone completes tasks)
    assert with_due == {"title": "Lernen: SA Mechanik", "notes": "n", "due": "2026-10-09T00:00:00.000Z"}
    assert "status" not in with_due and "status" not in without


# --------------------------------------------------------------------------- readers: realistic API samples

LIVE_EVENT = {
    "kind": "calendar#event",
    "etag": '"3417051123456000"',
    "id": make_event_id(UID),
    "status": "confirmed",
    "htmlLink": "https://www.google.com/calendar/event?eid=PLACEHOLDER",
    "summary": "Abgabe: Protokoll Messtechnik 3",
    "description": "Persönliche Notiz\n--- Automatisch übernommen ---\nFrist: Freitag",
    "start": {"date": "2026-10-16"},
    "end": {"date": "2026-10-17"},
    "extendedProperties": {"private": dict(PROPS)},
    "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1440}]},
}
TOMBSTONE = {"id": make_event_id(UID), "status": "cancelled"}


def test_event_readers_on_live_event():
    assert not event_is_deleted(LIVE_EVENT)
    assert event_description(LIVE_EVENT).startswith("Persönliche Notiz")
    assert event_private_props(LIVE_EVENT) == PROPS


def test_event_readers_on_tombstone():
    assert event_is_deleted(TOMBSTONE)
    assert event_description(TOMBSTONE) == ""
    assert event_private_props(TOMBSTONE) == {}


def test_event_private_props_returns_copy():
    props = event_private_props(LIVE_EVENT)
    props["x"] = "y"
    assert "x" not in LIVE_EVENT["extendedProperties"]["private"]


OPEN_TASK = {
    "kind": "tasks#task",
    "id": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTA",
    "etag": '"LTE5MjA="',
    "title": "Bearbeiten: Protokoll Messtechnik 3",
    "updated": "2026-10-14T06:01:12.000Z",
    "status": "needsAction",
    "due": "2026-10-13T00:00:00.000Z",
    "notes": f"Arbeitsbeginn: Dienstag, 13.10.2026\n{TASK_MARKER_PREFIX}{UID}\nQuelle: Eduvidual",
}
COMPLETED_TASK = {**OPEN_TASK, "status": "completed", "completed": "2026-10-15T17:30:00.000Z"}
HIDDEN_TASK = {**COMPLETED_TASK, "hidden": True}
DELETED_TASK = {**OPEN_TASK, "deleted": True}


def test_task_readers_open():
    assert task_due_date(OPEN_TASK) == date(2026, 10, 13)
    assert not task_is_completed(OPEN_TASK)
    assert not task_is_deleted(OPEN_TASK) and not task_is_hidden(OPEN_TASK)
    assert task_marker_uid(OPEN_TASK) == UID
    assert "Arbeitsbeginn" in task_notes(OPEN_TASK)


def test_task_readers_completed_hidden_deleted():
    assert task_is_completed(COMPLETED_TASK) and not task_is_hidden(COMPLETED_TASK)
    assert task_is_completed(HIDDEN_TASK) and task_is_hidden(HIDDEN_TASK)
    assert task_is_deleted(DELETED_TASK) and not task_is_completed(DELETED_TASK)
    assert task_marker_uid(DELETED_TASK) == UID


def test_task_completed_by_timestamp_only():
    assert task_is_completed({"completed": "2026-10-15T17:30:00.000Z"})


def test_task_due_missing_or_garbage():
    assert task_due_date({}) is None
    assert task_due_date({"due": "soon"}) is None
    assert task_due_date({"due": None}) is None


def test_task_due_uses_date_prefix_without_timezone_shift():
    assert task_due_date({"due": "2026-03-29T00:00:00.000Z"}) == date(2026, 3, 29)


def test_task_marker_variants():
    assert task_marker_uid({}) is None
    assert task_marker_uid({"notes": "nur Text"}) is None
    assert task_marker_uid({"notes": f"a\r\n  {TASK_MARKER_PREFIX}webuntis:homework:77  \r\nb"}) == "webuntis:homework:77"
    assert task_marker_uid({"notes": f"{TASK_MARKER_PREFIX}"}) is None
    # mention inside a sentence is not a marker line
    assert task_marker_uid({"notes": f"siehe {TASK_MARKER_PREFIX}foo"}) is None
    # first marker wins
    assert task_marker_uid({"notes": f"{TASK_MARKER_PREFIX}one\n{TASK_MARKER_PREFIX}two"}) == "one"
