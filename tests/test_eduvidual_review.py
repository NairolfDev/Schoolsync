"""Adversarial review tests for the Eduvidual adapter. Each test demonstrates one real defect (fails today)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from schulsync.adapters import eduvidual as ev
from schulsync.adapters.eduvidual import EduvidualAdapter, html_to_text, safe_http_url
from schulsync.config import EduvidualConfig
from schulsync.models import Category, CategoryStatus
from schulsync.timeutil import VIENNA
from tests.support.fake_http import FakeHttp, json_response, text_response
from tests.test_eduvidual import (
    CAT,
    EXPECTED_IN_WINDOW,
    PASSWORD,
    add_public_config,
    add_web,
    add_ws,
    ids,
    jf,
    make,
    run,
    tf,
    ws_adapter,
)


def test_numeric_course_ids_in_config_do_not_crash_the_fetch(now):
    # config.example.toml: "courses = [] ... sonst Kurs-IDs oder exakte Kursnamen"; config.py does not validate
    # element types, so `courses = [7002]` (TOML integers, the natural spelling of an ID) reaches the adapter.
    http = FakeHttp()
    add_ws(http)
    adapter = EduvidualAdapter(EduvidualConfig(username="u", method="webservice", courses=(7002,)), PASSWORD, http_factory=lambda: http)
    res = run(adapter, now)  # today: AttributeError: 'int' object has no attribute 'strip'
    assert ids(res) == ["assign:4108", "assign:4103", "assign:4106"]


def test_login_page_from_token_endpoint_is_not_a_credential_rejection(now):
    # A login page answering login/token.php says nothing about the password (it was never evaluated). The
    # adapter already treats "REST answers with a login page after a token was issued" as route unavailable
    # because a login_error would block automatic runs (DESIGN 7.4 auth_blocked). Same reasoning applies here.
    http = FakeHttp()
    add_ws(http, token=text_response(tf("login_page.html")))
    add_web(http)
    res = run(make(http, method="auto"), now)
    assert res.login_error is None
    assert res.complete and ids(res) == EXPECTED_IN_WINDOW  # fell back to the web route


def test_list_items_with_paragraphs_keep_bullet_and_text_on_one_line():
    # Pasted/WYSIWYG content very often has <li><p>..</p></li>
    assert html_to_text("<ul><li><p>Aufgabe eins</p></li><li><p>zwei</p></li></ul>") == "- Aufgabe eins\n- zwei"


def test_preformatted_line_breaks_are_kept():
    # ANFORDERUNGEN 5: Beschreibung unverfälscht; code in <pre> loses every line break today.
    assert html_to_text("<pre>def f():\n    return 1\nprint(f())</pre>").count("\n") >= 2


@pytest.fixture
def frozen(monkeypatch, now):
    monkeypatch.setattr(ev, "now_vienna", lambda: now)
    return now


def test_check_does_not_crash_on_unrepresentable_due_date(frozen):
    data = jf("assignments.json")
    data["courses"][0]["assignments"][0]["duedate"] = 10**15  # fetch() guards this (notice); check() does not
    http = FakeHttp()
    add_public_config(http)
    add_ws(http, assignments=json_response(data))
    report = make(http, method="webservice").check()  # today: ValueError / OverflowError escapes
    assert report.source is not None


@pytest.mark.parametrize("param", ["access_token", "api_key", "id_token"])
def test_safe_http_url_rejects_common_secret_parameter_names(param):
    assert safe_http_url(f"https://example.org/x?{param}=abc123") is None


def test_missing_username_is_a_config_problem_not_a_schema_change(now):
    # An empty username (setup not done) sends a pointless request with credentials and is then reported as
    # "Schnittstelle hat sich vermutlich geändert" (missingparam -> SchemaError). It should be a config error
    # and must not contact the server.
    http = FakeHttp()
    add_ws(http, token=json_response({"error": "Notwendiger Parameter \"username\" fehlt", "errorcode": "missingparam"}))
    res = run(make(http, method="webservice", username=""), now)
    assert not http.requests
    assert res.categories[CAT].error.code == "config"
