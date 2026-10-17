"""Tests for the Eduvidual adapter.

HONESTY: every response used here is a SYNTHETIC fixture (tests/fixtures/eduvidual, see README.txt there) that
mirrors the documented Moodle structures. Nothing was recorded from a real, authenticated Eduvidual session.
These tests prove the adapter's rules (parsing, filtering, failure semantics, read-only behaviour); they cannot
prove that the real server answers exactly like this. `schulsync check` on the user's PC is the real test.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from schulsync.adapters import eduvidual as ev
from schulsync.adapters.eduvidual import (
    FN_SUBMISSION,
    FN_ASSIGNMENTS,
    FN_COURSES,
    FN_MONTH,
    FN_SITE_INFO,
    READ_ONLY_AJAX_FUNCTIONS,
    READ_ONLY_WS_FUNCTIONS,
    EduvidualAdapter,
    html_to_text,
    months_to_scan,
    parse_m_cfg,
    require,
    safe_http_url,
    ts_to_vienna,
)
from schulsync.config import EduvidualConfig, RetryConfig
from schulsync.errors import AuthError, ErrorInfo, NetworkError, SchemaError, TransientError
from schulsync.models import (
    Category,
    CategoryStatus,
    ItemStatus,
    Kind,
    Severity,
    Source,
    TrackedRef,
)
from schulsync.timeutil import UTC, VIENNA, fetch_window
from tests.support.fake_http import FakeHttp, json_response, redirect, text_response

FIX = Path(__file__).parent / "fixtures" / "eduvidual"
BASE = "https://www.eduvidual.at"
PASSWORD = "hunter2-dummy"
SECRETS = [PASSWORD, "dummy-wstoken-0001", "dummy-sesskey-AbCd1234", "dummy-logintoken-LOGIN", "dummy-logintoken-GUEST"]
LONG_NAME = "Projektdokumentation Teil 2 – Datenbankentwurf (ER-Modell und Normalisierung der Schulbibliothek)"
CAT = Category.ASSIGNMENTS


# --------------------------------------------------------------------------- helpers


def jf(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def tf(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def ajax_ok(data) -> object:
    return json_response([{"error": False, "data": data}])


def ajax_err(code: str) -> object:
    return json_response([{"error": True, "exception": {"message": "x", "errorcode": code}}])


DEFAULT_COURSE_PAGES = {
    0: lambda: json_response(jf("courses_page1.json")),
    2: lambda: json_response(jf("courses_page2.json")),
    4: lambda: json_response(jf("courses_last_empty.json")),
}


def add_public_config(http: FakeHttp, **over) -> None:
    data = {"sitename": "eduvidual.at", "enablewebservices": 1, "enablemobilewebservice": 1, "maintenanceenabled": 0, "typeoflogin": 2}
    data.update(over)
    http.add("GET", r"/lib/ajax/service-nologin\.php", ajax_ok(data))


SUBMISSIONS: dict[int, tuple[str, int]] = {}  # assign id -> (status, attemptnumber); default ("new", 0)


def add_ws(http: FakeHttp, *, token=None, site=None, pages=None, assignments=None, rest=None, submissions=None) -> None:
    http.add("POST", r"/login/token\.php", token or json_response(jf("token_ok.json")))
    pages = pages or DEFAULT_COURSE_PAGES

    def handler(req):
        if rest is not None:
            r = rest(req)
            if r is not None:
                return r
        fn = req.data["wsfunction"]
        if fn == FN_SITE_INFO:
            return site or json_response(jf("site_info.json"))
        if fn == FN_COURSES:
            page = pages[int(req.data["offset"])]
            return page() if callable(page) else page
        if fn == FN_ASSIGNMENTS:
            resp = assignments or json_response(jf("assignments.json"))
            try:  # like Moodle: only the requested courses come back
                wanted = {int(v) for k, v in req.data.items() if k.startswith("courseids[")}
                body = json.loads(resp.text)
                if isinstance(body, dict) and isinstance(body.get("courses"), list) and wanted:
                    body["courses"] = [c for c in body["courses"] if c["id"] in wanted]
                    return json_response(body)
            except (ValueError, KeyError, TypeError):
                pass
            return resp
        if fn == FN_SUBMISSION:  # synthetic, shape of mod_assign_get_submission_status (lastattempt.submission)
            status, attempt = (submissions if submissions is not None else SUBMISSIONS).get(int(req.data["assignid"]), ("new", 0))
            return json_response({"lastattempt": {"submission": {"status": status, "attemptnumber": attempt}}, "warnings": []})
        raise AssertionError(f"unexpected function {fn}")

    http.add("POST", r"/webservice/rest/server\.php", handler)


def month_payload(events: list[dict], year: int, month: int) -> dict:
    sel = [e for e in events if (d := ts_to_vienna(e["timestart"])).year == year and d.month == month]
    # structure of core_calendar_get_calendar_monthly_view: weeks -> days -> events (multi-day events repeat)
    return {"weeks": [{"days": [{"events": sel}, {"events": []}]}, {"days": [{"events": sel[:1]}]}]}


def add_web(http: FakeHttp, *, events=None, pages=None, login_page=None, login_post=None, dashboard=None, ajax_override=None) -> None:
    events = events if events is not None else jf("calendar_due_events.json")
    pages = pages or DEFAULT_COURSE_PAGES
    http.add("GET", r"/login/index\.php", text_response(login_page or tf("login_page.html")))
    http.add("POST", r"/login/index\.php", login_post or redirect(BASE + "/my/"))
    http.add("GET", r"/my/", text_response(dashboard or tf("dashboard.html")))

    def ajax(req):
        call = req.json[0]
        if ajax_override is not None:
            r = ajax_override(call)
            if r is not None:
                return r
        if call["methodname"] == FN_COURSES:
            page = pages[call["args"]["offset"]]
            body = page() if callable(page) else page
            return ajax_ok(json.loads(body.text))
        if call["methodname"] == FN_MONTH:
            return ajax_ok(month_payload(events, call["args"]["year"], call["args"]["month"]))
        raise AssertionError(f"unexpected ajax function {call['methodname']}")

    http.add("POST", r"/lib/ajax/service\.php", ajax)


def make(http: FakeHttp, **cfg) -> EduvidualAdapter:
    cfg.setdefault("username", "max.mustermann")
    return EduvidualAdapter(EduvidualConfig(**cfg), PASSWORD, http_factory=lambda: http)


def ws_adapter(**kw) -> tuple[EduvidualAdapter, FakeHttp]:
    http = FakeHttp()
    ws_keys = ("token", "site", "pages", "assignments", "rest", "submissions")
    add_ws(http, **{k: v for k, v in kw.items() if k in ws_keys})
    return make(http, method="webservice", **{k: v for k, v in kw.items() if k not in ws_keys}), http


def web_adapter(**kw) -> tuple[EduvidualAdapter, FakeHttp]:
    http = FakeHttp()
    add_web(http, **{k: v for k, v in kw.items() if k in ("events", "pages", "login_page", "login_post", "dashboard", "ajax_override")})
    return make(http, method="web", **{k: v for k, v in kw.items() if k not in ("events", "pages", "login_page", "login_post", "dashboard", "ajax_override")}), http


def run(adapter: EduvidualAdapter, now, tracked=()):
    return adapter.fetch(fetch_window(now), list(tracked), now)


def ids(result) -> list[str]:
    return [i.source_id for i in result.items]


def codes(result) -> list[str]:
    return [n.code for n in result.notices]


def tracked_ref(cmid: int, start, kind=Kind.ASSIGNMENT, source=Source.EDUVIDUAL) -> TrackedRef:
    return TrackedRef(source, kind, f"assign:{cmid}", start, None)


def all_texts(result) -> list[str]:
    out: list[str] = []
    for n in result.notices:
        out += [n.message, n.title or "", n.code]
    infos = [result.login_error] + [c.error for c in result.categories.values()]
    for i in infos:
        if i:
            out += [i.code, i.message, i.hint or "", i.detail or ""]
    out += [c.detail or "" for c in result.categories.values()]
    for it in result.items:
        out += list(it.notes) + [it.title, it.description or "", it.url or ""]
    return out


def assert_no_secrets(*texts: str) -> None:
    blob = "\n".join(texts)
    for secret in SECRETS:
        assert secret not in blob, f"secret leaked: {secret[:10]}..."


EXPECTED_IN_WINDOW = ["assign:4108", "assign:4101", "assign:4102", "assign:4103", "assign:4106"]  # sorted by due date


# --------------------------------------------------------------------------- route 1: web service


def test_webservice_happy_path_items(now):
    adapter, http = ws_adapter()
    res = run(adapter, now)
    assert res.complete and res.login_error is None
    assert res.categories[CAT].status == CategoryStatus.OK
    assert res.categories[CAT].item_count == 5
    assert ids(res) == EXPECTED_IN_WINDOW
    it = {i.source_id: i for i in res.items}["assign:4101"]
    assert it.uid == "eduvidual:assignment:assign:4101"
    assert it.source == Source.EDUVIDUAL and it.kind == Kind.ASSIGNMENT and it.status == ItemStatus.ACTIVE
    assert it.title == LONG_NAME  # the name of the assignment record as is; completion over a truncated label: tests/test_audit_adapters.py
    assert it.course == "Softwareentwicklung 4AHITN 2026/27"
    assert it.url == f"{BASE}/mod/assign/view.php?id=4101"
    assert it.when.start == datetime(2026, 10, 16, 23, 59, tzinfo=VIENNA) and it.when.end is None
    assert not it.when.all_day and it.id_stable
    assert it.description == (
        "Erstellt das ER-Modell für die Schulbibliothek.\n\n- Entitäten mit Attributen\n- Normalisierung bis 3NF\n\n"
        "Vorlage: Vorlage ER-Modell (https://www.eduvidual.at/mod/resource/view.php?id=5001)"
    )
    assert it.notes == ()


def test_webservice_empty_intro_gives_no_description(now):
    adapter, _ = ws_adapter()
    it = {i.source_id: i for i in run(adapter, now).items}["assign:4103"]
    assert it.description is None


def test_webservice_request_shapes(now):
    adapter, http = ws_adapter()
    run(adapter, now)
    token = http.calls(r"/login/token\.php")
    assert len(token) == 1 and token[0].method == "POST"
    assert token[0].data == {"username": "max.mustermann", "password": PASSWORD, "service": "moodle_mobile_app"}
    rest = http.calls(r"/webservice/rest/server\.php")
    by_fn = {}
    for r in rest:
        by_fn.setdefault(r.data["wsfunction"], []).append(r.data)
        assert r.data["moodlewsrestformat"] == "json" and r.data["wstoken"] == "dummy-wstoken-0001"
    assert set(by_fn) == {FN_SITE_INFO, FN_COURSES, FN_ASSIGNMENTS, FN_SUBMISSION}
    assert [d["offset"] for d in by_fn[FN_COURSES]] == ["0", "2", "4"]
    assert all(d["classification"] == "inprogress" and d["limit"] == "50" for d in by_fn[FN_COURSES])
    ass = by_fn[FN_ASSIGNMENTS][0]
    assert [ass[f"courseids[{i}]"] for i in range(4)] == ["7001", "7002", "7003", "7004"]
    # nothing but the web service route was used
    assert not http.calls(r"/login/index\.php") and not http.calls(r"/lib/ajax/service\.php")


def test_secrets_only_in_post_bodies_never_in_urls(now):
    adapter, http = ws_adapter()
    run(adapter, now)
    for r in http.requests:
        assert PASSWORD not in r.url
        assert PASSWORD not in json.dumps(r.params or {}) and PASSWORD not in json.dumps(r.json or {})
        if r.method == "GET":
            assert "token" not in r.url.lower()
    assert any(PASSWORD in json.dumps(r.data) for r in http.requests if r.data)  # it IS sent, but only as body


def test_webservice_no_due_date_gives_notice_and_no_item(now):
    adapter, _ = ws_adapter()
    res = run(adapter, now)
    n = [x for x in res.notices if x.code == "no_date"]
    assert len(n) == 1
    assert n[0].severity == Severity.WARNING and n[0].source == Source.EDUVIDUAL
    assert n[0].title == "Freiwillige Zusatzaufgabe (ohne Frist)" and n[0].uid == "eduvidual:assignment:assign:4104"
    assert "assign:4104" not in ids(res)
    assert res.complete


@pytest.mark.parametrize(
    ("cmid", "expected"),
    [
        (4108, True),  # due exactly today 00:00 -> window start is inclusive
        (4107, False),  # due yesterday 23:59 -> before the window
        (4106, True),  # due 2026-11-03 23:59:59 -> last second inside
        (4105, False),  # due exactly 2026-11-04 00:00 -> window end is exclusive
        (4109, False),  # far outside
    ],
)
def test_window_edges(now, cmid, expected):
    adapter, _ = ws_adapter()
    assert (f"assign:{cmid}" in ids(run(adapter, now))) is expected


def test_outside_window_items_are_silent(now):
    adapter, _ = ws_adapter()
    res = run(adapter, now)
    assert not [n for n in res.notices if n.uid and any(c in n.uid for c in ("4105", "4107", "4109"))]


def test_tracked_item_outside_window_is_still_returned_with_current_data(now):
    adapter, _ = ws_adapter()
    # known deadline was 20 Nov (outside the window); Eduvidual now says 15 Dec -> shift must be visible
    res = run(adapter, now, [tracked_ref(4109, datetime(2026, 11, 20, 8, 0, tzinfo=VIENNA))])
    it = {i.source_id: i for i in res.items}["assign:4109"]
    assert it.when.start == datetime(2026, 12, 15, 8, 0, tzinfo=VIENNA)
    assert "assign:4107" not in ids(res)
    assert any("Beschreibung" in n for n in it.notes)  # server hid the intro -> honest note, nothing invented
    assert it.description is None
    assert any(n.code == "incomplete_data" and n.uid == it.uid for n in res.notices)


def test_tracked_past_item_is_still_returned(now):
    adapter, _ = ws_adapter()
    res = run(adapter, now, [tracked_ref(4107, datetime(2026, 10, 13, 23, 59, tzinfo=VIENNA))])
    assert "assign:4107" in ids(res)


def test_tracked_item_missing_is_not_a_withdrawal(now):
    adapter, _ = ws_adapter()
    res = run(adapter, now, [tracked_ref(9999, date(2026, 10, 20))])
    assert res.complete and not res.withdrawn
    n = [x for x in res.notices if x.code == "tracked_not_found"]
    assert len(n) == 1 and n[0].uid == "eduvidual:assignment:assign:9999" and n[0].severity == Severity.INFO
    assert all(i.status == ItemStatus.ACTIVE for i in res.items)


def test_tracked_of_other_source_or_kind_is_ignored(now):
    adapter, _ = ws_adapter()
    res = run(adapter, now, [tracked_ref(4109, date(2026, 12, 15), kind=Kind.HOMEWORK), tracked_ref(4109, date(2026, 12, 15), source=Source.WEBUNTIS)])
    assert "assign:4109" not in ids(res)


def test_course_filter_by_id(now):
    adapter, http = ws_adapter(courses=("7002",))
    res = run(adapter, now)
    assert ids(res) == ["assign:4108", "assign:4103", "assign:4106"]
    ass = [r for r in http.calls(r"/webservice/rest/server\.php") if r.data["wsfunction"] == FN_ASSIGNMENTS][0].data
    assert ass["courseids[0]"] == "7002" and "courseids[1]" not in ass


def test_course_filter_by_exact_full_name(now):
    adapter, _ = ws_adapter(courses=("Softwareentwicklung  4AHITN 2026/27",))  # extra blank is normalised
    assert ids(run(adapter, now)) == ["assign:4101", "assign:4102"]


def test_course_filter_unknown_course_warns_and_never_sends_empty_courseids(now):
    adapter, http = ws_adapter(courses=("Kunstgeschichte",))
    res = run(adapter, now)
    assert res.items == [] and res.categories[CAT].status == CategoryStatus.OK
    n = [x for x in res.notices if x.code == "course_not_found"]
    assert len(n) == 1 and n[0].severity == Severity.WARNING and "Kunstgeschichte" in n[0].message
    # an empty courseids list would make Moodle return ALL courses -> must not be requested at all
    assert not [r for r in http.calls(r"/webservice/rest/server\.php") if r.data["wsfunction"] == FN_ASSIGNMENTS]


def test_course_filter_partial_match_keeps_warning(now):
    adapter, _ = ws_adapter(courses=("7001", "9999"))
    res = run(adapter, now)
    assert ids(res) == ["assign:4101", "assign:4102"]
    assert "course_not_found" in codes(res)


def test_honest_empty_account_is_ok_with_zero_items(now):
    adapter, http = ws_adapter(pages={0: json_response(jf("courses_none.json"))})
    res = run(adapter, now)
    assert res.complete and res.items == [] and res.categories[CAT].status == CategoryStatus.OK
    assert not [r for r in http.calls(r"/webservice/rest/server\.php") if r.data["wsfunction"] == FN_ASSIGNMENTS]


def test_honest_empty_assignment_list_is_ok(now):
    empty = json_response({"courses": [{"id": c, "fullname": "x", "shortname": "x", "timemodified": 1, "assignments": []} for c in (7001, 7002, 7003, 7004)], "warnings": []})
    adapter, _ = ws_adapter(assignments=empty)
    res = run(adapter, now)
    assert res.complete and res.items == [] and res.categories[CAT].item_count == 0


# ---- pagination


def test_course_pagination_reads_every_page(now):
    adapter, http = ws_adapter()
    run(adapter, now)
    offs = [r.data["offset"] for r in http.calls(r"/webservice/rest/server\.php") if r.data["wsfunction"] == FN_COURSES]
    assert offs == ["0", "2", "4"]


def test_empty_page_with_advancing_offset_is_not_the_end(now):
    p1, p2 = jf("courses_page1.json"), jf("courses_page2.json")
    pages = {
        0: json_response(p1),
        2: json_response({"courses": [], "nextoffset": 3}),  # nextoffset = offset + processed courses
        3: json_response({**p2, "nextoffset": 5}),
        5: json_response({"courses": [], "nextoffset": 5}),
    }
    adapter, http = ws_adapter(pages=pages)
    res = run(adapter, now)
    assert res.complete and len(ids(res)) == 5
    ass = [r for r in http.calls(r"/webservice/rest/server\.php") if r.data["wsfunction"] == FN_ASSIGNMENTS][0].data
    assert ass["courseids[3]"] == "7004"


def test_truncated_course_pagination_is_incomplete_not_empty(now):
    broken = json_response({"courses": []})  # second page lost 'nextoffset'
    adapter, _ = ws_adapter(pages={0: json_response(jf("courses_page1.json")), 2: broken})
    res = run(adapter, now)
    oc = res.categories[CAT]
    assert oc.status == CategoryStatus.INCOMPLETE and not res.complete
    assert oc.error is not None and oc.error.code == "incomplete" and "nextoffset" in (oc.error.detail or "")
    assert ids(res)  # what was readable is still delivered, but never as a complete result


def test_pagination_without_progress_is_incomplete(now):
    stuck = json_response({"courses": jf("courses_page1.json")["courses"], "nextoffset": 0})
    adapter, _ = ws_adapter(pages={0: stuck})
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.INCOMPLETE and not res.complete


def test_failing_first_course_page_is_failed(now):
    adapter, _ = ws_adapter(pages={0: json_response({"courses": []})})
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.items == [] and not res.complete
    assert res.categories[CAT].error.code == "schema"


# ---- warnings returned by mod_assign_get_assignments


def test_module_warning_is_info_only(now):
    adapter, _ = ws_adapter(assignments=json_response(jf("assignments_module_warning.json")))
    res = run(adapter, now)
    assert res.complete and ids(res) == EXPECTED_IN_WINDOW
    n = [x for x in res.notices if x.code == "module_not_readable"]
    assert len(n) == 1 and n[0].severity == Severity.INFO


def test_course_warning_makes_category_incomplete(now):
    adapter, _ = ws_adapter(assignments=json_response(jf("assignments_course_warning.json")))
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.INCOMPLETE and not res.complete
    assert "7004" in (res.categories[CAT].error.detail or "")


def test_course_missing_in_answer_is_incomplete(now):
    data = jf("assignments.json")
    data["courses"] = data["courses"][:3]
    adapter, _ = ws_adapter(assignments=json_response(data))
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.INCOMPLETE


# ---- unexpected structure / wrong content type


def test_missing_field_is_schema_failure_not_empty(now):
    adapter, _ = ws_adapter(assignments=json_response(jf("assignments_missing_duedate.json")))
    res = run(adapter, now)
    oc = res.categories[CAT]
    assert oc.status == CategoryStatus.FAILED and not res.complete and res.items == []
    assert oc.error.code == "schema"
    assert "courses[0].assignments[0].duedate" in oc.error.detail  # JSON path of the missing field


def test_wrong_type_is_schema_failure(now):
    data = jf("assignments.json")
    data["courses"][0]["assignments"][0]["duedate"] = "morgen"
    adapter, _ = ws_adapter(assignments=json_response(data))
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and "duedate" in res.categories[CAT].error.detail


def test_non_object_answer_is_schema_failure(now):
    adapter, _ = ws_adapter(assignments=json_response([]))
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.categories[CAT].error.code == "schema"


def test_html_login_page_instead_of_json_is_failure_not_empty(now):
    http = FakeHttp()
    add_web(http, ajax_override=lambda call: text_response(tf("login_page.html")) if call["methodname"] == FN_COURSES else None)
    res = run(make(http, method="web"), now)
    oc = res.categories[CAT]
    assert oc.status == CategoryStatus.FAILED and not res.complete and res.items == []
    assert oc.error.code == "auth"


def test_html_error_page_instead_of_json_is_schema_failure(now):
    http = FakeHttp()
    add_ws(http, assignments=text_response("<html><body><h1>Wartung</h1></body></html>"))
    res = run(make(http, method="webservice"), now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.categories[CAT].error.code == "schema"


def test_rest_login_page_after_valid_token_is_route_problem_not_credential_rejection(now):
    http = FakeHttp()
    add_ws(http, site=text_response(tf("login_page.html")))
    res = run(make(http, method="webservice"), now)
    assert res.login_error is None  # credentials worked (a token was issued) -> must not block automatic runs
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.categories[CAT].error.code == "route_unavailable"


def test_moodle_exception_json_is_failure(now):
    adapter, _ = ws_adapter(assignments=json_response({"exception": "moodle_exception", "errorcode": "generalexceptionmessage", "message": "Kaputt"}))
    res = run(adapter, now)
    oc = res.categories[CAT]
    assert oc.status == CategoryStatus.FAILED and oc.error.code == "eduvidual_error" and "generalexceptionmessage" in oc.error.detail
    assert "Kaputt" not in oc.error.message  # server text is not copied


def test_no_permission_is_access_denied(now):
    adapter, _ = ws_adapter(assignments=json_response({"exception": "required_capability_exception", "errorcode": "nopermissions", "message": "x"}))
    res = run(adapter, now)
    assert res.categories[CAT].error.code == "access_denied"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_http_client_errors_on_data_call_fail_the_category(now, status):
    http = FakeHttp()
    add_ws(http, assignments=text_response("", status))
    res = run(make(http, method="webservice"), now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and not res.complete


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_http_server_errors_are_transient_and_propagate(now, status):
    http = FakeHttp()
    add_ws(http, assignments=text_response("<html>Fehler</html>", status))
    with pytest.raises(TransientError):
        run(make(http, method="webservice"), now)


def test_token_endpoint_5xx_is_transient_not_a_fallback(now):
    http = FakeHttp()
    add_ws(http, token=text_response("<html>Bad Gateway</html>", 502))
    add_web(http)
    with pytest.raises(TransientError):
        run(make(http, method="auto"), now)
    assert not http.calls(r"/login/index\.php")


def test_network_error_propagates_as_transient(now):
    adapter, http = ws_adapter()
    http.fail_network = True
    with pytest.raises(NetworkError):
        run(adapter, now)
    with pytest.raises(TransientError):
        run(make(http, method="auto"), now)


def test_require_helper_messages_name_the_json_path():
    obj = {"a": {"b": [1, {"c": "x"}]}}
    assert require(obj, "a", dict, "root") is obj["a"]
    with pytest.raises(SchemaError) as e:
        require(obj["a"], "zzz", str, "root.a")
    assert "root.a.zzz" in e.value.detail and "fehlt" in e.value.detail
    with pytest.raises(SchemaError) as e:
        require({"n": True}, "n", int, "r")  # bool is not an int here
    assert "r.n" in e.value.detail
    with pytest.raises(SchemaError) as e:
        require([], "x", int, "")
    assert "<root>" in e.value.detail


# --------------------------------------------------------------------------- credentials / auto routing


def test_invalid_credentials_stop_everything_after_exactly_one_login_attempt(now):
    http = FakeHttp()
    add_ws(http, token=json_response(jf("token_invalid_login.json")))
    add_web(http)
    res = run(make(http, method="auto"), now)
    assert isinstance(res.login_error, ErrorInfo) and res.login_error.code == "auth"
    assert "Passwort" in res.login_error.message and res.login_error.hint
    oc = res.categories[CAT]
    assert oc.status == CategoryStatus.FAILED and not res.complete and res.items == []
    assert len(http.calls(r"/login/token\.php")) == 1
    assert not http.calls(r"/login/index\.php", "POST") and not http.calls(r"/login/index\.php")  # no second route
    assert len(http.requests) == 1
    assert_no_secrets(*all_texts(res))


def test_invalid_credentials_on_web_route_are_reported_once(now):
    http = FakeHttp()
    add_web(http, login_post=text_response(tf("login_page.html")))  # login page shown again
    res = run(make(http, method="web"), now)
    assert res.login_error is not None and res.login_error.code == "auth"
    assert len(http.calls(r"/login/index\.php", "POST")) == 1
    assert_no_secrets(*all_texts(res))


@pytest.mark.parametrize("code", ["usernotconfirmed", "passwordisexpired", "wsaccessusersuspended"])
def test_account_problems_are_auth_errors_without_fallback(now, code):
    http = FakeHttp()
    add_ws(http, token=json_response({"error": "x", "errorcode": code}))
    add_web(http)
    res = run(make(http, method="auto"), now)
    assert res.login_error.code == "auth" and not http.calls(r"/login/index\.php")


@pytest.mark.parametrize(
    "token",
    [
        json_response(jf("token_service_disabled.json")),
        json_response(jf("token_service_unavailable.json")),
        text_response("", 404),
        text_response("<html>kein JSON</html>"),
    ],
    ids=["ws-disabled", "service-unavailable", "endpoint-404", "html-instead-of-json"],
)
def test_auto_falls_back_to_web_route_when_service_unavailable(now, token):
    http = FakeHttp()
    add_ws(http, token=token)
    add_web(http)
    res = run(make(http, method="auto"), now)
    assert res.complete and ids(res) == EXPECTED_IN_WINDOW
    assert len(http.calls(r"/login/token\.php")) == 1  # token route tried once
    assert len(http.calls(r"/login/index\.php", "POST")) == 1  # web login once
    assert not http.calls(r"/webservice/rest/server\.php")
    fb = [n for n in res.notices if n.code == "route_fallback"]
    assert len(fb) == 1 and "webservice" in fb[0].message and "web" in fb[0].message
    assert "Zugang: web" in res.categories[CAT].detail
    assert_no_secrets(*all_texts(res))


def test_auto_falls_back_when_functions_are_not_permitted(now):
    http = FakeHttp()
    add_ws(http, site=json_response(jf("site_info_missing_functions.json")))
    add_web(http)
    res = run(make(http, method="auto"), now)
    assert res.complete and ids(res) == EXPECTED_IN_WINDOW
    assert len(http.calls(r"/login/token\.php")) == 1
    fb = [n for n in res.notices if n.code == "route_fallback"]
    assert "mod_assign_get_assignments" in fb[0].message or "core_course" in fb[0].message


def test_auto_falls_back_when_rest_denies_function_access(now):
    http = FakeHttp()
    add_ws(http, rest=lambda req: json_response(jf("access_exception.json")) if req.data["wsfunction"] == FN_COURSES else None)
    add_web(http)
    res = run(make(http, method="auto"), now)
    assert res.complete and "route_fallback" in codes(res)
    assert len(http.calls(r"/login/token\.php")) == 1


def test_auto_prefers_webservice_when_available(now):
    http = FakeHttp()
    add_ws(http)
    add_web(http)
    res = run(make(http, method="auto"), now)
    assert res.complete and "route_fallback" not in codes(res)
    assert not http.calls(r"/login/index\.php")
    assert "Zugang: webservice" in res.categories[CAT].detail


def test_forced_webservice_does_not_fall_back(now):
    http = FakeHttp()
    add_ws(http, token=json_response(jf("token_service_disabled.json")))
    add_web(http)
    res = run(make(http, method="webservice"), now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.categories[CAT].error.code == "route_unavailable"
    assert res.login_error is None and not http.calls(r"/login/index\.php")


def test_forced_web_never_touches_the_token_endpoint(now):
    http = FakeHttp()
    add_ws(http)
    add_web(http)
    res = run(make(http, method="web"), now)
    assert res.complete and not http.calls(r"/login/token\.php") and not http.calls(r"/webservice/rest")


def test_both_routes_unavailable_is_failed(now):
    http = FakeHttp()
    add_ws(http, token=json_response(jf("token_service_disabled.json")))
    add_web(http, ajax_override=lambda call: ajax_err("servicenotavailable"))
    res = run(make(http, method="auto"), now)
    oc = res.categories[CAT]
    assert oc.status == CategoryStatus.FAILED and oc.error.code == "route_unavailable" and not res.complete
    assert "webservice" in oc.error.detail and "web" in oc.error.detail


# --------------------------------------------------------------------------- route 2: web session


def test_web_route_happy_path(now):
    adapter, http = web_adapter()
    res = run(adapter, now)
    assert res.complete and ids(res) == EXPECTED_IN_WINDOW
    it = {i.source_id: i for i in res.items}["assign:4101"]
    assert it.title == LONG_NAME and it.course == "Softwareentwicklung 4AHITN 2026/27"
    assert it.when.start == datetime(2026, 10, 16, 23, 59, tzinfo=VIENNA)
    assert it.url == f"{BASE}/mod/assign/view.php?id=4101"
    assert it.description.startswith("Erstellt das ER-Modell für die Schulbibliothek.")
    assert "Normalisierung bis 3NF" in it.description


def test_web_and_webservice_route_agree_on_core_fields(now):
    ws, _ = ws_adapter()
    web, _ = web_adapter()
    a = {i.source_id: i for i in run(ws, now).items}
    b = {i.source_id: i for i in run(web, now).items}
    assert a.keys() == b.keys()
    for k in a:
        assert (a[k].title, a[k].course, a[k].when, a[k].url) == (b[k].title, b[k].course, b[k].when, b[k].url)


def test_web_login_uses_the_login_form_token_and_posts_credentials_in_body(now):
    adapter, http = web_adapter()
    run(adapter, now)
    post = http.calls(r"/login/index\.php", "POST")[0]
    assert post.data == {"anchor": "", "logintoken": "dummy-logintoken-LOGIN", "username": "max.mustermann", "password": PASSWORD}
    assert PASSWORD not in post.url


def test_web_ajax_calls_carry_sesskey_and_are_json_batches(now):
    adapter, http = web_adapter()
    run(adapter, now)
    calls = http.calls(r"/lib/ajax/service\.php", "POST")
    assert calls
    for c in calls:
        assert c.params["sesskey"] == "dummy-sesskey-AbCd1234"
        assert c.params["info"] == c.json[0]["methodname"] and c.json[0]["index"] == 0 and len(c.json) == 1
    months = [c.json[0]["args"] for c in calls if c.json[0]["methodname"] == FN_MONTH]
    assert [(m["year"], m["month"]) for m in months] == [(2026, 10), (2026, 11), (2026, 12), (2027, 1)]
    assert all(m["courseid"] == 1 and m["includenavigation"] is False for m in months)


def test_web_route_ignores_noise_events_and_foreign_courses(now):
    adapter, _ = web_adapter()
    res = run(adapter, now)
    assert "assign:4999" not in ids(res) and "assign:4101" in ids(res)
    # the gradingdue event of 4101 (20 Oct 09:00) must not replace the due date
    assert {i.source_id: i for i in res.items}["assign:4101"].when.start.day == 16


def test_web_route_tracked_shifted_deadline_is_found_in_later_month(now):
    adapter, http = web_adapter()
    res = run(adapter, now, [tracked_ref(4109, date(2026, 11, 20))])
    it = {i.source_id: i for i in res.items}["assign:4109"]
    assert it.when.start == datetime(2026, 12, 15, 8, 0, tzinfo=VIENNA)


def test_web_route_override_event_without_course_uses_course_of_sibling_event(now):
    events = jf("calendar_due_events.json") + [jf("calendar_override_event.json")]
    adapter, _ = web_adapter(events=events)
    res = run(adapter, now)
    it = {i.source_id: i for i in res.items}["assign:4102"]
    assert it.when.start == datetime(2026, 10, 27, 12, 0, tzinfo=VIENNA)  # personal date wins
    assert it.course == "Softwareentwicklung 4AHITN 2026/27"
    assert any("mehrere Fristen" in n for n in it.notes)
    assert any(n.code == "incomplete_data" and n.severity == Severity.WARNING for n in res.notices)


def test_web_route_override_only_event_has_unknown_course_and_says_so(now):
    events = jf("calendar_due_events.json") + [jf("calendar_override_only_event.json")]
    adapter, _ = web_adapter(events=events)
    res = run(adapter, now)
    it = {i.source_id: i for i in res.items}["assign:4110"]
    assert it.course is None and any("Kursname" in n for n in it.notes)
    assert any(n.code == "incomplete_data" and n.uid == it.uid for n in res.notices)


def test_web_route_unknown_course_is_skipped_when_a_course_filter_is_configured(now):
    events = jf("calendar_due_events.json") + [jf("calendar_override_only_event.json")]
    adapter, _ = web_adapter(events=events, courses=("7001",))
    res = run(adapter, now)
    assert "assign:4110" not in ids(res)
    assert any(n.code == "incomplete_data" and "Kursauswahl" in n.message for n in res.notices)


def test_web_route_course_filter(now):
    adapter, _ = web_adapter(courses=("7002",))
    assert ids(run(adapter, now)) == ["assign:4108", "assign:4103", "assign:4106"]


def test_web_route_session_without_user_is_credential_failure(now):
    adapter, http = web_adapter(dashboard=tf("dashboard_loggedout.html"))
    res = run(adapter, now)
    assert res.login_error is not None and res.login_error.code == "auth"
    assert not http.calls(r"/lib/ajax/service\.php")


def test_web_route_login_form_missing_is_schema_failure(now):
    adapter, _ = web_adapter(login_page="<html><body>Weiterleitung zum Bildungsportal</body></html>")
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.categories[CAT].error.code == "schema"
    assert res.login_error is None


def test_web_route_expired_session_mid_run_is_auth_failure(now):
    adapter, _ = web_adapter(ajax_override=lambda call: ajax_err("servicerequireslogin") if call["methodname"] == FN_COURSES else None)
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.categories[CAT].error.code == "auth"


def test_web_route_invalid_parameter_error_is_schema_failure(now):
    adapter, _ = web_adapter(ajax_override=lambda call: ajax_err("invalidparameter") if call["methodname"] == FN_COURSES else None)
    res = run(adapter, now)
    assert res.categories[CAT].error.code == "schema"


def test_web_route_one_failed_month_makes_result_incomplete(now):
    def override(call):
        if call["methodname"] == FN_MONTH and call["args"]["month"] == 11:
            return ajax_err("generalexceptionmessage")
        return None

    adapter, _ = web_adapter(ajax_override=override)
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.INCOMPLETE and not res.complete
    assert "11/2026" in res.categories[CAT].error.message


def test_web_route_all_months_failing_is_failed(now):
    adapter, _ = web_adapter(ajax_override=lambda call: ajax_err("generalexceptionmessage") if call["methodname"] == FN_MONTH else None)
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.items == []


def test_web_route_malformed_month_structure_is_schema_failure(now):
    adapter, _ = web_adapter(ajax_override=lambda call: ajax_ok({"weeks": "kaputt"}) if call["methodname"] == FN_MONTH else None)
    res = run(adapter, now)
    assert res.categories[CAT].status == CategoryStatus.FAILED and res.categories[CAT].error.code == "schema"


def test_months_to_scan_covers_window_and_shifted_deadlines():
    w = fetch_window(datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA))
    months, truncated = months_to_scan(w, [])
    assert months == [(2026, 10), (2026, 11), (2026, 12), (2027, 1)] and not truncated
    # As long as a known open assignment is tracked, a moved deadline can only be found where its NEW date lies: the web
    # route has no per-assignment lookup, so the whole documented range (24 months) is read (audit 2.9c / 3.4c; it used to
    # stop two months behind the latest tracked date, which left a deadline moved further out stale).
    months, truncated = months_to_scan(w, [date(2027, 3, 1)])
    assert len(months) == ev.WEB_MAX_MONTHS and months[0] == (2026, 10) and months[-1] == (2028, 9) and not truncated
    months, truncated = months_to_scan(w, [date(2031, 1, 1)])
    assert len(months) == ev.WEB_MAX_MONTHS and truncated
    months, _ = months_to_scan(w, [date(2026, 9, 20)])  # a tracked item that moved earlier
    assert months[0] == (2026, 9)


def test_parse_m_cfg():
    cfg = parse_m_cfg(tf("dashboard.html"))
    assert cfg["userId"] == 90210 and cfg["siteId"] == 1 and cfg["sesskey"].startswith("dummy-")
    with pytest.raises(SchemaError):
        parse_m_cfg("<html>nichts</html>")
    with pytest.raises(SchemaError):
        parse_m_cfg("M.cfg = {kaputt};")


# --------------------------------------------------------------------------- read-only guarantee


FORBIDDEN = re.compile(r"save|submit|update|delete|remove|add_|set_|create|insert|grade|lock|unlock|start_|finish|process|view_", re.I)


@pytest.mark.parametrize("method", ["auto", "webservice", "web"])
def test_only_read_requests_are_ever_sent(now, method):
    http = FakeHttp()
    add_ws(http)
    add_web(http)
    res = run(make(http, method=method), now, [tracked_ref(4109, date(2026, 12, 15))])
    assert res.complete
    for r in http.requests:
        assert r.method in ("GET", "POST")
        if r.method == "GET":
            assert not r.data and not r.json
            continue
        if re.search(r"/login/token\.php$", r.url) or re.search(r"/login/index\.php$", r.url):
            continue  # login POSTs
        if "/webservice/rest/server.php" in r.url:
            fn = r.data["wsfunction"]
            assert fn in READ_ONLY_WS_FUNCTIONS and not FORBIDDEN.search(fn.replace("get_", ""))
        elif "/lib/ajax/service.php" in r.url:
            fn = r.json[0]["methodname"]
            assert fn in READ_ONLY_AJAX_FUNCTIONS and not FORBIDDEN.search(fn.replace("get_", ""))
        else:
            pytest.fail(f"unexpected POST target {r.url}")


def test_allow_lists_contain_only_get_functions():
    for fn in READ_ONLY_WS_FUNCTIONS | READ_ONLY_AJAX_FUNCTIONS:
        assert "_get_" in fn
    assert not any("assign_" in f and "get" not in f for f in READ_ONLY_WS_FUNCTIONS | READ_ONLY_AJAX_FUNCTIONS)


def test_guard_refuses_functions_outside_the_allow_list_without_any_request():
    http = FakeHttp()
    ws = ev._WebserviceRoute(http, BASE, "u", PASSWORD)
    with pytest.raises(ev.EduvidualError):
        ws._call("mod_assign_save_submission", {"assignmentid": 1})
    web = ev._WebSessionRoute(http, BASE, "u", PASSWORD)
    with pytest.raises(ev.EduvidualError):
        web._ajax("core_calendar_delete_calendar_events", {})
    assert http.requests == []


# --------------------------------------------------------------------------- secrets


FAILURE_SCENARIOS = ["ok_ws", "ok_web", "invalid", "schema", "html", "fallback"]


@pytest.mark.parametrize("scenario", FAILURE_SCENARIOS)
def test_secrets_never_appear_in_notices_errors_details_or_logs(now, caplog, scenario):
    caplog.set_level(logging.DEBUG)
    http = FakeHttp()
    method = "auto"
    if scenario == "ok_ws":
        add_ws(http)
    elif scenario == "ok_web":
        add_web(http)
        method = "web"
    elif scenario == "invalid":
        add_ws(http, token=json_response(jf("token_invalid_login.json")))
    elif scenario == "schema":
        add_ws(http, assignments=json_response(jf("assignments_missing_duedate.json")))
    elif scenario == "html":
        add_ws(http, assignments=text_response(tf("login_page.html")))
    else:
        add_ws(http, token=json_response(jf("token_service_disabled.json")))
        add_web(http)
    adapter = make(http, method=method)
    res = run(adapter, now)
    assert_no_secrets(*all_texts(res), caplog.text, repr(adapter), str(res.login_error), str(res.categories[CAT].error))


def test_exceptions_do_not_contain_secrets():
    http = FakeHttp()
    add_ws(http, token=json_response({"error": f"falsch {PASSWORD}", "errorcode": "invalidlogin"}))
    route = ev._WebserviceRoute(http, BASE, "u", PASSWORD)
    with pytest.raises(AuthError) as e:
        route.login()
    assert_no_secrets(str(e.value), e.value.message, e.value.hint or "", e.value.detail or "")  # server text is not echoed


def test_adapter_repr_hides_password():
    assert PASSWORD not in repr(make(FakeHttp()))


# --------------------------------------------------------------------------- misc item rules


def test_unreadable_title_gets_placeholder_and_notice(now):
    data = jf("assignments.json")
    data["courses"][0]["assignments"][1]["name"] = "  <b> </b>  "
    adapter, _ = ws_adapter(assignments=json_response(data))
    res = run(adapter, now)
    it = {i.source_id: i for i in res.items}["assign:4102"]
    assert it.title == "Aufgabe 4102" and any("Titel" in n for n in it.notes)
    assert any(n.code == "incomplete_data" and n.severity == Severity.WARNING for n in res.notices)


def test_title_entities_and_tags_are_normalised(now):
    data = jf("assignments.json")
    data["courses"][0]["assignments"][1]["name"] = "Übung&nbsp;5:  <span>SQL</span> &amp; Co"
    adapter, _ = ws_adapter(assignments=json_response(data))
    assert {i.source_id: i for i in run(adapter, now).items}["assign:4102"].title == "Übung 5: SQL & Co"


def test_unsupported_module_type_is_reported_not_silently_dropped(now):
    adapter, _ = ws_adapter(modules=("assign", "quiz"))
    res = run(adapter, now)
    n = [x for x in res.notices if x.code == "unsupported_module"]
    assert len(n) == 1 and "quiz" in n[0].message and res.complete


def test_two_fetches_give_identical_item_hashes(now):
    a, _ = ws_adapter()
    b, _ = ws_adapter()
    h1 = [i.content_hash() for i in run(a, now).items]
    h2 = [i.content_hash() for i in run(b, now + timedelta(hours=3)).items]
    assert h1 == h2  # no volatile data in items


def test_item_roundtrips_through_models_serialisation(now):
    adapter, _ = ws_adapter()
    from schulsync.models import SourceItem

    for it in run(adapter, now).items:
        assert SourceItem.from_dict(it.to_dict()) == it


# --------------------------------------------------------------------------- timestamps


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (datetime(2026, 3, 29, 0, 59, 59, tzinfo=UTC), "2026-03-29T01:59:59+01:00"),  # last second of winter time
        (datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC), "2026-03-29T03:00:00+02:00"),  # 02:00 does not exist
        (datetime(2026, 10, 24, 22, 0, 0, tzinfo=UTC), "2026-10-25T00:00:00+02:00"),
        (datetime(2026, 10, 25, 0, 59, 59, tzinfo=UTC), "2026-10-25T02:59:59+02:00"),
        (datetime(2026, 10, 25, 1, 0, 0, tzinfo=UTC), "2026-10-25T02:00:00+01:00"),  # second 02:00 (fold)
        (datetime(2026, 1, 15, 22, 59, 0, tzinfo=UTC), "2026-01-15T23:59:00+01:00"),
        (datetime(2026, 7, 15, 21, 59, 0, tzinfo=UTC), "2026-07-15T23:59:00+02:00"),
    ],
)
def test_timestamps_become_aware_vienna_datetimes(utc, expected):
    got = ts_to_vienna(int(utc.timestamp()))
    assert got.tzinfo is not None and got.utcoffset() is not None
    assert got.isoformat() == expected


def test_items_around_the_october_dst_change_keep_their_wall_clock_time(now):
    adapter, _ = ws_adapter()
    items = {i.source_id: i for i in run(adapter, now).items}
    before, after = items["assign:4102"].when.start, items["assign:4103"].when.start
    assert (before.hour, before.utcoffset()) == (12, timedelta(hours=2))  # 24 Oct, summer time
    assert (after.hour, after.utcoffset()) == (10, timedelta(hours=1))  # 26 Oct, winter time


# --------------------------------------------------------------------------- html -> text


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("<p></p>", ""),
        ("Nur Text", "Nur Text"),
        ("<p>Absatz eins</p><p>Absatz zwei</p>", "Absatz eins\n\nAbsatz zwei"),
        ("Zeile 1<br>Zeile 2<br/><br>Zeile 4", "Zeile 1\nZeile 2\n\nZeile 4"),
        ("<p>a &amp; b &lt;c&gt; &auml;&ouml;&uuml; &nbsp;d</p>", "a & b <c> äöü d"),
        ("<ul><li>eins</li><li>zwei</li></ul>", "- eins\n- zwei"),
        ("<p>x</p><ul><li>a</li><li>b</li></ul><p>y</p>", "x\n\n- a\n- b\n\ny"),
        ("<ol><li>eins<ul><li>a</li></ul></li><li>zwei</li></ol>", "- eins\n- a\n- zwei"),
        ('Text <a href="https://x.at/a">Link</a>.', "Text Link (https://x.at/a)."),
        ('<a href="https://x.at/a">https://x.at/a</a>', "https://x.at/a"),  # URL not repeated
        ('<a href="javascript:alert(1)">klick</a>', "klick"),  # unsafe link target dropped, text kept
        ('<a href="https://x.at/?token=abc">Link</a>', "Link"),  # secret-looking URL not copied
        ("<script>evil()</script><style>p{}</style>sichtbar", "sichtbar"),
        ('<img src="x.png" alt="Bild"><p>nur Text</p>', "nur Text"),  # images are not interpreted
        ("<table><tr><th>Tag</th><th>Zeit</th></tr><tr><td>Mo</td><td>08:00</td></tr></table>", "Tag Zeit\nMo 08:00"),
        ("<div>  viel \n\n  Leerraum\t hier </div>", "viel Leerraum hier"),
        ("<p>unclosed <b>fett", "unclosed fett"),
    ],
)
def test_html_to_text(markup, expected):
    assert html_to_text(markup) == expected


def test_html_to_text_is_deterministic():
    src = '<div><p>Hallo <em>Welt</em></p><ul><li><a href="https://a.at/x">A</a></li></ul></div>'
    assert html_to_text(src) == html_to_text(src)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://www.eduvidual.at/mod/assign/view.php?id=4101", True),
        ("http://example.org/x", True),
        ("javascript:alert(1)", False),
        ("ftp://example.org/x", False),
        ("https://user:pw@example.org/x", False),
        ("https://example.org/x?sesskey=abc", False),
        ("https://example.org/x?wstoken=abc", False),
        ("//example.org/x", False),
        ("", False),
        (None, False),
    ],
)
def test_safe_http_url(url, ok):
    assert (safe_http_url(url) is not None) is ok


# --------------------------------------------------------------------------- check()


@pytest.fixture
def frozen_now(monkeypatch, now):
    monkeypatch.setattr(ev, "now_vienna", lambda: now)
    return now


def test_check_webservice_route_reports_every_step_in_german(frozen_now):
    http = FakeHttp()
    add_public_config(http)
    add_ws(http)
    report = make(http, method="auto").check()
    assert report.ok and report.source == Source.EDUVIDUAL
    names = [s.name for s in report.steps]
    assert names[:2] == ["Eduvidual erreichbar", "Web-Service verfügbar"]
    assert "Anmeldung" in names and "Kurse lesen" in names and "Aufgaben lesen" in names
    text = {s.name: s.detail for s in report.steps}
    assert "Web-Service-Token" in text["Anmeldung"] and "4.5.2" in text["Anmeldung"]
    assert "4 aktuelle Kurse" in text["Kurse lesen"]
    assert "9 Aufgaben gelesen, davon 8 mit Frist, 5 im Prüffenster" in text["Aufgaben lesen"]
    oc = report.categories[CAT]
    assert oc.status == CategoryStatus.OK and oc.item_count == 5 and "webservice" in oc.detail
    assert_no_secrets(*(s.detail for s in report.steps))


def test_check_with_unavailable_webservice_uses_web_route_and_says_so(frozen_now):
    http = FakeHttp()
    add_public_config(http, enablewebservices=0)
    add_ws(http, token=json_response(jf("token_service_disabled.json")))
    add_web(http)
    report = make(http, method="auto").check()
    assert report.ok
    text = {s.name: s.detail for s in report.steps}
    assert "abgeschaltet" in text["Web-Service verfügbar"] and "Weboberfläche" in text["Web-Service verfügbar"]
    assert "Weboberfläche" in text["Anmeldung"]
    assert "Zugangsart „webservice“" in [s.name for s in report.steps]
    assert "nicht sichtbar" in text["Aufgaben lesen"]  # web route cannot list assignments without due date


def test_check_forced_webservice_with_disabled_service_is_not_ok(frozen_now):
    http = FakeHttp()
    add_public_config(http, enablemobilewebservice=0)
    add_ws(http, token=json_response(jf("token_service_disabled.json")))
    report = make(http, method="webservice").check()
    assert not report.ok
    assert [s.ok for s in report.steps if s.name == "Web-Service verfügbar"] == [False]


def test_check_invalid_credentials(frozen_now):
    http = FakeHttp()
    add_public_config(http)
    add_ws(http, token=json_response(jf("token_invalid_login.json")))
    add_web(http)
    report = make(http, method="auto").check()
    login = [s for s in report.steps if s.name == "Anmeldung"][0]
    assert not login.ok and "Passwort" in login.detail
    assert not report.ok and report.categories[CAT].status == CategoryStatus.FAILED
    assert len(http.calls(r"/login/token\.php")) == 1 and not http.calls(r"/login/index\.php")
    assert_no_secrets(*(s.detail for s in report.steps))


def test_check_server_unreachable(frozen_now):
    http = FakeHttp()
    http.fail_network = True
    report = make(http).check()
    assert not report.ok and report.steps[0].name == "Eduvidual erreichbar" and not report.steps[0].ok
    assert len(report.steps) == 1


def test_check_maintenance_mode_is_reported(frozen_now):
    http = FakeHttp()
    add_public_config(http, maintenanceenabled=1)
    add_ws(http)
    report = make(http).check()
    assert not report.steps[0].ok and "Wartungsmodus" in report.steps[0].detail


def test_check_public_config_html_instead_of_json(frozen_now):
    http = FakeHttp()
    http.add("GET", r"/lib/ajax/service-nologin\.php", text_response("<html>Fehlerseite</html>"))
    report = make(http).check()
    assert not report.ok and not report.steps[0].ok


def test_check_reads_only(frozen_now):
    http = FakeHttp()
    add_public_config(http)
    add_ws(http)
    make(http).check()
    assert all(r.method in ("GET", "POST") for r in http.requests)
    for r in http.calls(r"/webservice/rest/server\.php"):
        assert r.data["wsfunction"] in READ_ONLY_WS_FUNCTIONS


# --------------------------------------------------------------------------- construction


def test_default_http_client_honours_retry_timeouts(monkeypatch):
    seen = []

    class Spy:
        def __init__(self, connect_timeout, read_timeout):
            seen.append((connect_timeout, read_timeout))

    monkeypatch.setattr(ev, "RequestsHttp", Spy)
    EduvidualAdapter(EduvidualConfig(username="u"), "pw", retry=RetryConfig(connect_timeout=3.0, read_timeout=7.0))._http_factory()
    EduvidualAdapter(EduvidualConfig(username="u"), "pw")._http_factory()
    assert seen == [(3.0, 7.0), (RetryConfig().connect_timeout, RetryConfig().read_timeout)]


def test_adapter_declares_its_source():
    assert EduvidualAdapter.source == Source.EDUVIDUAL


def test_base_url_trailing_slash_is_tolerated(now):
    http = FakeHttp()
    add_ws(http)
    res = run(make(http, method="webservice", base_url="https://www.eduvidual.at/"), now)
    assert res.complete and res.items[0].url.startswith("https://www.eduvidual.at/mod/assign/view.php?id=")
    assert all("//login" not in r.url and "at//" not in r.url for r in http.requests)


def test_submission_status_is_read_per_assignment_and_unknown_on_errors(now):
    """mod_assign_get_submission_status (read-only) -> SourceItem.submitted/attempt; a refused call means 'unknown'."""
    adapter, http = ws_adapter(submissions={301: ("submitted", 0), 302: ("reopened", 1)})
    items = {i.source_id: i for i in run(adapter, now).items}
    assert (items["assign:4101"].submitted, items["assign:4101"].attempt) == (True, 0)
    assert (items["assign:4102"].submitted, items["assign:4102"].attempt) == (False, 1)
    assert items["assign:4103"].submitted is False  # default "new"

    def refuse(req):
        if req.data["wsfunction"] == FN_SUBMISSION:
            return json_response({"exception": "required_capability_exception", "errorcode": "nopermissions"})
        return None

    adapter, http = ws_adapter(rest=refuse)
    res = run(adapter, now)
    assert res.complete and all(i.submitted is None and i.attempt is None for i in res.items)
