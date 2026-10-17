"""Adversarial review tests for src/schulsync/adapters/webuntis.py. Each test demonstrates one defect (all FAIL today)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from schulsync.adapters import webuntis as wu
from schulsync.models import Category, CategoryStatus, Kind
from schulsync.timeutil import VIENNA
from tests.support.fake_http import json_response, text_response
from tests.test_webuntis import (  # helpers only (no test_* names imported)
    NOW,
    World,
    adapter,
    by_kind,
    html_page,
    lesson,
    ref,
    rpc_err,
    rpc_ok,
    run,
)
from schulsync.timeutil import fetch_window


# --- 1. HIGH: plain-text homework containing "<...>" is silently mangled as HTML ------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Implementiere List<String> sortiere(List<String> in)",  # Java generics (4AHIT)
        "Zeige: a<b und c>d",  # inequalities
        "Wenn x<y dann (z>0) gilt",
    ],
)
def test_clean_text_keeps_plain_text_with_angle_brackets(raw):
    assert wu.clean_text(raw) == raw


def test_homework_description_and_title_keep_generics():
    w = World()
    text = "Methode List<Integer> filter(List<Integer> xs) schreiben"
    w.homework = lambda r: json_response(
        {"data": {"homeworks": [{"id": 77, "lessonId": 3301, "date": 20261012, "dueDate": 20261016, "text": text,
                                 "remark": "", "completed": False, "attachments": []}],
                  "lessons": [{"id": 3301, "subject": "POS"}]}}
    )
    item = by_kind(run(w), Kind.HOMEWORK)["77"]
    assert item.description == text and item.title == text


# --- 2. MEDIUM (DESIGN 8): exam without a date aborts the whole category instead of notice `no_date` --------


@pytest.mark.parametrize("exam", [
    {"id": 1, "examType": "Test", "name": "Ohne Datum", "startTime": 800, "endTime": 850, "subject": "AM"},
    {"id": 1, "examType": "Test", "name": "Datum 0", "examDate": 0, "startTime": 0, "endTime": 0, "subject": "AM"},
])
def test_exam_without_date_is_notice_not_category_failure(exam):
    w = World()
    good = {"id": 2, "examType": "Test", "name": "Gut", "examDate": 20261021, "startTime": 800, "endTime": 850,
            "subject": "AM", "teachers": [], "rooms": [], "text": ""}
    w.exam = lambda r: json_response({"data": {"exams": [exam, good]}})
    res = run(w)
    assert res.categories[Category.EXAMS].status == CategoryStatus.OK
    assert set(by_kind(res, Kind.EXAM)) == {"2"}
    assert any(n.code == "no_date" for n in res.notices)


# --- 3. LOW: lesson with end <= start is passed on unguarded (exams are guarded) -----------------------------


def test_cancelled_lesson_with_end_before_start_gets_no_negative_interval():
    w = World()
    w.get_timetable = lambda b: rpc_ok([lesson(5, 20261015, 950, 900, "cancelled")])
    item = by_kind(run(w), Kind.CANCELLATION)["5-20261015-0950"]
    assert item.when.end is None or item.when.end > item.when.start


# --- 4. MEDIUM/LOW: HTTP 403 handled inconsistently with JSON-RPC "no right" (-8509) --------------------------


def test_http_403_on_rpc_timetable_falls_back_to_rest_like_minus_8509():
    w = World()
    w.get_timetable = lambda b: text_response("", 403)
    res = run(w)
    c = res.categories[Category.CANCELLATIONS]
    assert c.status == CategoryStatus.OK, c
    assert set(by_kind(res, Kind.CANCELLATION)) == {"701002-20261014-0855"}


def test_http_403_on_getschoolyears_only_disables_clipping():
    w = World()
    w.school_years = lambda b: text_response("", 403)
    res = run(w)
    assert res.categories[Category.CANCELLATIONS].status == CategoryStatus.OK


# --- 5. LOW: notices for homework that is never delivered (outside window, not tracked) -------------------


def test_no_notices_for_past_homework_that_is_not_returned():
    w = World()
    past = {"id": 31, "lessonId": 3301, "date": 20260925, "dueDate": 20261005, "text": "", "remark": "",
            "completed": False, "attachments": []}
    w.homework = lambda r: json_response({"data": {"homeworks": [past], "lessons": [{"id": 3301, "subject": "AM"}]}})
    res = run(w)
    assert by_kind(res, Kind.HOMEWORK) == {}
    assert [n for n in res.notices if n.uid == "webuntis:homework:31"] == []


# --- 6. LOW: same lesson key both cancelled and regular -> item AND withdrawn for the same uid ---------------


def test_same_key_cancelled_and_regular_is_not_also_withdrawn():
    w = World()
    w.get_timetable = lambda b: rpc_ok([
        lesson(701002, 20261014, 855, 945, "cancelled"),
        lesson(701002, 20261014, 855, 945, "irregular"),
    ])
    tracked = [ref(Kind.CANCELLATION, "701002-20261014-0855", datetime(2026, 10, 14, 8, 55, tzinfo=VIENNA),
                   datetime(2026, 10, 14, 9, 45, tzinfo=VIENNA))]
    res = run(w, tracked)
    uid = "webuntis:cancellation:701002-20261014-0855"
    assert not (uid in {i.uid for i in res.items} and uid in res.withdrawn)


# --- 7. LOW: unguarded AttributeError on non-object JSON from the login form -----------------------------------


def test_form_login_json_list_response_is_classified_error_not_crash():
    w = World()
    w.raw_rpc = lambda r: html_page()
    w.form = lambda r: json_response([])
    res = run(w)  # must not raise AttributeError
    assert res.login_error is not None


# --- 8. LOW: user-visible German text contains "Fehlercode None" ----------------------------------------------


def test_missing_person_id_does_not_print_fehlercode_none():
    w = World()
    ok = {"jsonrpc": "2.0", "id": "SchulSync", "result": {"sessionId": "DUMMY-SESSION-0001", "personType": 5}}
    w.authenticate = lambda body: json_response(ok)
    res = run(w)
    assert "Fehlercode None" not in (res.categories[Category.CANCELLATIONS].detail or "")


# --- 9. MEDIUM: tracked exam moved beyond covered range is never found (stale calendar entry) ----------------


def test_tracked_exam_moved_far_beyond_window_is_still_found():
    w = World()
    w.exam = lambda r: json_response({"data": {"exams": [
        {"id": 5501, "examType": "Schularbeit", "name": "2. Schularbeit", "examDate": 20261201, "startTime": 800,
         "endTime": 950, "subject": "AM", "teachers": [], "rooms": [], "text": ""}
        ] if int(r.params["startDate"]) <= 20261201 <= int(r.params["endDate"]) else []}})
    tracked = [ref(Kind.EXAM, "5501", datetime(2026, 10, 21, 8, 0, tzinfo=VIENNA))]
    res = run(w, tracked)
    assert "5501" in by_kind(res, Kind.EXAM)
