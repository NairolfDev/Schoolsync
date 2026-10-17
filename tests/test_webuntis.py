"""Tests für den WebUntis-Adapter (src/schulsync/adapters/webuntis.py).

EHRLICHKEIT: Alle Antworten hier sind SYNTHETISCH ("synthetic, not recorded"): von Hand nach den in der Moduldoku
zitierten Strukturen (python-webuntis, SchoolUtils/WebUntis, stundenglas) gebaut, mit Fantasienamen. Es wurde nie gegen
ein echtes WebUntis-Konto getestet. Die Tests belegen die Fachregeln und die Fehlerbehandlung des Adapters, NICHT dass
WebUntis tatsächlich so antwortet; das zeigt erst `schulsync check` auf dem Zielsystem.

Referenzzeitpunkt: Mittwoch 2026-10-14 08:00 Europe/Vienna, Prüffenster 14.10. 00:00 bis 04.11. 00:00 (exklusiv).
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from schulsync.adapters import webuntis as wu
from schulsync.adapters.webuntis import WebUntisAdapter
from schulsync.config import WebUntisConfig
from schulsync.errors import NetworkError, SchemaError, TransientError
from schulsync.http import HttpResponse
from schulsync.models import (
    Category,
    CategoryStatus,
    ItemStatus,
    ItemTime,
    Kind,
    Severity,
    Source,
    SourceItem,
    TrackedRef,
    make_uid,
)
from schulsync.timeutil import VIENNA, Window, fetch_window, local_midnight, make_local
from tests.support.fake_http import FakeHttp, json_response, redirect, text_response

FIX = Path(__file__).parent / "fixtures" / "webuntis"
BASE = "https://htbla-grieskirchen.webuntis.com"
PW = "hunter2-dummy"
SESSION_ID = "DUMMY-SESSION-0001"  # = sessionId in jsonrpc_authenticate_ok.json
BEARER = "dummy-bearer-" + "x" * 60
NOW = datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA)
CFG = WebUntisConfig(username="beispiel.max")


def fx(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def rpc_ok(result: Any) -> HttpResponse:
    return json_response({"jsonrpc": "2.0", "id": "SchulSync", "result": result})


def rpc_err(code: int, message: str = "server text must never be copied") -> HttpResponse:
    return json_response({"jsonrpc": "2.0", "id": "SchulSync", "error": {"message": message, "code": code}})


def html_page(status: int = 200) -> HttpResponse:
    return text_response("<html><body><form>Anmeldung</form></body></html>", status, {"content-type": "text/html"})


def in_range(value: int, params: dict) -> bool:
    return int(params["startDate"]) <= value <= int(params["endDate"])


class World:
    """Fake-WebUntis. Jede Route ruft das aktuelle Attribut auf; Tests ersetzen einzelne Handler."""

    def __init__(self) -> None:
        self.http = FakeHttp()
        self.http.cookies["JSESSIONID"] = SESSION_ID
        self.homeworks = fx("rest_homeworks.json")
        self.exams = fx("rest_exams.json")
        self.lessons = fx("jsonrpc_timetable.json")["result"]
        self.rest_tt = fx("rest_timetable_entries.json")
        # Handler (req -> HttpResponse)
        self.start: Callable = lambda r: text_response("<html>WebUntis</html>")
        self.authenticate: Callable = lambda body: json_response(fx("jsonrpc_authenticate_ok.json"))
        self.school_years: Callable = lambda body: rpc_ok(fx("jsonrpc_schoolyears.json")["result"])
        self.get_timetable: Callable = self._timetable
        self.homework: Callable = self._homework
        self.exam: Callable = self._exam
        self.form: Callable = lambda r: redirect("/WebUntis/index.do")
        self.token: Callable = lambda r: text_response(BEARER)
        self.app_data: Callable = lambda r: json_response(fx("rest_app_data.json"))
        self.tt_rest: Callable = self._tt_rest
        self.raw_rpc: Callable | None = None  # ersetzt jsonrpc.do komplett (z. B. HTML statt JSON-RPC)
        h = self.http
        h.add("GET", r"/WebUntis/\?school=", lambda r: self.start(r))
        h.add("POST", r"/WebUntis/jsonrpc\.do", self._rpc)
        h.add("POST", r"/WebUntis/j_spring_security_check", lambda r: self.form(r))
        h.add("GET", r"/api/token/new", lambda r: self.token(r))
        h.add("GET", r"/api/rest/view/v1/app/data", lambda r: self.app_data(r))
        h.add("GET", r"/api/rest/view/v1/timetable/entries", lambda r: self.tt_rest(r))
        h.add("GET", r"/api/homeworks/lessons", lambda r: self.homework(r))
        h.add("GET", r"/api/exams", lambda r: self.exam(r))

    def _rpc(self, req) -> HttpResponse:
        if self.raw_rpc is not None:
            return self.raw_rpc(req)
        body = req.json
        method = body["method"]
        handler = {"authenticate": self.authenticate, "getSchoolyears": self.school_years, "getTimetable": self.get_timetable}
        if method not in handler:
            raise AssertionError(f"unerwartete JSON-RPC-Methode {method}")
        return handler[method](body)

    def _timetable(self, body) -> HttpResponse:
        opts = body["params"]["options"]
        got = [l for l in self.lessons if opts["startDate"] <= l["date"] <= opts["endDate"]]
        return rpc_ok(got)

    def _homework(self, req) -> HttpResponse:
        hws = [h for h in self.homeworks["data"]["homeworks"] if h["dueDate"] == 0 or in_range(h["dueDate"], req.params)]
        return json_response({"data": {"homeworks": hws, "lessons": self.homeworks["data"]["lessons"]}})

    def _exam(self, req) -> HttpResponse:
        return json_response({"data": {"exams": [e for e in self.exams["data"]["exams"] if in_range(e["examDate"], req.params)]}})

    def _tt_rest(self, req) -> HttpResponse:
        a, b = req.params["start"], req.params["end"]
        days = [d for d in self.rest_tt["days"] if a <= d["date"] <= b]
        return json_response({**self.rest_tt, "days": days})

    # -- Auswertung
    def methods(self) -> list[str]:
        return [r.json["method"] for r in self.http.calls(r"jsonrpc\.do", "POST")]


def adapter(w: World, cfg: WebUntisConfig = CFG, now: datetime = NOW) -> WebUntisAdapter:
    return WebUntisAdapter(cfg, PW, http_factory=lambda: w.http, clock=lambda: now)


def run(w: World, tracked=(), now: datetime = NOW, cfg: WebUntisConfig = CFG, window: Window | None = None):
    return adapter(w, cfg, now).fetch(window or fetch_window(now), list(tracked), now)


def by_kind(result, kind: Kind) -> dict[str, SourceItem]:
    return {i.source_id: i for i in result.items if i.kind == kind}


def ref(kind: Kind, source_id: str, start, end=None, cancelled=False) -> TrackedRef:
    return TrackedRef(Source.WEBUNTIS, kind, source_id, start, end, cancelled)


def lesson(lid: int, day: int, start: int, end: int, code: str | None = None, subject: str = "Deutsch", **extra) -> dict:
    d = {
        "id": lid, "lsnumber": lid, "date": day, "startTime": start, "endTime": end,
        "kl": [{"id": 88, "name": "4AHIT"}], "te": [{"id": 8, "name": "NIX"}],
        "su": [{"id": 10, "name": subject[:2].upper(), "longname": subject}], "ro": [{"id": 23, "name": "R1"}],
    }
    if code:
        d["code"] = code
    d.update(extra)
    return d


# =============================================================================== Anmeldung


def test_rpc_login_single_post_with_credentials_only_in_body():
    w = World()
    result = run(w)
    auth = [r for r in w.http.calls(r"jsonrpc\.do", "POST") if r.json["method"] == "authenticate"]
    assert len(auth) == 1
    assert auth[0].json["params"] == {"user": "beispiel.max", "password": PW, "client": "SchulSync"}
    assert result.login_error is None and result.complete
    assert w.http.calls(r"j_spring_security_check") == []  # kein zweiter Weg, solange JSON-RPC antwortet


def test_bad_credentials_one_attempt_never_retried():
    w = World()
    w.authenticate = lambda body: rpc_err(-8504)
    a = adapter(w)
    r1 = a.fetch(fetch_window(NOW), [], NOW)
    r2 = a.fetch(fetch_window(NOW), [], NOW)
    r3 = a.check()
    assert r1.login_error is not None and r1.login_error.code == "auth"
    assert not r1.complete and r1.items == []
    assert set(r1.categories) == {Category.HOMEWORK, Category.EXAMS, Category.CANCELLATIONS}
    assert all(c.status == CategoryStatus.FAILED for c in r1.categories.values())
    assert r2.login_error == r1.login_error
    assert not r3.ok
    assert w.methods().count("authenticate") == 1  # genau EIN Versuch, auch bei späteren Aufrufen
    assert w.http.calls(r"j_spring_security_check") == []  # keine zweite Anmeldeart nach inhaltlicher Ablehnung


def test_unknown_login_error_code_is_auth_error_without_server_text():
    w = World()
    w.authenticate = lambda body: rpc_err(-8999, "geheimer Servertext")
    res = run(w)
    assert res.login_error.code == "auth"
    assert "geheimer Servertext" not in repr(res)
    assert w.http.calls(r"j_spring_security_check") == []


def test_login_result_without_session_id_is_rejected():
    w = World()
    w.authenticate = lambda body: rpc_ok({"code": 5})
    assert run(w).login_error.code == "auth"


def test_non_student_account_is_rejected():
    w = World()
    ok = fx("jsonrpc_authenticate_ok.json")
    ok["result"]["personType"] = 2
    w.authenticate = lambda body: json_response(ok)
    res = run(w)
    assert res.login_error.code == "schema" and "kein Schülerkonto" in res.login_error.message


def test_missing_session_cookie_after_login_is_schema_error():
    w = World()
    w.http.cookies.clear()
    assert run(w).login_error.code == "schema"


def test_rpc_login_server_error_is_transient_and_not_remembered_as_rejection():
    w = World()
    calls = {"n": 0}

    def flaky(body):
        calls["n"] += 1
        return text_response("", 503) if calls["n"] == 1 else json_response(fx("jsonrpc_authenticate_ok.json"))

    w.authenticate = flaky
    a = adapter(w)
    with pytest.raises(TransientError):
        a.fetch(fetch_window(NOW), [], NOW)
    assert a.fetch(fetch_window(NOW), [], NOW).complete  # Netzproblem ist keine Ablehnung: neuer Versuch erlaubt


def test_network_down_propagates_and_check_reports_it():
    w = World()
    w.http.fail_network = True
    with pytest.raises(NetworkError):
        run(w)
    probe = adapter(w).check()
    assert not probe.ok and probe.steps[0].name == "Erreichbarkeit" and not probe.steps[0].ok


@pytest.mark.parametrize("rpc_answer", ["html", "404", "-32601", "redirect"])
def test_form_login_only_when_jsonrpc_does_not_speak_json_rpc(rpc_answer):
    w = World()
    w.raw_rpc = {
        "html": lambda r: html_page(),
        "404": lambda r: text_response("", 404),
        "-32601": lambda r: rpc_err(-32601),
        "redirect": lambda r: redirect("/WebUntis/index.do"),
    }[rpc_answer]
    res = run(w)
    forms = w.http.calls(r"j_spring_security_check", "POST")
    assert len(forms) == 1
    assert forms[0].data == {"school": "htbla-grieskirchen", "j_username": "beispiel.max", "j_password": PW, "token": ""}
    assert res.login_error is None and res.complete
    # Entfälle kommen dann über den REST-Stundenplan (JSON-RPC wird nicht erneut versucht)
    assert w.methods() == ["authenticate"]
    assert w.http.calls(r"timetable/entries")
    assert "REST timetable/entries" in res.categories[Category.CANCELLATIONS].detail


def test_form_login_route_reported_in_check():
    w = World()
    w.raw_rpc = lambda r: html_page()
    probe = adapter(w).check()
    assert probe.ok
    assert "Webformular" in next(s for s in probe.steps if s.name == "Anmeldung").detail


def test_form_login_rejected_when_redirected_elsewhere():
    w = World()
    w.raw_rpc = lambda r: html_page()
    w.form = lambda r: redirect("/WebUntis/login/error")
    a = adapter(w)
    res = a.fetch(fetch_window(NOW), [], NOW)
    a.fetch(fetch_window(NOW), [], NOW)
    assert res.login_error.code == "auth"
    assert len(w.http.calls(r"j_spring_security_check")) == 1
    assert w.http.calls(r"api/token/new") == []


def test_form_login_rejected_when_no_session_arises():
    """Erfolgs- und Ablehnungs-Weiterleitung können beide auf index.do zeigen; maßgeblich ist, ob token/new eine Sitzung sieht."""
    w = World()
    w.raw_rpc = lambda r: html_page()
    w.token = lambda r: redirect("/WebUntis/index.do")
    assert run(w).login_error.code == "auth"


def test_form_login_two_factor_is_clear_error():
    w = World()
    w.raw_rpc = lambda r: html_page()
    w.form = lambda r: json_response({"state": "TOKEN_REQUIRED"})
    res = run(w)
    assert res.login_error.code == "auth" and "Zwei-Faktor" in res.login_error.message


def test_form_login_403_is_login_failure_not_exception():
    w = World()
    w.raw_rpc = lambda r: html_page()
    w.form = lambda r: text_response("", 403)
    res = run(w)
    assert res.login_error.code == "access_denied" and not res.complete


# =============================================================================== Happy paths


def test_full_fetch_happy_path_counts_and_routes():
    w = World()
    res = run(w)
    assert res.complete and res.login_error is None
    assert {c: o.item_count for c, o in res.categories.items()} == {
        Category.HOMEWORK: 2, Category.EXAMS: 2, Category.CANCELLATIONS: 2,
    }
    assert all(o.status == CategoryStatus.OK for o in res.categories.values())
    assert "REST /api/homeworks/lessons" in res.categories[Category.HOMEWORK].detail
    assert "REST /api/exams" in res.categories[Category.EXAMS].detail
    assert "JSON-RPC getTimetable" in res.categories[Category.CANCELLATIONS].detail
    assert len(res.items) == 6


def test_homework_items():
    hw = by_kind(run(World()), Kind.HOMEWORK)
    assert set(hw) == {"9001", "9002"}  # 9004 abgelaufen, 9003 ausserhalb, 9005 ohne Datum
    a = hw["9001"]
    assert a.uid == "webuntis:homework:9001" and a.id_stable
    assert a.when == ItemTime(date(2026, 10, 16)) and a.when.all_day
    assert a.course == "AM" and a.title == "Übungsblatt 4,"
    assert a.description == "Übungsblatt 4,\nAufgaben 1-6 lösen\n\nAnmerkung: Bitte sauber ausarbeiten"
    assert a.notes == () and a.status == ItemStatus.ACTIVE and a.teacher is None and a.room is None
    b = hw["9002"]
    assert b.title == "Gedichtanalyse & Interpretation" and b.course == "D"
    assert b.description == "Gedichtanalyse & Interpretation\n\nSeite 42"  # <p>-Absätze -> Leerzeile, Entität aufgelöst
    assert any("Anhänge" in n for n in b.notes)
    assert b.when == ItemTime(date(2026, 10, 20))  # `completed` wird nie ausgewertet


def test_homework_without_due_date_is_notice_not_item():
    res = run(World())
    notices = [n for n in res.notices if n.code == "no_date"]
    assert len(notices) == 1 and notices[0].severity == Severity.WARNING
    assert notices[0].title == "Ohne Datum" and notices[0].source == Source.WEBUNTIS
    assert "9005" not in by_kind(res, Kind.HOMEWORK)


def test_exam_items():
    ex = by_kind(run(World()), Kind.EXAM)
    assert set(ex) == {"5501", "5502"}  # 5503 liegt ausserhalb des Fensters
    a = ex["5501"]
    assert a.uid == "webuntis:exam:5501"
    assert a.subtype == "Schularbeit" and a.title == "2. Schularbeit" and a.course == "AM"
    assert a.when == ItemTime(make_local(date(2026, 10, 21), datetime(2026, 10, 21, 8, 0).time()),
                              make_local(date(2026, 10, 21), datetime(2026, 10, 21, 9, 50).time()))
    assert a.when.start.tzinfo is not None and a.when.end is not None
    assert (a.teacher, a.room, a.description) == ("MUS", "R101", "Stoff: Kapitel 3-5")
    b = ex["5502"]  # keine Uhrzeit -> ganztägig; Titel aus dem Fach, weil kein Name
    assert b.when == ItemTime(date(2026, 10, 27)) and b.subtype == "Test" and b.title == "NWP"
    assert any("keine Uhrzeit" in n for n in b.notes)
    assert (b.teacher, b.room, b.description) == (None, None, None)


def test_exam_with_equal_start_and_end_has_unknown_end():
    ex = by_kind(run(World(), tracked=[ref(Kind.EXAM, "5503", make_local(date(2026, 11, 10), datetime(2026, 11, 10, 10, 30).time()))]), Kind.EXAM)
    c = ex["5503"]
    assert c.when.end is None and c.when.start.hour == 10 and c.when.start.minute == 30
    assert any("Endzeit" in n and "unplausibel" in n for n in c.notes)


def test_exam_iso_shape_and_bare_envelope():
    w = World()
    w.exam = lambda r: json_response(fx("rest_exams_iso_bare.json"))
    ex = by_kind(run(w), Kind.EXAM)["5601"]
    assert ex.when.start == datetime(2026, 10, 22, 10, 30, tzinfo=VIENNA)
    assert ex.when.end == datetime(2026, 10, 22, 12, 20, tzinfo=VIENNA)
    assert ex.subtype == "Schularbeit" and ex.teacher == "BEI" and ex.room == "R102"


def test_homework_bare_envelope():
    w = World()
    w.homework = lambda r: json_response({"homeworks": [fx("rest_homeworks.json")["data"]["homeworks"][0]]})
    hw = by_kind(run(w), Kind.HOMEWORK)
    assert set(hw) == {"9001"} and hw["9001"].course is None  # ohne lessons[] kein Fach (nichts erfunden)


def test_cancellation_items():
    ca = by_kind(run(World()), Kind.CANCELLATION)
    assert set(ca) == {"701002-20261014-0855", "701005-20261016-1350"}  # 701099 ausserhalb, 701003/701004/701001 keine Entfälle
    a = ca["701002-20261014-0855"]
    assert a.uid == "webuntis:cancellation:701002-20261014-0855"
    assert a.when == ItemTime(datetime(2026, 10, 14, 8, 55, tzinfo=VIENNA), datetime(2026, 10, 14, 9, 45, tzinfo=VIENNA))
    assert (a.title, a.course, a.teacher, a.room) == ("Deutsch", "Deutsch", "Beispiel Berta", "R102")
    assert a.description == "Vertretungstext: Lehrkraft erkrankt"  # Whitespace normalisiert
    assert a.status == ItemStatus.ACTIVE and a.id_stable and a.subtype is None
    b = ca["701005-20261016-1350"]
    assert b.when.start == datetime(2026, 10, 16, 13, 50, tzinfo=VIENNA) and b.when.end == datetime(2026, 10, 16, 14, 40, tzinfo=VIENNA)
    assert b.description is None and b.title == "Programmieren und Softwareentwicklung"


def test_cancelled_vs_substitution_vs_room_change_vs_regular():
    w = World()
    w.lessons = [
        lesson(1, 20261014, 800, 850),  # regulär
        lesson(2, 20261014, 855, 945, "cancelled"),  # Entfall
        lesson(3, 20261015, 800, 850, "irregular", substText="Vertretung durch TES"),  # Supplierung
        lesson(4, 20261015, 1030, 1120, ro=[{"id": 23, "name": "EDV2", "orgid": 24}]),  # Raumwechsel (orgid)
        lesson(5, 20261016, 800, 850, "irregular", te=[{"id": 7, "name": "TES", "orgid": 8}]),  # Lehrkraftwechsel
        lesson(6, 20261016, 900, 950, "somethingnew"),  # unbekannter Code -> nie raten
    ]
    assert set(by_kind(run(w), Kind.CANCELLATION)) == {"2-20261014-0855"}


def test_rest_route_cancelled_vs_changed_vs_event():
    w = World()
    w.get_timetable = lambda body: rpc_err(-32601)  # JSON-RPC nicht nutzbar -> Weg 2
    res = run(w)
    ca = by_kind(res, Kind.CANCELLATION)
    assert set(ca) == {"701002-20261014-0855"}  # CHANGED, REGULAR und EVENT sind keine Entfälle
    a = ca["701002-20261014-0855"]
    assert a.when == ItemTime(datetime(2026, 10, 14, 8, 55, tzinfo=VIENNA), datetime(2026, 10, 14, 10, 35, tzinfo=VIENNA))
    assert a.title == "Deutsch" and a.teacher == "Beispiel Berta"  # "?" -> Name aus `removed`
    assert any("2 aufeinanderfolgende" in n for n in a.notes)
    assert a.description == "Vertretungstext: Lehrkraft erkrankt"
    detail = res.categories[Category.CANCELLATIONS].detail
    assert "REST timetable/entries" in detail and "JSON-RPC getTimetable nicht nutzbar" in detail
    # Bearer wird geholt (einmal), nie in einer URL, nur im Header der REST-Aufrufe
    assert len(w.http.calls(r"api/token/new")) == 1 and len(w.http.calls(r"v1/app/data")) == 1
    for r in w.http.calls(r"/api/rest/view/v1/"):
        assert r.headers["Authorization"] == f"Bearer {BEARER}"
        if "timetable" in r.url:
            assert r.params["resources"] == 4711 and r.params["resourceType"] == "STUDENT"


@pytest.mark.parametrize("rpc_failure", [lambda b: rpc_err(-8509), lambda b: rpc_err(-32601), lambda b: text_response("", 404)])
def test_rpc_timetable_unusable_falls_back_to_rest(rpc_failure):
    w = World()
    w.get_timetable = rpc_failure
    res = run(w)
    assert res.complete and set(by_kind(res, Kind.CANCELLATION)) == {"701002-20261014-0855"}


def test_both_timetable_routes_failing_is_failed_with_both_routes_named():
    w = World()
    w.get_timetable = lambda b: rpc_err(-8509)
    w.tt_rest = lambda r: text_response("", 404)
    res = run(w)
    c = res.categories[Category.CANCELLATIONS]
    assert c.status == CategoryStatus.FAILED and "Weg 1" in c.error.detail and "-8509" in c.error.detail
    assert res.categories[Category.HOMEWORK].status == CategoryStatus.OK  # andere Kategorien unberührt
    assert not res.complete


def test_rest_errors_array_is_incomplete_not_empty():
    w = World()
    w.get_timetable = lambda b: rpc_err(-32601)
    w.tt_rest = lambda r: json_response({"days": [], "errors": [{"code": "X"}]})
    c = run(w).categories[Category.CANCELLATIONS]
    assert c.status == CategoryStatus.INCOMPLETE and c.error.code == "incomplete"


def test_date_not_allowed_is_incomplete():
    w = World()
    w.get_timetable = lambda b: rpc_err(-7004)
    res = run(w)
    assert res.categories[Category.CANCELLATIONS].status == CategoryStatus.INCOMPLETE and not res.complete
    assert res.categories[Category.EXAMS].status == CategoryStatus.OK


# =============================================================================== Leere Listen und Fehlerarten


def test_honest_empty_lists_are_ok_with_zero_items():
    w = World()
    w.homework = lambda r: json_response({"data": {"homeworks": [], "lessons": []}})
    w.exam = lambda r: json_response({"data": {"exams": []}})
    w.get_timetable = lambda b: rpc_ok([])
    res = run(w)
    assert res.items == [] and res.complete
    assert all(o.status == CategoryStatus.OK and o.item_count == 0 for o in res.categories.values())


def test_exams_403_is_partial_success():
    w = World()
    w.exam = lambda r: text_response("forbidden", 403)
    res = run(w)
    c = res.categories[Category.EXAMS]
    assert c.status == CategoryStatus.FAILED and c.error.code == "access_denied"
    assert "Prüfungen/Schularbeiten" in c.error.message and "fehlende Rechte" in c.error.message
    assert res.categories[Category.HOMEWORK].status == CategoryStatus.OK
    assert res.categories[Category.CANCELLATIONS].status == CategoryStatus.OK
    assert not res.complete
    assert {i.kind for i in res.items} == {Kind.HOMEWORK, Kind.CANCELLATION}  # die anderen liefern weiter


@pytest.mark.parametrize(
    "payload,path",
    [
        ({"data": {"homeworks": "kaputt"}}, "$.data.homeworks"),
        ({"data": {}}, "$.data.homeworks"),
        ({"data": [1]}, "$.data"),
        ({"foo": 1}, "$.data"),
        ({"data": {"homeworks": [{"text": "x", "dueDate": 20261016}]}}, "$.data.homeworks[0].id"),
        ({"data": {"homeworks": [{"id": "hunter2-dummy-secret", "dueDate": 20261016}]}}, "$.data.homeworks[0].id"),
        ({"data": {"homeworks": [{"id": 1, "dueDate": "2026-10-16"}]}}, "$.data.homeworks[0].dueDate"),
        ({"data": {"homeworks": [{"id": 1, "dueDate": 20261316}]}}, "$.data.homeworks[0].dueDate"),
        ({"data": {"homeworks": [{"id": 1, "dueDate": 20261016, "text": 5}]}}, "$.data.homeworks[0].text"),
    ],
)
def test_unexpected_homework_structure_is_schema_error_with_path(payload, path):
    w = World()
    w.homework = lambda r: json_response(payload)
    res = run(w)
    c = res.categories[Category.HOMEWORK]
    assert c.status == CategoryStatus.FAILED and c.error.code == "schema"
    assert path in c.error.detail
    assert PW not in repr(c) and "secret" not in repr(c)  # nur der Pfad, nie Datenwerte
    assert not res.complete and res.categories[Category.EXAMS].status == CategoryStatus.OK


@pytest.mark.parametrize(
    "exam,path",
    [
        ({"examDate": 20261021, "startTime": 800, "endTime": 900}, "$.data.exams[0].id"),
        ({"id": 1, "examDate": "20261021", "startTime": 800, "endTime": 900}, "$.data.exams[0].examDate"),  # Typfehler; fehlendes Datum = Notice no_date (siehe test_webuntis_review)
        ({"id": 1, "examDate": 20261021, "startTime": 2500, "endTime": 900}, "$.data.exams[0].startTime"),
        ({"id": 1, "examDate": 20261021, "startTime": 800, "endTime": 900, "teachers": "MUS"}, "$.data.exams[0].teachers"),
        ({"id": 1, "startDateTime": "gestern"}, "$.data.exams[0].startDateTime"),
    ],
)
def test_unexpected_exam_structure(exam, path):
    w = World()
    w.exam = lambda r: json_response({"data": {"exams": [exam]}})
    c = run(w).categories[Category.EXAMS]
    assert c.status == CategoryStatus.FAILED and c.error.code == "schema" and path in c.error.detail


@pytest.mark.parametrize(
    "result,path",
    [
        ({"not": "list"}, "$.result"),
        ([{"date": 20261014, "startTime": 800, "endTime": 850}], "$.result[0].id"),
        ([{"id": 1, "date": 20261014, "startTime": "8", "endTime": 850}], "$.result[0].startTime"),
        ([{"id": 1, "date": 20261014, "startTime": 800, "endTime": 850, "code": 7}], "$.result[0].code"),
        ([{"id": 1, "date": 20261014, "startTime": 800, "endTime": 850, "su": {"id": 1}}], "$.result[0].su"),
    ],
)
def test_unexpected_timetable_structure(result, path):
    w = World()
    w.get_timetable = lambda b: rpc_ok(result)
    c = run(w).categories[Category.CANCELLATIONS]
    assert c.status == CategoryStatus.FAILED and c.error.code == "schema" and path in c.error.detail


def test_html_login_page_instead_of_json_is_failure_never_empty():
    w = World()
    w.homework = lambda r: html_page()  # 200 mit Loginseite
    res = run(w)
    c = res.categories[Category.HOMEWORK]
    assert c.status == CategoryStatus.FAILED and c.error.code == "session_expired"
    assert c.item_count == 0 and not res.complete


def test_redirect_to_login_instead_of_json_is_failure():
    w = World()
    w.exam = lambda r: redirect("/WebUntis/index.do")
    c = run(w).categories[Category.EXAMS]
    assert c.status == CategoryStatus.FAILED and c.error.code == "session_expired"


def test_session_expiry_mid_fetch_stops_further_requests_and_reports_all_remaining():
    w = World()
    w.exam = lambda r: redirect("/WebUntis/index.do")
    res = run(w)
    assert res.categories[Category.HOMEWORK].status == CategoryStatus.OK
    for cat in (Category.EXAMS, Category.CANCELLATIONS):
        assert res.categories[cat].status == CategoryStatus.FAILED and res.categories[cat].error.code == "session_expired"
    assert "getTimetable" not in w.methods()  # keine weiteren Anfragen mit toter Sitzung
    assert not res.complete


def test_rpc_session_expiry_not_authenticated_code():
    w = World()
    w.get_timetable = lambda b: rpc_err(-8520)
    c = run(w).categories[Category.CANCELLATIONS]
    assert c.status == CategoryStatus.FAILED and c.error.code == "session_expired"


def test_new_login_after_expired_session_is_allowed_on_next_fetch():
    w = World()
    state = {"expire": True}
    original = w.exam

    def exam(r):
        return redirect("/WebUntis/index.do") if state["expire"] else original(r)

    w.exam = exam
    a = adapter(w)
    assert not a.fetch(fetch_window(NOW), [], NOW).complete
    state["expire"] = False
    assert a.fetch(fetch_window(NOW), [], NOW).complete
    assert w.methods().count("authenticate") == 2  # gültige Zugangsdaten: neue Sitzung, kein Ablehnungs-Wiederholen


def test_server_error_is_transient_and_propagates():
    w = World()
    w.exam = lambda r: text_response("", 503)
    with pytest.raises(TransientError):
        run(w)


def test_not_found_endpoint_is_failed_category():
    w = World()
    w.homework = lambda r: text_response("", 404)
    c = run(w).categories[Category.HOMEWORK]
    assert c.status == CategoryStatus.FAILED and c.error.code == "not_found"


# =============================================================================== Datumsbereiche, Fenster, tracked


def _next_day(ymd: int) -> int:
    d = date(ymd // 10000, ymd // 100 % 100, ymd % 100) + timedelta(days=1)
    return d.year * 10000 + d.month * 100 + d.day


def test_date_range_is_union_of_window_and_tracked_refs():
    w = World()
    tracked = [
        ref(Kind.HOMEWORK, "9003", date(2026, 11, 4)),
        ref(Kind.EXAM, "5503", datetime(2026, 11, 10, 10, 30, tzinfo=VIENNA)),
        ref(Kind.CANCELLATION, "701099-20261105-0800", datetime(2026, 11, 5, 8, 0, tzinfo=VIENNA), datetime(2026, 11, 5, 8, 50, tzinfo=VIENNA)),
    ]
    res = run(w, tracked)
    hw_calls = [r.params for r in w.http.calls(r"homeworks/lessons")]
    # Verfolgte Hausaufgaben/Prüfungen: lückenlos von (Fensterbeginn - 28 Tage Rückblick) bis Schuljahresende 31.08.2027,
    # damit eine verschobene Frist auffällt (28 Tage Rückblick: Filter nach Zuweisungs-/Fälligkeitsdatum unverified)
    assert hw_calls[0]["startDate"] == 20260916 and hw_calls[-1]["endDate"] == 20270831
    assert all(b["startDate"] == _next_day(a["endDate"]) for a, b in zip(hw_calls, hw_calls[1:]))  # keine Lücke
    ex_calls = [r.params for r in w.http.calls(r"/api/exams")]
    assert ex_calls[0]["startDate"] == 20261014 and ex_calls[-1]["endDate"] == 20270831
    assert all(b["startDate"] == _next_day(a["endDate"]) for a, b in zip(ex_calls, ex_calls[1:]))
    # Entfälle ändern ihre Kennung bei Verlegung (neuer Entfall): kein Schuljahreshorizont, nur Fenster + verfolgter Termin
    tt = [(r.json["params"]["options"]["startDate"], r.json["params"]["options"]["endDate"]) for r in w.http.calls(r"jsonrpc") if r.json["method"] == "getTimetable"]
    assert tt == [(20261014, 20261103), (20261105, 20261105)]
    assert set(by_kind(res, Kind.HOMEWORK)) == {"9001", "9002", "9003"}
    assert set(by_kind(res, Kind.EXAM)) == {"5501", "5502", "5503"}
    assert set(by_kind(res, Kind.CANCELLATION)) == {"701002-20261014-0855", "701005-20261016-1350", "701099-20261105-0800"}


def test_without_tracked_refs_outside_items_are_not_returned_even_if_server_sends_them():
    w = World()
    w.homework = lambda r: json_response({"data": fx("rest_homeworks.json")["data"]})  # Server liefert ALLES
    w.exam = lambda r: json_response(fx("rest_exams.json"))
    w.get_timetable = lambda b: rpc_ok(fx("jsonrpc_timetable.json")["result"])
    res = run(w)
    assert set(by_kind(res, Kind.HOMEWORK)) == {"9001", "9002"}
    assert set(by_kind(res, Kind.EXAM)) == {"5501", "5502"}
    assert set(by_kind(res, Kind.CANCELLATION)) == {"701002-20261014-0855", "701005-20261016-1350"}


def test_tracked_ref_of_other_source_or_kind_does_not_widen_other_categories():
    w = World()
    other = TrackedRef(Source.EDUVIDUAL, Kind.ASSIGNMENT, "77", date(2027, 1, 1), None)
    exam_only = ref(Kind.EXAM, "5503", datetime(2026, 11, 10, 10, 30, tzinfo=VIENNA))
    run(w, [other, exam_only])
    assert len(w.http.calls(r"homeworks/lessons")) == 1 and w.http.calls(r"homeworks/lessons")[0].params["endDate"] == 20261103
    assert w.http.calls(r"/api/exams")[-1].params["endDate"] == 20270831  # nur die Kategorie mit verfolgtem Eintrag wird verlängert
    tt = [r for r in w.http.calls(r"jsonrpc") if r.json["method"] == "getTimetable"]
    assert [r.json["params"]["options"]["endDate"] for r in tt] == [20261103]  # Entfälle: Fenster unverändert


def test_window_edges_homework_exam_lesson():
    w = World()
    hw = lambda i, d: {"id": i, "lessonId": 3301, "date": 20261001, "dueDate": d, "text": f"HA {i}", "remark": "", "completed": False, "attachments": []}
    w.homework = lambda r: json_response({"data": {"homeworks": [hw(1, 20261013), hw(2, 20261014), hw(3, 20261103), hw(4, 20261104)], "lessons": [{"id": 3301, "subject": "AM"}]}})
    ex = lambda i, d, s, e: {"id": i, "examType": "Test", "name": f"T{i}", "examDate": d, "startTime": s, "endTime": e, "subject": "AM", "teachers": [], "rooms": [], "text": ""}
    w.exam = lambda r: json_response({"data": {"exams": [ex(1, 20261013, 1500, 1550), ex(2, 20261014, 800, 850), ex(3, 20261103, 2300, 2350), ex(4, 20261104, 800, 850)]}})
    w.get_timetable = lambda b: rpc_ok([
        lesson(1, 20261013, 1500, 1550, "cancelled"), lesson(2, 20261014, 800, 850, "cancelled"),
        lesson(3, 20261103, 1350, 1440, "cancelled"), lesson(4, 20261104, 800, 850, "cancelled"),
    ])
    res = run(w)
    assert set(by_kind(res, Kind.HOMEWORK)) == {"2", "3"}
    assert set(by_kind(res, Kind.EXAM)) == {"2", "3"}
    assert set(by_kind(res, Kind.CANCELLATION)) == {"2-20261014-0800", "3-20261103-1350"}


def test_long_range_is_split_into_chunks_without_duplicates():
    assert wu.chunk_range(date(2026, 1, 1), date(2026, 1, 1)) == [(date(2026, 1, 1), date(2026, 1, 1))]
    chunks = wu.chunk_range(date(2026, 1, 1), date(2026, 5, 1))
    assert chunks[0][0] == date(2026, 1, 1) and chunks[-1][1] == date(2026, 5, 1)
    assert all((b - a).days + 1 <= wu.MAX_SPAN_DAYS for a, b in chunks)
    assert all(chunks[i][1] + timedelta(days=1) == chunks[i + 1][0] for i in range(len(chunks) - 1))
    w = World()
    big = Window(local_midnight(date(2026, 10, 14)), local_midnight(date(2027, 1, 22)))  # 100 Tage
    res = run(w, window=big)
    calls = [(r.params["startDate"], r.params["endDate"]) for r in w.http.calls(r"/api/exams")]
    assert calls == [(20261014, 20261214), (20261215, 20270121)]
    assert {i.source_id for i in res.items if i.kind == Kind.EXAM} == {"5501", "5502", "5503"}
    assert len(res.items) == len(set(i.uid for i in res.items))


def test_merge_ranges():
    d = date
    assert wu.merge_ranges([(d(2026, 1, 5), d(2026, 1, 9)), (d(2026, 1, 1), d(2026, 1, 4))]) == [(d(2026, 1, 1), d(2026, 1, 9))]
    assert wu.merge_ranges([(d(2026, 1, 1), d(2026, 1, 3)), (d(2026, 1, 5), d(2026, 1, 6))]) == [(d(2026, 1, 1), d(2026, 1, 3)), (d(2026, 1, 5), d(2026, 1, 6))]
    assert wu.merge_ranges([(d(2026, 1, 1), d(2026, 1, 10)), (d(2026, 1, 3), d(2026, 1, 4))]) == [(d(2026, 1, 1), d(2026, 1, 10))]


def test_rpc_range_is_clipped_to_school_years_and_holidays_give_honest_empty():
    w = World()
    # Fenster im Sommer (zwischen den Schuljahren 2025/26 bis 05.07. und 2026/27 ab 07.09.)
    now = datetime(2026, 7, 20, 8, 0, tzinfo=VIENNA)
    res = run(w, now=now)
    assert "getTimetable" not in w.methods()
    c = res.categories[Category.CANCELLATIONS]
    assert c.status == CategoryStatus.OK and c.item_count == 0 and "kein Zeitraum" in c.detail
    # Fenster, das ins neue Schuljahr hineinreicht: nur der Teil ab 07.09. wird angefragt
    w2 = World()
    now2 = datetime(2026, 8, 30, 8, 0, tzinfo=VIENNA)
    run(w2, now=now2)
    opts = [r.json["params"]["options"] for r in w2.http.calls(r"jsonrpc") if r.json["method"] == "getTimetable"]
    assert [(o["startDate"], o["endDate"]) for o in opts] == [(20260907, 20260919)]


def test_school_years_unavailable_means_no_clipping():
    w = World()
    w.school_years = lambda b: rpc_err(-32601)
    now = datetime(2026, 7, 20, 8, 0, tzinfo=VIENNA)
    run(w, now=now)
    assert "getTimetable" in w.methods()


# =============================================================================== Rücknahme (withdrawn)


KEY = "701002-20261014-0855"
UID = make_uid(Source.WEBUNTIS, Kind.CANCELLATION, KEY)


def tracked_cancellation(cancelled=False, key=KEY):
    return ref(Kind.CANCELLATION, key, datetime(2026, 10, 14, 8, 55, tzinfo=VIENNA), datetime(2026, 10, 14, 9, 45, tzinfo=VIENNA), cancelled)


def test_withdrawn_when_tracked_lesson_is_explicitly_regular_again_rpc():
    w = World()
    w.lessons = [lesson(701002, 20261014, 855, 945)]  # gleiche Stunde, kein code mehr
    res = run(w, [tracked_cancellation()])
    assert UID in res.withdrawn
    assert "nicht mehr als Entfall" in res.withdrawn[UID] and "14.10.2026" in res.withdrawn[UID] and "08:55" in res.withdrawn[UID]
    assert UID not in {i.uid for i in res.items} and res.complete


def test_withdrawn_when_lesson_became_substitution():
    w = World()
    w.lessons = [lesson(701002, 20261014, 855, 945, "irregular")]
    assert UID in run(w, [tracked_cancellation()]).withdrawn


def test_withdrawn_rest_route_explicit_regular_status():
    w = World()
    w.get_timetable = lambda b: rpc_err(-32601)
    w.rest_tt = fx("rest_timetable_regular_again.json")
    res = run(w, [tracked_cancellation()])
    assert UID in res.withdrawn and "regulär" in res.withdrawn[UID]
    assert not by_kind(res, Kind.CANCELLATION)


def test_no_withdrawal_on_mere_absence():
    w = World()
    w.lessons = [lesson(1, 20261014, 800, 850)]  # 701002 fehlt in der Liste
    res = run(w, [tracked_cancellation()])
    assert res.withdrawn == {} and res.complete


def test_no_withdrawal_on_empty_list():
    w = World()
    w.get_timetable = lambda b: rpc_ok([])
    assert run(w, [tracked_cancellation()]).withdrawn == {}


def test_no_withdrawal_when_timetable_fetch_failed():
    w = World()
    w.get_timetable = lambda b: rpc_err(-7004)
    res = run(w, [tracked_cancellation()])
    assert res.withdrawn == {} and not res.complete


def test_no_withdrawal_on_unknown_status():
    w = World()
    w.lessons = [lesson(701002, 20261014, 855, 945, "neuercode")]
    assert run(w, [tracked_cancellation()]).withdrawn == {}


def test_no_withdrawal_when_time_changed_key_differs():
    w = World()
    w.lessons = [lesson(701002, 20261014, 900, 950)]  # andere Startzeit = andere Kennung: kein Beleg
    assert run(w, [tracked_cancellation()]).withdrawn == {}


def test_no_withdrawal_twice_and_reactivation_returns_item():
    w = World()
    w.lessons = [lesson(701002, 20261014, 855, 945)]
    assert run(w, [tracked_cancellation(cancelled=True)]).withdrawn == {}  # schon zurückgenommen
    w.lessons = [lesson(701002, 20261014, 855, 945, "cancelled")]
    res = run(w, [tracked_cancellation(cancelled=True)])
    assert res.withdrawn == {} and KEY in by_kind(res, Kind.CANCELLATION)  # erneut ausgefallen -> Eintrag kommt wieder


def test_homework_and_exams_never_produce_withdrawn():
    w = World()
    w.homework = lambda r: json_response({"data": {"homeworks": [], "lessons": []}})  # Hausaufgabe verschwindet
    w.exam = lambda r: json_response({"data": {"exams": []}})
    tracked = [ref(Kind.HOMEWORK, "9001", date(2026, 10, 16)), ref(Kind.EXAM, "5501", datetime(2026, 10, 21, 8, 0, tzinfo=VIENNA))]
    res = run(w, tracked)
    assert res.withdrawn == {} and res.complete  # Abwesenheit ist nie eine Absage


# =============================================================================== Zeitumstellung, ID-Stabilität


def test_dst_spring_and_autumn_offsets():
    w = World()
    now = datetime(2026, 3, 27, 8, 0, tzinfo=VIENNA)
    w.lessons = [lesson(1, 20260328, 800, 850, "cancelled"), lesson(2, 20260329, 800, 850, "cancelled")]
    w.exams = {"data": {"exams": [{"id": 9, "examType": "Test", "name": "T", "examDate": 20260329, "startTime": 900, "endTime": 950, "subject": "AM", "teachers": [], "rooms": [], "text": ""}]}}
    res = run(w, now=now)
    ca = by_kind(res, Kind.CANCELLATION)
    assert ca["1-20260328-0800"].when.start.isoformat() == "2026-03-28T08:00:00+01:00"
    assert ca["2-20260329-0800"].when.start.isoformat() == "2026-03-29T08:00:00+02:00"  # Sommerzeit begonnen
    assert by_kind(res, Kind.EXAM)["9"].when.start.isoformat() == "2026-03-29T09:00:00+02:00"
    assert ca["2-20260329-0800"].when.end.isoformat() == "2026-03-29T08:50:00+02:00"

    w2 = World()
    now2 = datetime(2026, 10, 24, 8, 0, tzinfo=VIENNA)
    w2.lessons = [lesson(1, 20261024, 800, 850, "cancelled"), lesson(2, 20261025, 800, 850, "cancelled")]
    ca2 = by_kind(run(w2, now=now2), Kind.CANCELLATION)
    assert ca2["1-20261024-0800"].when.start.isoformat() == "2026-10-24T08:00:00+02:00"
    assert ca2["2-20261025-0800"].when.start.isoformat() == "2026-10-25T08:00:00+01:00"  # Winterzeit


def test_dst_window_over_transition_and_homework_dates_unaffected():
    w = World()
    now = datetime(2026, 10, 24, 8, 0, tzinfo=VIENNA)
    w.homeworks = {"data": {"homeworks": [{"id": 5, "lessonId": 3301, "date": 20261020, "dueDate": 20261025, "text": "Lesen", "remark": "", "completed": False, "attachments": []}],
                            "lessons": [{"id": 3301, "subject": "D"}]}}
    hw = by_kind(run(w, now=now), Kind.HOMEWORK)["5"]
    assert hw.when == ItemTime(date(2026, 10, 25)) and hw.when.end is None


def test_ids_are_stable_when_dates_shift_for_homework_and_exam():
    w1, w2 = World(), World()
    w2.homeworks = copy.deepcopy(w2.homeworks)
    for h in w2.homeworks["data"]["homeworks"]:
        if h["id"] == 9001:
            h["dueDate"] = 20261023
    w2.exams = copy.deepcopy(w2.exams)
    for e in w2.exams["data"]["exams"]:
        if e["id"] == 5501:
            e["examDate"] = 20261028
    r1, r2 = run(w1), run(w2)
    a, b = by_kind(r1, Kind.HOMEWORK)["9001"], by_kind(r2, Kind.HOMEWORK)["9001"]
    assert a.uid == b.uid and a.when != b.when and a.content_hash() != b.content_hash()
    c, d = by_kind(r1, Kind.EXAM)["5501"], by_kind(r2, Kind.EXAM)["5501"]
    assert c.uid == d.uid and c.when != d.when


def test_cancellation_key_composition_documented_shift_creates_new_id():
    w1, w2 = World(), World()
    w2.lessons = [lesson(701002, 20261015, 855, 945, "cancelled")]  # dieselbe Stunden-ID, anderer Tag
    k1 = set(by_kind(run(w1), Kind.CANCELLATION))
    k2 = set(by_kind(run(w2), Kind.CANCELLATION))
    assert "701002-20261014-0855" in k1 and k2 == {"701002-20261015-0855"}


def test_items_roundtrip_through_model_dicts():
    for item in run(World()).items:
        assert SourceItem.from_dict(item.to_dict()) == item


# =============================================================================== Titel- und Textregeln


def test_homework_title_rule_unit():
    assert wu.homework_title("", "AM") == ("Hausaufgabe AM", False, False)
    assert wu.homework_title("", None) == ("Hausaufgabe", False, False)
    assert wu.homework_title("Kurz\nzweite Zeile", "AM") == ("Kurz", False, True)
    long_text = " ".join(f"Wort{i}" for i in range(60))
    title, clipped, has = wu.homework_title(long_text, "AM")
    assert clipped and has and title.endswith("…") and len(title) <= wu.TITLE_MAX + 1
    assert long_text.startswith(title[:-1].rstrip()) and not title[:-1].endswith(" ")
    exact = "x" * wu.TITLE_MAX
    assert wu.homework_title(exact, None) == (exact, False, True)


def test_clipped_homework_title_marks_notes_and_notice_and_keeps_full_text():
    long_text = "Lies das Kapitel über Netzwerkprotokolle und beantworte " + " ".join(f"Frage{i}" for i in range(40)) + "\nZweite Zeile"
    w = World()
    w.homework = lambda r: json_response({"data": {"homeworks": [{"id": 7, "lessonId": 3301, "date": 20261012, "dueDate": 20261016, "text": long_text, "remark": "", "completed": False, "attachments": []}], "lessons": [{"id": 3301, "subject": "NWP"}]}})
    res = run(w)
    item = by_kind(res, Kind.HOMEWORK)["7"]
    assert item.title.endswith("…") and any("Titel gekürzt" in n for n in item.notes)
    assert item.description == long_text  # alles bleibt erhalten
    n = [x for x in res.notices if x.code == "incomplete_data"]
    assert len(n) == 1 and n[0].uid == "webuntis:homework:7" and n[0].severity == Severity.INFO


def test_homework_without_text_gets_title_from_subject_and_warning():
    w = World()
    w.homework = lambda r: json_response({"data": {"homeworks": [{"id": 8, "lessonId": 3301, "date": 20261012, "dueDate": 20261016, "text": "  ", "remark": "", "completed": False, "attachments": []}], "lessons": [{"id": 3301, "subject": "NWP"}]}})
    res = run(w)
    item = by_kind(res, Kind.HOMEWORK)["8"]
    assert item.title == "Hausaufgabe NWP" and item.description is None
    assert any("Kein Hausaufgabentext" in n for n in item.notes)
    assert [n.severity for n in res.notices if n.code == "incomplete_data"] == [Severity.WARNING]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("", ""),
        ("  a \t b  ", "a b"),
        ("a\r\n\r\n\r\nb", "a\n\nb"),
        ("x y", "x y"),
        ("<p>Eins</p><p>Zwei</p>", "Eins\n\nZwei"),
        ("Zeile<br>zwei<br/>drei", "Zeile\nzwei\ndrei"),
        ("<ul><li>a</li><li>b</li></ul>", "a\nb"),
        ("A &amp; B &lt; C", "A &amp; B &lt; C"),  # kein HTML-Tag -> unverändert (nichts deuten)
        ("<b>A &amp; B</b>", "A & B"),
        ("Text<script>alert(1)</script> Ende<style>p{}</style>", "Text Ende"),
        ("2 < 3 und 5 > 4", "2 < 3 und 5 > 4"),
    ],
)
def test_clean_text(raw, expected):
    assert wu.clean_text(raw) == expected


def test_time_parsing_units():
    assert wu.parse_hmm(800, "p").hour == 8 and wu.parse_hmm(1350, "p").minute == 50 and wu.parse_hmm(0, "p").hour == 0
    for bad in (2400, 960, -5, "800", True, None):
        with pytest.raises(SchemaError):
            wu.parse_hmm(bad, "p")
    assert wu.parse_ymd(20261014, "p") == date(2026, 10, 14)
    for bad in (20261314, 2026, "20261014", None, 20260230):
        with pytest.raises(SchemaError):
            wu.parse_ymd(bad, "p")


def test_require_helper_reports_path_without_values():
    with pytest.raises(SchemaError) as e:
        wu.require({"a": {"b": "geheim-wert"}}, "x", dict, "$.a")
    assert e.value.detail == "JSON-Pfad: $.a.x" and "geheim" not in str(e.value) + (e.value.detail or "")
    with pytest.raises(SchemaError) as e2:
        wu.require({"a": "geheim-wert"}, "a", int, "$")
    assert "geheim" not in (e2.value.detail or "") + e2.value.message


# =============================================================================== Konfiguration, Lesezugriff, Geheimnisse, check()


def test_disabled_categories_are_not_fetched():
    w = World()
    cfg = WebUntisConfig(username="beispiel.max", categories=("homework",))
    res = run(w, cfg=cfg)
    assert res.categories[Category.EXAMS].status == CategoryStatus.DISABLED
    assert res.categories[Category.CANCELLATIONS].status == CategoryStatus.DISABLED
    assert res.complete and {i.kind for i in res.items} == {Kind.HOMEWORK}
    assert w.http.calls(r"/api/exams") == [] and "getTimetable" not in w.methods() and "getSchoolyears" not in w.methods()
    cfg2 = WebUntisConfig(username="beispiel.max", categories=("exams", "cancellations"))
    w2 = World()
    res2 = run(w2, cfg=cfg2)
    assert res2.categories[Category.HOMEWORK].status == CategoryStatus.DISABLED and w2.http.calls(r"homeworks") == []


def test_disabled_category_does_not_hide_failure_of_enabled_one():
    w = World()
    w.exam = lambda r: text_response("", 403)
    cfg = WebUntisConfig(username="beispiel.max", categories=("exams",))
    res = run(w, cfg=cfg)
    assert not res.complete and res.categories[Category.EXAMS].status == CategoryStatus.FAILED


READ_ONLY_RPC = {"authenticate", "getSchoolyears", "getTimetable"}


def assert_read_only(http: FakeHttp) -> None:
    for r in http.requests:
        if r.method == "GET":
            continue
        assert r.method == "POST", r
        if "jsonrpc.do" in r.url:
            assert r.json["method"] in READ_ONLY_RPC
        else:
            assert "j_spring_security_check" in r.url  # einziger weiterer POST: das Login-Formular


def test_read_only_guarantee_rpc_route_and_form_route():
    w = World()
    run(w, tracked=[ref(Kind.EXAM, "5503", datetime(2026, 11, 10, 10, 30, tzinfo=VIENNA))])
    adapter(w).check()
    assert_read_only(w.http)
    assert any(r.method == "POST" for r in w.http.requests)
    w2 = World()
    w2.raw_rpc = lambda r: html_page()
    run(w2)
    assert_read_only(w2.http)
    # keine Schreib-Endpunkte der Homework-API (z. B. Erledigt-Häkchen) werden je angesprochen
    for r in w.http.requests + w2.http.requests:
        assert not any(x in r.url for x in ("complete", "done", "submit", "delete", "update"))


def all_text(result) -> str:
    return repr(result) + "".join(f"{n}" for n in getattr(result, "notices", []))


@pytest.mark.parametrize("scenario", ["ok", "rest", "badlogin", "403", "schema", "expired", "html"])
def test_secrets_never_leak_into_results_errors_or_requests(scenario):
    w = World()
    if scenario == "rest":
        w.get_timetable = lambda b: rpc_err(-32601)
    elif scenario == "badlogin":
        w.authenticate = lambda body: rpc_err(-8504, f"falsch {PW}")
    elif scenario == "403":
        w.exam = lambda r: text_response(f"nope {BEARER}", 403)
    elif scenario == "schema":
        w.homework = lambda r: json_response({"data": {"homeworks": [{"id": BEARER}]}})
    elif scenario == "expired":
        w.exam = lambda r: redirect(f"/WebUntis/index.do?token={BEARER}&sid={SESSION_ID}")
    elif scenario == "html":
        w.raw_rpc = lambda r: html_page()
    a = adapter(w)
    res = a.fetch(fetch_window(NOW), [], NOW)
    probe = a.check()
    text = all_text(res) + repr(probe)
    for secret in (PW, BEARER, SESSION_ID):
        assert secret not in text, scenario
    for r in w.http.requests:
        for secret in (PW, BEARER, SESSION_ID):
            assert secret not in r.url, (scenario, r.url)
            assert secret not in json.dumps(dict(r.params or {})), scenario
        hdr = json.dumps(dict(r.headers or {}))
        assert PW not in hdr and SESSION_ID not in hdr
        # Das Passwort steht nur im Body der Anmeldung (JSON-RPC authenticate bzw. Login-Formular), nirgends sonst.
        is_login = (isinstance(r.json, dict) and r.json.get("method") == "authenticate") or (
            isinstance(r.data, dict) and "j_password" in r.data
        )
        if not is_login:
            assert PW not in json.dumps(r.json) + json.dumps(r.data), (scenario, r.url)
        if BEARER in hdr:
            assert r.method == "GET" and "/api/rest/view/" in r.url  # Bearer nur im Header der REST-Aufrufe


def test_secret_in_transient_error_message():
    w = World()
    w.exam = lambda r: text_response(f"{PW}", 502)
    with pytest.raises(TransientError) as e:
        run(w)
    assert PW not in str(e.value) + (e.value.detail or "") + (e.value.hint or "")


def test_check_report_steps_routes_and_counts():
    w = World()
    probe = adapter(w).check()
    names = [s.name for s in probe.steps]
    assert names == ["Erreichbarkeit", "Anmeldung", "Schülerkennung", "Hausaufgaben", "Prüfungen/Schularbeiten", "Unterrichtsentfälle"]
    assert probe.ok and all(s.ok for s in probe.steps)
    login = probe.steps[1].detail
    assert "JSON-RPC authenticate" in login and "Hinweis" not in login
    # Prüfzeitraum 7 Tage ab heute (14.10. bis 20.10.): 2 Hausaufgaben, 0 Prüfungen, 2 Entfälle
    assert "REST /api/homeworks/lessons" in probe.steps[3].detail and "2 Einträge" in probe.steps[3].detail
    assert "0 Einträge" in probe.steps[4].detail and "REST /api/exams" in probe.steps[4].detail
    assert "JSON-RPC getTimetable" in probe.steps[5].detail and "2 Entfall-Einträge" in probe.steps[5].detail
    assert set(probe.categories) == {Category.HOMEWORK, Category.EXAMS, Category.CANCELLATIONS}
    assert_read_only(w.http)


def test_check_reports_wrong_credentials_and_partial_failure():
    w = World()
    w.authenticate = lambda body: rpc_err(-8504)
    probe = adapter(w).check()
    assert not probe.ok and [s.name for s in probe.steps][-1] == "Anmeldung" and not probe.steps[-1].ok
    assert "Benutzername oder Passwort" in probe.steps[-1].detail
    w2 = World()
    w2.exam = lambda r: text_response("", 403)
    p2 = adapter(w2).check()
    assert not p2.ok
    step = next(s for s in p2.steps if s.name.startswith("Prüfungen"))
    assert not step.ok and "Kein Zugriff" in step.detail
    assert next(s for s in p2.steps if s.name == "Hausaufgaben").ok


def test_check_marks_disabled_categories_and_cookie_mismatch_hint():
    w = World()
    w.http.cookies["JSESSIONID"] = "ANDERE-KENNUNG"
    cfg = WebUntisConfig(username="beispiel.max", categories=("homework",))
    probe = adapter(w, cfg).check()
    assert "Hinweis" in probe.steps[1].detail and "ANDERE-KENNUNG" not in repr(probe) and SESSION_ID not in repr(probe)
    assert next(s for s in probe.steps if s.name.startswith("Prüfungen")).detail == "Per Konfiguration abgeschaltet."
    assert probe.ok


def test_check_uses_session_expiry_as_failed_category_not_exception():
    w = World()
    w.homework = lambda r: html_page()
    probe = adapter(w).check()
    assert not probe.ok
    assert next(s for s in probe.steps if s.name == "Hausaufgaben").ok is False


def test_adapter_uses_default_requests_http_when_no_factory(monkeypatch):
    created = []

    class Dummy:
        def __init__(self):
            created.append(self)

    monkeypatch.setattr(wu, "RequestsHttp", Dummy)
    a = WebUntisAdapter(CFG, PW)
    assert a._http is created[0] and a.source == Source.WEBUNTIS
