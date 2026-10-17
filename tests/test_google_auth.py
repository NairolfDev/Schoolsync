"""Tests for google_auth (OAuth helpers, token load/refresh, setup helpers).

No network, no real credentials: the token endpoint and the APIs are `HttpMockSequence` mocks, the interactive
browser flow is replaced by a stub of `InstalledAppFlow.run_local_server` (so the real loopback server, the
browser round trip and the code exchange with Google are NOT exercised here - only our parameters and error
handling). All JSON answers are SYNTHETIC.
"""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timedelta, timezone

import httplib2
import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence
from oauthlib.oauth2.rfc6749 import errors as oauth_errors

from schulsync import google_auth as ga
from schulsync.errors import (
    ConfigError,
    GoogleAuthError,
    NetworkError,
    OutcomeUnknownError,
    TargetUnavailableError,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        raise AssertionError(f"a test tried to resolve {host}")

    def forbidden(self, *args, **kwargs):
        raise AssertionError("a test tried to open a real socket")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    return sleeps


class Seq(HttpMockSequence):
    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        if self._iterable and isinstance(self._iterable[0], BaseException):
            self.request_sequence.append((uri, method, body, headers))
            raise self._iterable.pop(0)
        return super().request(uri, method, body=body, headers=headers, **kwargs)


def ok(body=None, status=200):
    return ({"status": str(status), "content-type": "application/json"}, json.dumps(body if body is not None else {}))


class DictSecrets:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.writes: list[str] = []

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value):
        self.writes.append(name)
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)


