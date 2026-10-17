"""Audit fixes in the school adapters, and the adapter tests that were too weak (see tests/test_audit_2.py for the failing
audit tests of the same gaps).

* Eduvidual web route (ANFORDERUNGEN 2 / 3.4c): a known open assignment is followed over the whole documented range; the run
  says that assignments without a deadline are invisible on this route.
* Weak Eduvidual / WebUntis tests made strict: complete titles, silence about outside-window entries, literal allow-lists,
  a submission status that never changes anything.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from schulsync.adapters import eduvidual as ev
from schulsync.models import Category, Severity, Source
from schulsync.timeutil import VIENNA, fetch_window
from tests.support.fake_http import FakeHttp, json_response
from tests.test_eduvidual import (
    EXPECTED_IN_WINDOW,
    LONG_NAME,
    add_web,
    add_ws,
    jf,
    make as make_edu,
    run as run_edu,
    tracked_ref,
    web_adapter,
    ws_adapter,
)
from tests.test_webuntis import World as WuWorld, run as run_wu

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA)


# =============================================================================== Eduvidual web route (2.9c / 3.4c / 3.2c)


def test_the_web_route_scans_the_whole_range_while_something_is_tracked_and_only_the_window_otherwise():
    w = fetch_window(NOW)
    nothing, truncated = ev.months_to_scan(w, [])
    assert nothing == [(2026, 10), (2026, 11), (2026, 12), (2027, 1)] and not truncated
    tracked, truncated = ev.months_to_scan(w, [date(2026, 11, 20)])
    assert len(tracked) == ev.WEB_MAX_MONTHS and tracked[0] == (2026, 10) and tracked[-1] == (2028, 9) and not truncated
    far, truncated = ev.months_to_scan(w, [date(2031, 1, 1)])
    assert len(far) == ev.WEB_MAX_MONTHS and truncated  # a tracked date beyond the cap is still reported as limited


def test_a_deadline_moved_to_the_last_month_of_the_range_is_found_and_one_beyond_it_is_reported():
    events = [e for e in jf("calendar_due_events.json") if e["instance"] != 4109]
    base = next(e for e in jf("calendar_due_events.json") if e["instance"] == 4109)
    for when, found in ((datetime(2028, 9, 15, 9, 0, tzinfo=VIENNA), True), (datetime(2028, 11, 15, 9, 0, tzinfo=VIENNA), False)):
        moved = {**base, "timestart": int(when.timestamp())}
        adapter, http = web_adapter(events=[*events, moved])
        res = run_edu(adapter, NOW, [tracked_ref(4109, date(2026, 11, 20))])
        assert ("assign:4109" in {i.source_id for i in res.items}) is found
        if not found:
            assert any(n.code == "tracked_not_found" for n in res.notices)  # never silently: the entry stays as it was and says so
        months = [r.json[0]["args"]["month"] for r in http.requests if r.json and r.json[0].get("methodname") == ev.FN_MONTH]
        assert len(months) == ev.WEB_MAX_MONTHS


def test_the_web_route_without_tracked_entries_reads_only_the_window_months():
    adapter, http = web_adapter()
    run_edu(adapter, NOW)
    months = [r.json[0]["args"] for r in http.requests if r.json and r.json[0].get("methodname") == ev.FN_MONTH]
    assert len(months) == 4


@pytest.mark.parametrize("method", ["web", "auto"])
def test_the_web_route_says_in_the_notices_and_the_category_detail_that_deadline_less_assignments_are_invisible(method):
    http = FakeHttp()
    if method == "auto":
        add_ws(http, token=json_response(jf("token_service_disabled.json")))
    add_web(http)
    res = run_edu(make_edu(http, method=method), NOW)
    (notice,) = [n for n in res.notices if n.code == "no_date_unseen"]
    assert notice.severity == Severity.INFO and notice.source == Source.EDUVIDUAL and "ohne Abgabefrist" in notice.message
    assert "ohne Abgabefrist" in res.categories[Category.ASSIGNMENTS].detail


def test_the_webservice_route_sees_them_and_says_nothing_of_the_kind():
    adapter, _ = ws_adapter()
    res = run_edu(adapter, NOW)
    assert [n for n in res.notices if n.code == "no_date_unseen"] == []
    assert any(n.code == "no_date" and "keine Abgabefrist" in n.message for n in res.notices)  # reported one by one instead
    assert "nicht sichtbar" not in res.categories[Category.ASSIGNMENTS].detail


# =============================================================================== weak Eduvidual tests made strict


def test_a_very_long_assignment_name_is_taken_over_completely_not_cut():
    """ANFORDERUNGEN 5: "Abgeschnittene Titel nach Möglichkeit aus der Detailansicht vervollständigen". The webservice returns the
    full name; nothing in the adapter may shorten it (the happy path fixture has no truncated label that could prove it)."""
    body = jf("assignments.json")
    long_name = "Sehr langer Aufgabentitel " + "mit vielen Wörtern " * 30 + "bis zum Ende"
    for course in body["courses"]:
        for a in course["assignments"]:
            if a["cmid"] == 4101:
                a["name"] = long_name
    http = FakeHttp()
    add_ws(http, assignments=json_response(body))
    res = run_edu(make_edu(http, method="webservice"), NOW)
    item = {i.source_id: i for i in res.items}["assign:4101"]
    assert item.title == long_name and len(item.title) > 300 and "…" not in item.title


def test_the_web_route_takes_the_complete_activity_name_and_not_the_truncated_event_label():
    events = [dict(e) for e in jf("calendar_due_events.json")]
    for e in events:
        if e["instance"] == 4101:
            e["name"] = "Projektdokumentation Teil 2 – Datenba…"  # a truncated label, as a list view would show it
    adapter, _ = web_adapter(events=events)
    item = {i.source_id: i for i in run_edu(adapter, NOW).items}["assign:4101"]
    assert item.title == LONG_NAME


def test_nothing_at_all_is_said_about_assignments_outside_the_window():
    """The old filter `n.uid and any(...)` let a notice without uid through and looked at three ids only. Here no text of any
    notice, error or detail may mention ANY assignment that lies outside the window and is not tracked."""
    body = jf("assignments.json")
    outside = [a for c in body["courses"] for a in c["assignments"] if f"assign:{a['cmid']}" not in EXPECTED_IN_WINDOW and a["duedate"]]
    assert {a["cmid"] for a in outside} >= {4105, 4107, 4109}
    adapter, _ = ws_adapter()
    res = run_edu(adapter, NOW)
    texts = [n.message + " " + (n.uid or "") + " " + (n.title or "") for n in res.notices]
    texts += [oc.detail or "" for oc in res.categories.values()] + [str(oc.error) for oc in res.categories.values()]
    blob = "\n".join(texts)
    for a in outside:
        assert str(a["cmid"]) not in blob and a["name"] not in blob, a["name"]


def test_the_allow_lists_equal_an_independently_written_set_and_no_other_function_is_named_in_the_adapter():
    """The allow-lists are the adapter's own constants (a test that only reads them is circular). Pinned here as literals,
    plus a scan of the adapter source: every Moodle function name it mentions is on a list (or the public config probe)."""
    assert ev.READ_ONLY_WS_FUNCTIONS == {
        "mod_assign_get_submission_status",  # read-only: own submission status (auto-tick in Google Tasks)
        "core_webservice_get_site_info",
        "core_course_get_enrolled_courses_by_timeline_classification",
        "mod_assign_get_assignments",
    }
    assert ev.READ_ONLY_AJAX_FUNCTIONS == {
        "core_course_get_enrolled_courses_by_timeline_classification",
        "core_calendar_get_calendar_monthly_view",
    }
    source = Path(ev.__file__).read_text(encoding="utf-8")
    named = set(re.findall(r'"((?:core|mod|tool|message|enrol|local|gradereport|block|report)_[a-z_]+)"', source))
    allowed = ev.READ_ONLY_WS_FUNCTIONS | ev.READ_ONLY_AJAX_FUNCTIONS | {ev.FN_PUBLIC_CONFIG}
    assert named and named <= allowed, named - allowed


def test_submission_and_grading_status_never_changes_what_is_taken_over():
    """Fields of the assignment RECORD never change what is taken over (the submission status is read separately via
    mod_assign_get_submission_status and only ticks the Google task off, see test_submission.py). Records that
    carry every status Moodle might add (submitted, graded, overdue, not actionable) give exactly the same items."""
    plain = run_edu(ws_adapter()[0], NOW)
    body = jf("assignments.json")
    for course in body["courses"]:
        for a in course["assignments"]:
            a.update(submitted=True, graded=True, submissionstatus="submitted", gradingstatus="graded", cansubmit=False, status="submitted")
    http = FakeHttp()
    add_ws(http, assignments=json_response(body))
    marked = run_edu(make_edu(http, method="webservice"), NOW)
    assert [i.content_hash() for i in marked.items] == [i.content_hash() for i in plain.items] and marked.items

    events = [dict(e, action={"name": "Abgabe", "actionable": False, "itemcount": 0}, overdue=True, isactionevent=True) for e in jf("calendar_due_events.json")]
    plain_web = run_edu(web_adapter()[0], NOW)
    marked_web = run_edu(web_adapter(events=events)[0], NOW)
    assert [i.content_hash() for i in marked_web.items] == [i.content_hash() for i in plain_web.items] and marked_web.items


def test_the_homework_completed_flag_of_webuntis_never_changes_what_is_taken_over():
    def result(completed: bool):
        w = WuWorld()
        data = json.loads(json.dumps(w.homeworks))
        for h in data["data"]["homeworks"]:
            h["completed"] = completed
        w.homeworks = data
        return run_wu(w)

    done, open_ = result(True), result(False)
    assert [i.content_hash() for i in done.items] == [i.content_hash() for i in open_.items] and done.items
