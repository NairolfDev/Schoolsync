"""Audit of ANFORDERUNGEN sections 2 and 3 (sources, scope, real data access).

Every test here demonstrates ONE requirement gap and FAILS today for the stated reason. When the gap is fixed the
test passes. Helpers are imported from the adapter test modules (no test_* names are imported).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pytest

from schulsync.adapters.webuntis import WebUntisAdapter
from schulsync.models import Category, CategoryStatus
from schulsync.timeutil import VIENNA
from tests.support.fake_http import FakeHttp, json_response
from tests.test_eduvidual import add_web, add_ws, ids, jf, make, run as run_edu, tracked_ref, web_adapter
from tests.test_webuntis import CFG, PW, World, run as run_wu

ROOT = Path(__file__).resolve().parents[1]

_NO_DEADLINE = re.compile(r"ohne\s+(abgabe)?-?\s*frist|keine\s+(abgabe)?-?\s*frist|ohne\s+f[äa]lligkeit", re.I)


# --- 3.2 "Fehlende Kategorien dürfen nicht stillschweigend entfallen" (Eduvidual web route) -------------------


@pytest.mark.parametrize("method", ["web", "auto"])
def test_web_route_run_tells_that_assignments_without_deadline_cannot_be_seen(now, method):
    """Via the web route Moodle's calendar only knows assignments WITH a due date, so ones without are invisible and never
    appear as `no_date` in the report. docs/datenzugriff_eduvidual.md 7 says only `check` mentions it; the daily run
    (report, category detail, notices) stays silent."""
    http = FakeHttp()
    if method == "auto":
        add_ws(http, token=json_response(jf("token_service_disabled.json")))
    add_web(http)
    res = run_edu(make(http, method=method), now)
    assert res.complete  # an ordinary OK result: nothing else signals the blind spot
    visible = [n.message for n in res.notices] + [oc.detail or "" for oc in res.categories.values()]
    assert any(_NO_DEADLINE.search(t) for t in visible), visible


# --- 2 "Bekannte offene Einträge ... auch außerhalb des Fensters prüfen" (Eduvidual web route) -----------------


def test_web_route_finds_a_known_deadline_moved_more_than_two_months_out(now):
    """Web route scans only up to (latest tracked date + 2 months). A known assignment moved from 20.11.2026 to
    15.03.2027 (well inside the documented 24-month cap) stays stale; the webservice route would see it."""
    events = [e for e in jf("calendar_due_events.json") if e["instance"] != 4109]
    moved = dict(next(e for e in jf("calendar_due_events.json") if e["instance"] == 4109))
    moved["timestart"] = int(datetime(2027, 3, 15, 9, 0, tzinfo=VIENNA).timestamp())
    adapter, _ = web_adapter(events=[*events, moved])
    res = run_edu(adapter, now, [tracked_ref(4109, date(2026, 11, 20))])
    got = {i.source_id: i for i in res.items}
    assert "assign:4109" in got and got["assign:4109"].when.start.date() == date(2027, 3, 15)


# --- 3.2 WebUntis calendar export must be evaluated and documented -----------------------------------------------


def test_webuntis_data_access_report_evaluates_the_calendar_export():
    """ANFORDERUNGEN 3: check which calendar exports are accessible and what they fully deliver. The Eduvidual report
    evaluates its iCal export (section 8); the WebUntis report never mentions an iCal/calendar export at all."""
    text = (ROOT / "docs" / "datenzugriff_webuntis.md").read_text(encoding="utf-8")
    assert re.search(r"\biCal\b|Kalenderexport|\.ics\b", text, re.I)


# --- 3 "Schulanwendungen ausschließlich lesen" enforced in code (WebUntis) ---------------------------------------


def test_webuntis_rpc_layer_refuses_methods_outside_the_read_allow_list():
    """Eduvidual refuses non-allow-listed functions before any request (test_guard_refuses_...). WebUntis `_rpc` sends
    any method name it is given; read-only holds only by convention of today's three callers."""
    http = FakeHttp()
    http.add("POST", r"/WebUntis/jsonrpc\.do", json_response({"jsonrpc": "2.0", "id": "SchulSync", "result": []}))
    adapter = WebUntisAdapter(CFG, PW, http_factory=lambda: http)
    with pytest.raises(Exception):
        adapter._rpc("setHomeworkCompleted", {"id": 1}, "x")
    assert http.requests == []


# --- 3.4 "Geänderte Seitenstrukturen müssen einen erkennbaren Fehler auslösen" (WebUntis optional fields) ----------


def test_renamed_due_date_field_of_every_homework_is_not_an_ok_result():
    """If WebUntis renames/drops `dueDate`, every homework silently turns into a per-entry `no_date` warning and the
    category stays OK (day counted as success). Heuristic: a non-empty answer in which NO entry has the field at all
    is a structure change (a single legitimate undated entry stays a notice, see test_webuntis_review)."""
    w = World()
    hws = [
        {"id": i, "lessonId": 3301, "date": 20261012, "due": 20261016, "text": f"HA {i}", "remark": "", "completed": False, "attachments": []}
        for i in (1, 2, 3)
    ]
    w.homework = lambda r: json_response({"data": {"homeworks": hws, "lessons": [{"id": 3301, "subject": "AM"}]}})
    assert run_wu(w).categories[Category.HOMEWORK].status != CategoryStatus.OK


def test_renamed_date_field_of_every_exam_is_not_an_ok_result():
    w = World()
    exams = [
        {"id": i, "examType": "Test", "name": f"T{i}", "date": 20261021, "startTime": 800, "endTime": 850, "subject": "AM", "teachers": [], "rooms": [], "text": ""}
        for i in (1, 2, 3)
    ]
    w.exam = lambda r: json_response({"data": {"exams": exams}})
    assert run_wu(w).categories[Category.EXAMS].status != CategoryStatus.OK