def naive_utc(delta: timedelta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + delta


def make_creds(*, expired=False, **kw) -> Credentials:
    args = dict(
        token="dummy-access-token",
        refresh_token="dummy-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="dummy-client-id.apps.googleusercontent.com",
        client_secret="dummy-client-secret",
        scopes=list(ga.SCOPES),
        expiry=naive_utc(timedelta(hours=-1) if expired else timedelta(hours=1)),
    )
    args.update(kw)
    return Credentials(**args)


def visible(exc) -> str:
    return " ".join(filter(None, [exc.message, exc.hint, exc.detail]))


CLIENT_JSON = json.dumps(
    {
        "installed": {
            "client_id": "dummy-client-id.apps.googleusercontent.com",
            "client_secret": "dummy-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
)


# ---------------------------------------------------------------------------------------------- scopes & JSON


def test_scopes_are_the_minimal_set():
    assert ga.SCOPES == (
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/tasks",
    )
    assert (ga.SECRET_CLIENT, ga.SECRET_TOKEN) == ("google_client", "google_token")


def _api_calls_of_the_program() -> set[tuple[str, str, str]]:
    """Every (api, resource, method) the source code calls, read from the code itself: `.events().insert(` and so on."""
    import re
    from pathlib import Path

    import schulsync

    api_of = {"events": "calendar", "calendarList": "calendar", "calendars": "calendar", "tasks": "tasks", "tasklists": "tasks"}
    pattern = re.compile(r"\.(events|calendarList|calendars|tasks|tasklists)\(\)\s*\.(\w+)\(")
    found: set[tuple[str, str, str]] = set()
    for path in Path(schulsync.__file__).parent.rglob("*.py"):
        for resource, method in pattern.findall(path.read_text(encoding="utf-8")):
            found.add((api_of[resource], resource, method))
    return found


def test_the_scopes_cover_every_api_call_the_program_makes_and_no_scope_is_surplus():
    """ANFORDERUNGEN 4: 'Verwende nur die erforderlichen Berechtigungen'. Pinning the constant alone proves nothing about the
    CALLS: the discovery documents of the Google client library list which scopes each method accepts. Every call of the
    program must be allowed by at least one of our scopes, and every scope must be needed by at least one call."""
    import googleapiclient

    from pathlib import Path

    docs = Path(googleapiclient.__file__).parent / "discovery_cache" / "documents"
    calls = _api_calls_of_the_program()
    # not vacuous: the regex finds the calls the gateway and the setup helpers are known to make
    assert {("calendar", "events", "insert"), ("calendar", "events", "patch"), ("calendar", "calendarList", "list"),
            ("tasks", "tasks", "insert"), ("tasks", "tasks", "patch"), ("tasks", "tasks", "delete"),
            ("tasks", "tasklists", "list"), ("tasks", "tasklists", "insert")} <= calls
    used: set[str] = set()
    for api, resource, method in sorted(calls):
        doc = json.loads((docs / f"{api}.{'v3' if api == 'calendar' else 'v1'}.json").read_text(encoding="utf-8"))
        allowed = set(doc["resources"][resource]["methods"][method]["scopes"])
        granted = allowed & set(ga.SCOPES)
        assert granted, f"{api}.{resource}.{method} verlangt eines von {sorted(allowed)}, SCOPES enthaelt keines davon"
        used |= granted
    assert used == set(ga.SCOPES), f"nicht benoetigte Berechtigung: {sorted(set(ga.SCOPES) - used)}"
    # nothing writes calendars or calendar list entries: those calls would need broader scopes than we ask for
    assert not [c for c in calls if c[1] in ("calendars", "calendarList") and c[2] not in ("get", "list")]


def test_credentials_json_round_trip():
    c = make_creds()
    data = json.loads(ga.credentials_to_json(c))
    assert set(data) == {"token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "expiry"}
    back = ga.credentials_from_json(ga.credentials_to_json(c))
    assert back.token == c.token and back.refresh_token == c.refresh_token
    assert back.client_id == c.client_id and back.client_secret == c.client_secret
    assert back.token_uri == c.token_uri and list(back.scopes) == list(ga.SCOPES)
    assert back.expiry == c.expiry and back.valid


def test_credentials_json_without_expiry_is_treated_as_expired():
    back = ga.credentials_from_json(ga.credentials_to_json(make_creds(expiry=None)))
    assert back.expiry is None and back.refresh_token == "dummy-refresh-token"
    assert back.valid  # google-auth: a token without expiry never expires; load_credentials only refreshes if invalid


@pytest.mark.parametrize("text", ["", "not json", "[]", json.dumps({"token": "dummy-access-token"}), "null"])
def test_credentials_from_json_garbage_is_google_auth_error(text):
    with pytest.raises(GoogleAuthError) as ei:
        ga.credentials_from_json(text)
    assert "setup" in ei.value.hint
    assert "dummy-access-token" not in visible(ei.value)


def test_save_credentials_uses_secret_name():
    s = DictSecrets()
    ga.save_credentials(make_creds(), s)
    assert list(s.data) == ["google_token"]
    assert json.loads(s.data["google_token"])["refresh_token"] == "dummy-refresh-token"


# ---------------------------------------------------------------------------------------------- load_credentials


def test_load_missing_secret_has_setup_hint():
    with pytest.raises(GoogleAuthError) as ei:
        ga.load_credentials(DictSecrets())
    assert "schulsync setup" in ei.value.hint


def test_load_valid_token_makes_no_request_and_no_write():
    s = DictSecrets({"google_token": ga.credentials_to_json(make_creds())})
    http = Seq([])
    c = ga.load_credentials(s, http=http)
    assert c.token == "dummy-access-token" and http.request_sequence == [] and s.writes == []


def test_load_refreshes_expired_token_and_persists_it():
    s = DictSecrets({"google_token": ga.credentials_to_json(make_creds(expired=True))})
    http = Seq([ok({"access_token": "dummy-new-token", "expires_in": 3600})])
    c = ga.load_credentials(s, http=http)
    assert c.token == "dummy-new-token" and c.valid
    assert len(http.request_sequence) == 1 and http.request_sequence[0][1] == "POST"
    stored = json.loads(s.data["google_token"])
    assert stored["token"] == "dummy-new-token" and stored["refresh_token"] == "dummy-refresh-token"
    assert datetime.fromisoformat(stored["expiry"].rstrip("Z")) > naive_utc(timedelta(minutes=30))
    # a second load now needs no refresh
    http2 = Seq([])
    assert ga.load_credentials(s, http=http2).token == "dummy-new-token" and http2.request_sequence == []


def test_load_persists_a_rotated_refresh_token():
    s = DictSecrets({"google_token": ga.credentials_to_json(make_creds(expired=True))})
    http = Seq([ok({"access_token": "dummy-new-token", "expires_in": 3600, "refresh_token": "dummy-rotated"})])
    ga.load_credentials(s, http=http)
    assert json.loads(s.data["google_token"])["refresh_token"] == "dummy-rotated"


def test_load_token_without_access_token_is_refreshed():
    s = DictSecrets({"google_token": ga.credentials_to_json(make_creds(token=None, expiry=None))})
    http = Seq([ok({"access_token": "dummy-new-token", "expires_in": 3600})])
    assert ga.load_credentials(s, http=http).token == "dummy-new-token"


@pytest.mark.parametrize(
    "answer",
    [
        ({"status": "400"}, json.dumps({"error": "invalid_grant", "error_description": "Token has been expired or revoked."})),
        ({"status": "400"}, json.dumps({"error": "invalid_grant", "error_description": "Bad Request"})),
    ],
)
def test_invalid_grant_maps_to_google_auth_error_with_honest_hint(answer):
    before = ga.credentials_to_json(make_creds(expired=True))
    s = DictSecrets({"google_token": before})
    with pytest.raises(GoogleAuthError) as ei:
        ga.load_credentials(s, http=Seq([answer]))
    e = ei.value
    assert "schulsync setup" in e.hint
    assert "7 Tage" in e.hint and "Testing" in e.hint and "6 Monate" in e.hint and "widerrufen" in e.hint
    assert "nicht garantiert" in e.hint  # no promise of unlimited validity
    assert "dummy-refresh-token" not in visible(e) and "dummy-client-secret" not in visible(e)
    assert s.data["google_token"] == before and s.writes == []  # a failed refresh never overwrites the token


def test_invalid_client_has_its_own_message():
    s = DictSecrets({"google_token": ga.credentials_to_json(make_creds(expired=True))})
    answer = ({"status": "401"}, json.dumps({"error": "invalid_client", "error_description": "Unauthorized"}))
    with pytest.raises(GoogleAuthError) as ei:
        ga.load_credentials(s, http=Seq([answer]))
    assert "OAuth-Client" in ei.value.message


@pytest.mark.parametrize(
    "answer",
    [
        ({"status": "503"}, "Service Unavailable"),  # non-JSON gateway page
        ({"status": "503"}, json.dumps({"error": "temporarily_unavailable"})),  # structured, retryable
        ({"status": "502"}, "<html>Bad Gateway</html>"),
    ],
)
def test_token_endpoint_outage_is_network_error_not_auth_error(no_sleep, answer):
    s = DictSecrets({"google_token": ga.credentials_to_json(make_creds(expired=True))})
    http = Seq([answer] * 5)
    with pytest.raises(NetworkError):
        ga.load_credentials(s, http=http)
    assert s.writes == []


@pytest.mark.parametrize("exc", [socket.timeout("timed out"), ConnectionResetError(), httplib2.ServerNotFoundError("dns")])
def test_transport_errors_during_refresh_are_network_errors(exc):
    s = DictSecrets({"google_token": ga.credentials_to_json(make_creds(expired=True))})
    with pytest.raises(NetworkError) as ei:
        ga.load_credentials(s, http=Seq([exc]))
    assert ei.value.retryable


def test_load_without_injected_http_uses_httplib2_and_is_blocked_by_harness():
    s = DictSecrets({"google_token": ga.credentials_to_json(make_creds(expired=True))})
    with pytest.raises(AssertionError, match="oauth2.googleapis.com"):
        ga.load_credentials(s)  # reaches the real transport -> our harness stops it


# ---------------------------------------------------------------------------------------------- authorize_interactive


class FlowSpy:
    calls: list[dict] = []
    scopes_seen: list = []


@pytest.fixture
def flow_spy(monkeypatch):
    spy = FlowSpy()
    spy.calls = []
    spy.scopes_seen = []
    spy.result = make_creds(granted_scopes=list(ga.SCOPES))

    def fake_run(self, **kwargs):
        spy.calls.append(kwargs)
        spy.scopes_seen.append(list(self.oauth2session.scope))
        if isinstance(spy.result, BaseException):
            raise spy.result
        return spy.result

    monkeypatch.setattr(InstalledAppFlow, "run_local_server", fake_run)
    return spy


def test_authorize_interactive_passes_offline_consent_and_scopes(flow_spy):
    c = ga.authorize_interactive(CLIENT_JSON, open_browser=False, port=0)
    assert c is flow_spy.result
    (kw,) = flow_spy.calls
    assert kw["access_type"] == "offline" and kw["prompt"] == "consent"
    assert kw["open_browser"] is False and kw["port"] == 0 and kw["host"] == "localhost"
    assert "{url}" in kw["authorization_prompt_message"] and "Browser" in kw["authorization_prompt_message"]
    assert "Fenster" in kw["success_message"]
    assert flow_spy.scopes_seen == [list(ga.SCOPES)]


def test_authorize_interactive_defaults_open_browser(flow_spy):
    ga.authorize_interactive(CLIENT_JSON)
    assert flow_spy.calls[0]["open_browser"] is True and flow_spy.calls[0]["port"] == 0


def test_authorize_not_all_scopes_granted(flow_spy):
    flow_spy.result = make_creds(granted_scopes=[ga.SCOPES[0]])
    with pytest.raises(GoogleAuthError) as ei:
        ga.authorize_interactive(CLIENT_JSON, open_browser=False)
    assert "Berechtigungen" in ei.value.message


def test_authorize_without_refresh_token(flow_spy):
    flow_spy.result = make_creds(refresh_token=None, granted_scopes=list(ga.SCOPES))
    with pytest.raises(GoogleAuthError) as ei:
        ga.authorize_interactive(CLIENT_JSON, open_browser=False)
    assert "Refresh-Token" in ei.value.message


@pytest.mark.parametrize(
    "raised, expected",
    [
        (WSGITimeoutError("timeout"), GoogleAuthError),
        (oauth_errors.AccessDeniedError(), GoogleAuthError),
        (oauth_errors.InvalidGrantError(), GoogleAuthError),
        (Warning("Scope has changed from a to b"), GoogleAuthError),
        (OSError("port in use"), NetworkError),
    ],
)
def test_authorize_errors_are_translated(flow_spy, raised, expected):
    flow_spy.result = raised
    with pytest.raises(expected):
        ga.authorize_interactive(CLIENT_JSON, open_browser=False)


@pytest.mark.parametrize(
    "text",
    ["", "{", "[]", json.dumps({"web": {"client_id": "x", "client_secret": "y", "auth_uri": "a", "token_uri": "t"}}), json.dumps({"installed": {}})],
)
def test_authorize_rejects_bad_client_json(flow_spy, text):
    with pytest.raises(ConfigError) as ei:
        ga.authorize_interactive(text, open_browser=False)
    assert "Desktop-App" in ei.value.hint
    assert flow_spy.calls == []


# ---------------------------------------------------------------------------------------------- setup helpers


def cal_service(*responses):
    http = Seq(list(responses))
    return build("calendar", "v3", http=http, static_discovery=True, cache_discovery=False), http


def task_service(*responses):
    http = Seq(list(responses))
    return build("tasks", "v1", http=http, static_discovery=True, cache_discovery=False), http


def cal(id_, summary="Schule", role="owner", **kw):
    return {"id": id_, "summary": summary, "accessRole": role, **kw}


def test_find_calendar_exactly_one_match_across_pages():
    svc, http = cal_service(
        ok({"items": [cal("a", "Privat"), cal("p", "max@example.com", primary=True)], "nextPageToken": "n2"}),
        ok({"items": [cal("s1")]}),
    )
    found = ga.find_calendar_by_name(svc, "Schule")
    assert found["id"] == "s1"
    assert "showHidden=true" in http.request_sequence[0][0] and "pageToken=n2" in http.request_sequence[1][0]


def test_find_calendar_writer_role_ok():
    svc, _ = cal_service(ok({"items": [cal("s1", role="writer")]}))
    assert ga.find_calendar_by_name(svc, "Schule")["accessRole"] == "writer"


def test_find_calendar_none_has_create_hint():
    svc, _ = cal_service(ok({"items": [cal("a", "Privat"), cal("b", "Schulfest")]}))
    with pytest.raises(TargetUnavailableError) as ei:
        ga.find_calendar_by_name(svc, "Schule")
    assert "Schule" in ei.value.message and "anlegen" in ei.value.hint and "Google Kalender" in ei.value.hint


def test_find_calendar_empty_list():
    svc, _ = cal_service(ok({}))
    with pytest.raises(TargetUnavailableError):
        ga.find_calendar_by_name(svc, "Schule")


def test_find_calendar_several_lists_candidates():
    svc, _ = cal_service(ok({"items": [cal("s1"), cal("s2", role="reader"), cal("x", "Anderes")]}))
    with pytest.raises(ConfigError) as ei:
        ga.find_calendar_by_name(svc, "Schule")
    m = ei.value.message
    assert "s1" in m and "s2" in m and "owner" in m and "reader" in m and "Schule" in m
    assert "Anderes" not in m


def test_find_calendar_primary_match_is_never_accepted():
    svc, _ = cal_service(ok({"items": [cal("me@example.com", "Schule", primary=True)]}))
    with pytest.raises(TargetUnavailableError) as ei:
        ga.find_calendar_by_name(svc, "Schule")
    assert "Hauptkalender" in ei.value.message
    # primary + a second calendar of the same name is ambiguous -> still no acceptance
    svc, _ = cal_service(ok({"items": [cal("me@example.com", "Schule", primary=True), cal("s1")]}))
    with pytest.raises(ConfigError) as ei2:
        ga.find_calendar_by_name(svc, "Schule")
    assert "Hauptkalender" in ei2.value.message


@pytest.mark.parametrize("role", ["reader", "freeBusyReader", "writerWithoutPrivateAccess"])
def test_find_calendar_requires_write_role(role):
    svc, _ = cal_service(ok({"items": [cal("s1", role=role)]}))
    with pytest.raises(TargetUnavailableError) as ei:
        ga.find_calendar_by_name(svc, "Schule")
    assert "Schreibrechte" in ei.value.message


def test_find_calendar_uses_the_name_the_user_sees():
    svc, _ = cal_service(ok({"items": [cal("s1", "Calendar of x", summaryOverride="Schule"), cal("s2", "Schule", summaryOverride="Anderes")]}))
    assert ga.find_calendar_by_name(svc, "Schule")["id"] == "s1"


def test_find_calendar_api_errors_are_translated():
    svc, _ = cal_service(({"status": "403"}, json.dumps({"error": {"code": 403, "message": "m", "errors": [{"reason": "insufficientPermissions"}]}})))
    from schulsync.errors import AccessDeniedError

    with pytest.raises(AccessDeniedError):
        ga.find_calendar_by_name(svc, "Schule")


def test_find_or_create_tasklist_existing_is_reused_without_insert():
    svc, http = task_service(ok({"items": [{"id": "L1", "title": "Privat"}], "nextPageToken": "n2"}), ok({"items": [{"id": "L2", "title": "Schule"}]}))
    res = ga.find_or_create_tasklist(svc, "Schule")
    list_id, created = res  # tuple unpacking
    assert (list_id, created) == ("L2", False) and res.id == "L2" and res.created is False and res.note is None
    assert len(http.request_sequence) == 2 and all(r[1] == "GET" for r in http.request_sequence)
    assert "pageToken=n2" in http.request_sequence[1][0]


def test_find_or_create_tasklist_creates_when_missing_without_retry(no_sleep):
    svc, http = task_service(ok({"items": [{"id": "L1", "title": "schule"}]}), ok({"id": "L9", "title": "Schule"}))
    res = ga.find_or_create_tasklist(svc, "Schule")  # exact match: lower-case title is a different list
    assert tuple(res) == ("L9", True)
    uri, method, body, _ = http.request_sequence[1]
    assert method == "POST" and json.loads(body) == {"title": "Schule"}


def test_find_or_create_tasklist_insert_failure_is_not_retried(no_sleep):
    svc, http = task_service(ok({}), ({"status": "503"}, "x"))
    with pytest.raises(OutcomeUnknownError):
        ga.find_or_create_tasklist(svc, "Schule")
    assert len(http.request_sequence) == 2 and no_sleep == []


def test_find_or_create_tasklist_duplicates_choose_smallest_id_and_say_so():
    items = {"items": [{"id": "Lb", "title": "Schule"}, {"id": "La", "title": "Schule"}, {"id": "Lc", "title": "Schule"}]}
    svc, _ = task_service(ok(items))
    res = ga.find_or_create_tasklist(svc, "Schule")
    assert res.id == "La" and res.created is False and "3" in res.note and "La" in res.note
    items["items"].reverse()
    svc, _ = task_service(ok(items))
    assert ga.find_or_create_tasklist(svc, "Schule").id == "La"  # independent of API order
