"""Tests for google_client.RealGoogleGateway and the error translation.

No real Google call is ever made: HTTP is an `HttpMockSequence` (googleapiclient.http) and an autouse fixture
blocks DNS and socket connects, so any code path that bypasses the injected transport fails loudly. API shapes
(paths, query parameters) come from the static discovery documents shipped with google-api-python-client; the
JSON answers below are SYNTHETIC (hand written from the public API reference), not recordings of real Google.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import google_auth_httplib2
import httplib2
import pytest
from google.auth import exceptions as auth_exceptions
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMockSequence

from schulsync.errors import (
    AccessDeniedError,
    ConflictError,
    GoogleAuthError,
    IncompleteDataError,
    NetworkError,
    NotFoundError,
    OutcomeUnknownError,
    SchulSyncError,
    TargetUnavailableError,
    TransientError,
)
from schulsync.google_auth import SCOPES
from schulsync.google_client import RealGoogleGateway, RequestRejectedError, translate_http_error
from schulsync.google_types import TargetCheck

CAL = "schule0000000000000000000000000@group.calendar.google.com"
LIST = "MDAwMDAwMDAwMDAwMDAwMA"
DUMMY_TOKEN = "dummy-access-token"  # obviously fake


# ---------------------------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """DNS and connects are forbidden. `dns_calls` records attempts to leave the process."""
    dns_calls: list[str] = []

    def fake_getaddrinfo(host, *args, **kwargs):
        dns_calls.append(str(host))
        raise socket.gaierror(11001, "network disabled in tests")

    def forbidden_connect(self, *args, **kwargs):
        raise AssertionError("a test tried to open a real socket")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", forbidden_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_connect)
    return dns_calls


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    return sleeps


class Seq(HttpMockSequence):
    """HttpMockSequence that can also raise: items that are exceptions are raised instead of returned."""

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        if self._iterable and isinstance(self._iterable[0], BaseException):
            self.request_sequence.append((uri, method, body, headers))
            raise self._iterable.pop(0)
        return super().request(uri, method, body=body, headers=headers, **kwargs)


def ok(body: dict | None = None, status: int = 200):
    return ({"status": str(status), "content-type": "application/json"}, json.dumps(body if body is not None else {}))


def err(status: int, reason: str | None = None, message: str = "synthetic error"):
    error: dict = {"code": status, "message": message}
    if reason:
        error["errors"] = [{"domain": "usageLimits", "reason": reason, "message": message}]
    return ({"status": str(status), "content-type": "application/json"}, json.dumps({"error": error}))


def creds(*, expired: bool = False) -> Credentials:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Credentials(
        token=DUMMY_TOKEN,
        refresh_token="dummy-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="dummy-client-id.apps.googleusercontent.com",
        client_secret="dummy-client-secret",
        scopes=list(SCOPES),  # what the real login grants (a single scope here would hide a call that needs another one)
        expiry=now - timedelta(hours=1) if expired else now + timedelta(hours=1),
    )


def make(responses, **kw) -> tuple[RealGoogleGateway, Seq]:
    http = Seq(list(responses))
    kw.setdefault("read_retries", 0)
    gw = RealGoogleGateway(creds(), calendar_id=CAL, tasklist_id=LIST, http=http, **kw)
    return gw, http


def query(http: Seq, index: int = 0) -> dict[str, list[str]]:
    return parse_qs(urlparse(http.request_sequence[index][0]).query)


def path(http: Seq, index: int = 0) -> str:
    return urlparse(http.request_sequence[index][0]).path


def http_error(status: int, reason: str | None = None, *, raw: bytes | None = None) -> HttpError:
    if raw is None:
        raw = json.dumps(
            {"error": {"code": status, "message": "synthetic", "errors": [{"reason": reason}] if reason else []}}
        ).encode()
    return HttpError(httplib2.Response({"status": str(status)}), raw)


# ---------------------------------------------------------------------------------------------- translation


@pytest.mark.parametrize(
    "exc, write, expected",
    [
        (http_error(401, "authError"), False, GoogleAuthError),
        (http_error(401, "authError"), True, GoogleAuthError),
        (http_error(403, "rateLimitExceeded"), False, TransientError),
        (http_error(403, "userRateLimitExceeded"), False, TransientError),
        (http_error(403, "quotaExceeded"), False, TransientError),
        (http_error(403, "rateLimitExceeded"), True, TransientError),  # rejected before processing: not "unknown"
        (
            http_error(
                403,
                raw=json.dumps(
                    {"error": {"code": 403, "status": "RESOURCE_EXHAUSTED", "details": [{"reason": "RATE_LIMIT_EXCEEDED"}]}}
                ).encode(),
            ),
            False,
            TransientError,
        ),
        (http_error(403, "forbiddenForNonOrganizer"), False, AccessDeniedError),
        (http_error(403, "insufficientPermissions"), True, AccessDeniedError),
        (http_error(403, raw=b"<html>not json</html>"), False, AccessDeniedError),
        (http_error(404, "notFound"), False, NotFoundError),
        (http_error(410, "deleted"), False, NotFoundError),
        (http_error(409, "duplicate"), True, ConflictError),
        (http_error(412, "conditionNotMet"), False, ConflictError),
        (http_error(429, "rateLimitExceeded"), False, TransientError),
        (http_error(429, "rateLimitExceeded"), True, TransientError),
        (http_error(500, "backendError"), False, TransientError),
        (http_error(503), False, TransientError),
        (http_error(500, "backendError"), True, OutcomeUnknownError),
        (http_error(503), True, OutcomeUnknownError),
        (http_error(400, "invalid"), False, RequestRejectedError),
        # transport
        (httplib2.ServerNotFoundError("dns"), False, NetworkError),
        (httplib2.ServerNotFoundError("dns"), True, NetworkError),  # request never sent
        (socket.gaierror(11001, "dns"), True, NetworkError),
        (ConnectionRefusedError(), True, NetworkError),
        (ssl.SSLCertVerificationError(), True, NetworkError),
        (socket.timeout("timed out"), False, NetworkError),
        (socket.timeout("timed out"), True, OutcomeUnknownError),
        (TimeoutError(), True, OutcomeUnknownError),
        (ConnectionResetError(), False, NetworkError),
        (ConnectionResetError(), True, OutcomeUnknownError),
        (ssl.SSLError("handshake"), False, NetworkError),
        (ssl.SSLError("read"), True, OutcomeUnknownError),
        (OSError("broken"), False, NetworkError),
        (OSError("broken"), True, OutcomeUnknownError),
        (json.JSONDecodeError("bad", "x", 0), False, NetworkError),
        (json.JSONDecodeError("bad", "x", 0), True, OutcomeUnknownError),
        (auth_exceptions.TransportError("refresh failed"), True, NetworkError),
        (auth_exceptions.RefreshError("invalid_grant: Bad Request", {"error": "invalid_grant"}), True, GoogleAuthError),
        (auth_exceptions.RefreshError("server", {"error": "server_error"}, retryable=True), True, NetworkError),
    ],
)
def test_translate_table(exc, write, expected):
    out = translate_http_error(exc, op="Test", write=write)
    # exact class: OutcomeUnknownError/NetworkError are subclasses of TransientError
    assert type(out) is expected


def test_translate_retryable_flags():
    assert translate_http_error(http_error(403, "rateLimitExceeded"), op="x").retryable
    assert translate_http_error(http_error(500), op="x", write=True).retryable
    assert not translate_http_error(http_error(403, "forbiddenForNonOrganizer"), op="x").retryable
    assert not translate_http_error(http_error(401), op="x").retryable


def test_translate_unknown_exception_is_generic_not_swallowed_silently():
    out = translate_http_error(KeyError("x"), op="Test")
    assert type(out) is SchulSyncError


def test_translated_messages_are_german_and_secret_free():
    secret_body = json.dumps(
        {"error": {"code": 500, "message": "token ya29.SECRET-VALUE at https://x/?key=hunter2-dummy"}}
    ).encode()
    for exc in (
        HttpError(httplib2.Response({"status": "500"}), secret_body, uri="https://x/?access_token=ya29.SECRET-VALUE"),
        HttpError(httplib2.Response({"status": "401"}), secret_body),
        auth_exceptions.RefreshError("invalid_grant: ya29.SECRET-VALUE", {"error": "invalid_grant", "error_description": "ya29.SECRET-VALUE"}),
    ):
        out = translate_http_error(exc, op="Termin anlegen", write=True)
        blob = " ".join(filter(None, [out.message, out.hint, out.detail]))
        assert "SECRET" not in blob and "hunter2" not in blob and "https://" not in blob
        assert out.message  # German text present
    auth = translate_http_error(http_error(401), op="Termin lesen")
    assert "schulsync setup" in auth.hint and "7 Tage" in auth.hint


_SECRET_SHAPES = (
    "ya29.A0ARrdaM-dummyAccessTokenValue123",  # access token
    "1//0gDummyRefreshTokenValueABCDEFGH",  # refresh token
    "GOCSPX-dummyClientSecretValue12345",  # client secret
    "Bearer dummyBearerCredential9876",
    "https://dummyuser:dummyPassw0rd@example.invalid/cal?sig=dummySignature42&auth=dummyAuth77",
    "sesskey=dummySess55 cookie: MoodleSession=dummyCookie88",
)


def _everything_google_could_echo(secret: str):
    """One exception of every kind the translator handles, with `secret` in every field it could possibly read."""
    body = json.dumps(
        {"error": {"code": 500, "message": secret, "errors": [{"domain": secret, "reason": "backendError", "message": secret}], "status": "INTERNAL"}}
    ).encode()
    yield HttpError(httplib2.Response({"status": "500"}), body, uri=secret)
    for status in (400, 401, 403, 404, 409, 429, 503):
        yield HttpError(httplib2.Response({"status": str(status), "reason": secret}), body, uri=secret)
    yield auth_exceptions.RefreshError(secret, {"error": "invalid_grant", "error_description": secret})
    yield auth_exceptions.RefreshError(secret)  # unstructured
    yield auth_exceptions.TransportError(secret)
    yield OSError(secret)
    yield ConnectionResetError(secret)
    yield TimeoutError(secret)
    yield httplib2.ServerNotFoundError(secret)
    yield ssl.SSLError(secret)
    yield ValueError(secret)
    yield RuntimeError(secret)  # an unexpected type


@pytest.mark.parametrize("secret", _SECRET_SHAPES)
@pytest.mark.parametrize("write", [False, True])
def test_no_error_text_ever_echoes_anything_google_or_the_transport_said(secret, write):
    """ANFORDERUNGEN 8: no token, cookie or secret URL parameter in reports and logs. Looking for a few known secret shapes
    would pass for a leak of a different shape, so this pins the CONSTRUCTION: the texts of every translated error contain
    none of the many secret-like values that were put into every readable field, and the detail is only a status code with
    Google's machine reasons or a class name, never free text."""
    detail_shape = re.compile(r"^(HTTP \d{3}( \([a-z_, ]+\))?|[A-Za-z_]+)$")
    for exc in _everything_google_could_echo(secret):
        out = translate_http_error(exc, op="Termin anlegen", write=write)
        blob = " | ".join(filter(None, [out.message, out.hint, out.detail]))
        for part in re.split(r"[ :]", secret):
            assert len(part) < 6 or part not in blob, (type(exc).__name__, part)
        assert out.message and (out.detail is None or detail_shape.match(out.detail)), (type(exc).__name__, out.detail)


# ---------------------------------------------------------------------------------------------- transport wiring


def test_services_use_static_discovery_authorized_http_and_timeout(no_network):
    gw = RealGoogleGateway(creds(), calendar_id=CAL, tasklist_id=LIST, timeout=7, read_retries=0)
    for service in (gw._calendar, gw._tasks):
        assert isinstance(service._http, google_auth_httplib2.AuthorizedHttp)
        assert isinstance(service._http.http, httplib2.Http)
        assert service._http.http.timeout == 7
    assert no_network == []  # building the services (discovery) touched no network


def test_real_transport_path_is_blocked_by_the_test_harness(no_network):
    """Without an injected transport the gateway really uses httplib2 -> it hits the (forbidden) DNS lookup,
    and the failure is a clean German NetworkError instead of a real call."""
    gw = RealGoogleGateway(creds(), calendar_id=CAL, tasklist_id=LIST, read_retries=0)
    with pytest.raises(NetworkError) as ei:
        gw.get_event("ss123")
    assert no_network == ["www.googleapis.com"]
    assert "Google" in ei.value.message


def test_injected_transport_is_never_bypassed(no_network):
    gw, http = make([ok({"id": "ss1"}), ok({"id": "t1"})])
    gw.get_event("ss1")
    gw.get_task("t1")
    assert len(http.request_sequence) == 2  # everything went through the injected mock
    assert no_network == []
    assert http.request_sequence[0][3]["authorization"] == f"Bearer {DUMMY_TOKEN}"


def test_expired_token_is_refreshed_through_the_same_transport():
    http = Seq([ok({"access_token": "dummy-new-token", "expires_in": 3600}), ok({"id": "ss1"})])
    c = creds(expired=True)
    gw = RealGoogleGateway(c, calendar_id=CAL, tasklist_id=LIST, http=http, read_retries=0)
    assert gw.get_event("ss1") == {"id": "ss1"}
    assert "oauth2.googleapis.com" in http.request_sequence[0][0]
    assert http.request_sequence[1][3]["authorization"] == "Bearer dummy-new-token"
    assert c.token == "dummy-new-token"


def test_401_triggers_one_refresh_and_a_retry():
    http = Seq([err(401, "authError"), ok({"access_token": "dummy-new-token", "expires_in": 3600}), ok({"id": "ss1"})])
    gw = RealGoogleGateway(creds(), calendar_id=CAL, tasklist_id=LIST, http=http, read_retries=0)
    assert gw.get_event("ss1") == {"id": "ss1"}
    assert len(http.request_sequence) == 3


def test_invalid_grant_during_a_call_maps_to_google_auth_error():
    http = Seq([({"status": "400"}, json.dumps({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))])
    gw = RealGoogleGateway(creds(expired=True), calendar_id=CAL, tasklist_id=LIST, http=http, read_retries=0)
    with pytest.raises(GoogleAuthError) as ei:
        gw.list_tasks()
    assert "schulsync setup" in ei.value.hint
    assert "dummy-refresh-token" not in " ".join(filter(None, [ei.value.message, ei.value.hint, ei.value.detail]))


# ---------------------------------------------------------------------------------------------- calendar


def test_get_event_found_and_cancelled_are_returned():
    gw, http = make([ok({"id": "ssa", "status": "confirmed"}), ok({"id": "ssa", "status": "cancelled"})])
    assert gw.get_event("ssa")["status"] == "confirmed"
    assert gw.get_event("ssa")["status"] == "cancelled"
    assert path(http) == f"/calendar/v3/calendars/{CAL.replace('@', '%40')}/events/ssa"
    assert http.request_sequence[0][1] == "GET"


@pytest.mark.parametrize("status", [404, 410])
def test_get_event_missing_is_none(status):
    gw, _ = make([err(status)])
    assert gw.get_event("ssa") is None


def test_get_event_other_errors_raise():
    gw, _ = make([err(403, "forbiddenForNonOrganizer")])
    with pytest.raises(AccessDeniedError):
        gw.get_event("ssa")


def test_insert_event_posts_body_and_returns_resource():
    gw, http = make([ok({"id": "ssa", "status": "confirmed", "htmlLink": "https://calendar.example/x"})])
    body = {"id": "ssa", "summary": "Abgabe: Protokoll", "extendedProperties": {"private": {"schulsync": "1"}}}
    out = gw.insert_event(body)
    assert out["id"] == "ssa"
    uri, method, sent, _ = http.request_sequence[0]
    assert method == "POST" and json.loads(sent) == body


def test_insert_event_requires_id():
    gw, http = make([])
    with pytest.raises(ValueError):
        gw.insert_event({"summary": "x"})
    assert http.request_sequence == []


def test_insert_event_409_is_conflict():
    gw, _ = make([err(409, "duplicate")])
    with pytest.raises(ConflictError):
        gw.insert_event({"id": "ssa"})


@pytest.mark.parametrize("response", [err(500, "backendError"), err(503)])
def test_insert_event_5xx_is_outcome_unknown_and_never_retried(response, no_sleep):
    gw, http = make([response], read_retries=2)  # retries are configured, inserts must ignore them
    with pytest.raises(OutcomeUnknownError):
        gw.insert_event({"id": "ssa"})
    assert len(http.request_sequence) == 1 and no_sleep == []


@pytest.mark.parametrize("exc", [socket.timeout("timed out"), ConnectionResetError(), ssl.SSLError("read")])
def test_insert_event_transport_failure_after_send_is_outcome_unknown(exc, no_sleep):
    gw, http = make([exc], read_retries=2)
    with pytest.raises(OutcomeUnknownError):
        gw.insert_event({"id": "ssa"})
    assert len(http.request_sequence) == 1


def test_insert_event_dns_failure_is_plain_network_error():
    gw, _ = make([httplib2.ServerNotFoundError("dns")])
    with pytest.raises(NetworkError) as ei:
        gw.insert_event({"id": "ssa"})
    assert type(ei.value) is NetworkError


def test_insert_event_rate_limit_is_transient_not_unknown():
    gw, _ = make([err(403, "rateLimitExceeded")])
    with pytest.raises(TransientError) as ei:
        gw.insert_event({"id": "ssa"})
    assert type(ei.value) is TransientError


def test_patch_event_sends_patch_and_maps_404():
    gw, http = make([ok({"id": "ssa", "summary": "Abgesagt: x"}), err(404, "notFound")])
    assert gw.patch_event("ssa", {"summary": "Abgesagt: x"})["summary"] == "Abgesagt: x"
    assert http.request_sequence[0][1] == "PATCH"
    assert json.loads(http.request_sequence[0][2]) == {"summary": "Abgesagt: x"}
    with pytest.raises(NotFoundError):
        gw.patch_event("ssa", {})


def test_patch_5xx_is_plain_transient_because_patch_is_idempotent():
    gw, _ = make([err(503)])
    with pytest.raises(TransientError) as ei:
        gw.patch_event("ssa", {})
    assert type(ei.value) is TransientError


def test_find_events_by_uid_filters_server_side_and_pages():
    uid = "webuntis:1234"
    live = {"id": "ssa", "status": "confirmed", "extendedProperties": {"private": {"schulsync_uid": uid}}}
    gone = {"id": "ssb", "status": "cancelled"}  # Google may strip properties from deleted events
    foreign = {"id": "zzz", "status": "confirmed", "extendedProperties": {"private": {"schulsync_uid": "other"}}}
    gw, http = make([ok({"items": [live], "nextPageToken": "p2"}), ok({"items": [gone, foreign]})])
    out = gw.find_events_by_uid(uid)
    assert [e["id"] for e in out] == ["ssa", "ssb"]
    q1, q2 = query(http, 0), query(http, 1)
    assert q1["privateExtendedProperty"] == [f"schulsync_uid={uid}"]
    assert q1["showDeleted"] == ["true"] and "pageToken" not in q1
    assert q2["pageToken"] == ["p2"] and q2["privateExtendedProperty"] == q1["privateExtendedProperty"]
    assert path(http).endswith("/events")


def test_find_events_by_uid_empty():
    gw, _ = make([ok({})])
    assert gw.find_events_by_uid("x") == []


def test_paging_loop_raises_incomplete_instead_of_returning_a_short_list():
    gw, _ = make([ok({"items": [], "nextPageToken": "same"}), ok({"items": [], "nextPageToken": "same"})])
    with pytest.raises(IncompleteDataError):
        gw.find_events_by_uid("x")


def test_paging_failure_midway_raises_not_partial():
    gw, _ = make([ok({"items": [{"id": "t1"}], "nextPageToken": "p2"}), err(500)])
    with pytest.raises(TransientError):
        gw.list_tasks()


# ---------------------------------------------------------------------------------------------- tasks


def test_list_tasks_all_pages_with_all_flags():
    t = lambda i, **kw: {"id": i, "title": f"Bearbeiten: {i}", **kw}  # noqa: E731
    gw, http = make(
        [
            ok({"items": [t("a"), t("b", status="completed", hidden=True)], "nextPageToken": "p2"}),
            ok({"items": [t("c", deleted=True)], "nextPageToken": "p3"}),
            ok({"items": [t("d")]}),
        ]
    )
    out = gw.list_tasks()
    assert [x["id"] for x in out] == ["a", "b", "c", "d"]
    for i in range(3):
        q = query(http, i)
        assert q["showCompleted"] == ["true"] and q["showHidden"] == ["true"] and q["showDeleted"] == ["true"]
        assert q["maxResults"] == ["100"]
    assert "pageToken" not in query(http, 0)
    assert query(http, 1)["pageToken"] == ["p2"] and query(http, 2)["pageToken"] == ["p3"]
    assert f"/lists/{LIST}/tasks" in path(http)


def test_list_tasks_empty_list_is_ok():
    gw, _ = make([ok({"kind": "tasks#tasks"})])
    assert gw.list_tasks() == []


@pytest.mark.parametrize("status", [404, 410])
def test_get_task_missing_is_none(status):
    gw, _ = make([err(status)])
    assert gw.get_task("t1") is None


def test_get_task_returns_resource():
    gw, http = make([ok({"id": "t1", "status": "completed"})])
    assert gw.get_task("t1")["status"] == "completed"
    assert path(http).endswith(f"/lists/{LIST}/tasks/t1")


def test_insert_task_posts_body_and_is_never_retried(no_sleep):
    gw, http = make([ok({"id": "t9", "title": "Lernen: Test"})], read_retries=2)
    body = {"title": "Lernen: Test", "notes": "schulsync-id: uid1", "due": "2026-10-20T00:00:00.000Z"}
    assert gw.insert_task(body)["id"] == "t9"
    assert http.request_sequence[0][1] == "POST" and json.loads(http.request_sequence[0][2]) == body
    gw2, http2 = make([err(503)], read_retries=2)
    with pytest.raises(OutcomeUnknownError):
        gw2.insert_task(body)
    assert len(http2.request_sequence) == 1 and no_sleep == []


def test_insert_task_timeout_is_outcome_unknown():
    gw, _ = make([socket.timeout("timed out")])
    with pytest.raises(OutcomeUnknownError):
        gw.insert_task({"title": "x"})


def test_patch_task():
    gw, http = make([ok({"id": "t1", "title": "neu"})])
    assert gw.patch_task("t1", {"title": "neu"})["title"] == "neu"
    assert http.request_sequence[0][1] == "PATCH" and path(http).endswith("/tasks/t1")


@pytest.mark.parametrize("status", [404, 410])
def test_delete_task_missing_counts_as_done(status):
    gw, http = make([err(status)])
    assert gw.delete_task("t1") is None
    assert http.request_sequence[0][1] == "DELETE"


def test_delete_task_success_and_errors():
    gw, _ = make([({"status": "204"}, ""), err(403, "insufficientPermissions"), err(503)])
    assert gw.delete_task("t1") is None
    with pytest.raises(AccessDeniedError):
        gw.delete_task("t1")
    with pytest.raises(TransientError):
        gw.delete_task("t1")


# ---------------------------------------------------------------------------------------------- retries


def test_reads_retry_inside_the_library_but_bounded(no_sleep):
    gw, http = make([err(503), err(503), ok({"id": "ss1"})], read_retries=2)
    assert gw.get_event("ss1") == {"id": "ss1"}
    assert len(http.request_sequence) == 3 and len(no_sleep) == 2
    gw, http = make([err(503), err(503), err(503)], read_retries=2)
    with pytest.raises(TransientError):
        gw.get_event("ss1")
    assert len(http.request_sequence) == 3  # initial try + 2 retries, then give up


def test_read_retries_zero_means_single_attempt(no_sleep):
    gw, http = make([err(503)])
    with pytest.raises(TransientError):
        gw.get_task("t1")
    assert len(http.request_sequence) == 1 and no_sleep == []


# ---------------------------------------------------------------------------------------------- check_targets


def cal_entry(**kw):
    base = {"kind": "calendar#calendarListEntry", "id": CAL, "summary": "Schule", "accessRole": "owner"}
    base.update(kw)
    return base


def test_check_targets_ok():
    gw, http = make([ok(cal_entry()), ok({"id": LIST, "title": "Schule"})])
    assert gw.check_targets() == TargetCheck(CAL, "Schule", LIST, "Schule")
    assert "/users/me/calendarList/" in path(http, 0)
    assert path(http, 1).endswith(f"/users/@me/lists/{LIST}")


def test_check_targets_writer_role_is_enough():
    gw, _ = make([ok(cal_entry(accessRole="writer")), ok({"id": LIST, "title": "Schule"})])
    assert gw.check_targets().calendar_id == CAL


def test_check_targets_primary_calendar_is_refused():
    gw, http = make([ok(cal_entry(primary=True, summary="max@example.com"))])
    with pytest.raises(TargetUnavailableError) as ei:
        gw.check_targets()
    assert "Hauptkalender" in ei.value.message
    assert len(http.request_sequence) == 1  # no tasks call, no fallback


@pytest.mark.parametrize("cid", ["primary", "PRIMARY", ""])
def test_check_targets_never_uses_primary_or_empty_id_and_makes_no_request(cid):
    http = Seq([])
    gw = RealGoogleGateway(creds(), calendar_id=cid, tasklist_id=LIST, http=http, read_retries=0)
    with pytest.raises(TargetUnavailableError):
        gw.check_targets()
    assert http.request_sequence == []


def test_check_targets_calendar_missing_gives_german_hint_and_no_fallback():
    gw, http = make([err(404, "notFound")])
    with pytest.raises(TargetUnavailableError) as ei:
        gw.check_targets()
    assert "Schule" in ei.value.message and "nicht gefunden" in ei.value.message
    assert "schulsync setup" in ei.value.hint
    assert len(http.request_sequence) == 1


def test_check_targets_deleted_calendar_entry():
    gw, _ = make([ok(cal_entry(deleted=True))])
    with pytest.raises(TargetUnavailableError):
        gw.check_targets()


@pytest.mark.parametrize("role", ["reader", "freeBusyReader", "writerWithoutPrivateAccess", None])
def test_check_targets_read_only_role_is_refused(role):
    entry = cal_entry()
    if role is None:
        del entry["accessRole"]
    else:
        entry["accessRole"] = role
    gw, _ = make([ok(entry)])
    with pytest.raises(TargetUnavailableError) as ei:
        gw.check_targets()
    assert "Schreibrechte" in ei.value.message


def test_check_targets_tasklist_missing():
    gw, _ = make([ok(cal_entry()), err(404, "notFound")])
    with pytest.raises(TargetUnavailableError) as ei:
        gw.check_targets()
    assert "Aufgabenliste" in ei.value.message and "schulsync setup" in ei.value.hint


def test_check_targets_tasklist_not_configured():
    gw = RealGoogleGateway(creds(), calendar_id=CAL, tasklist_id="", http=Seq([]), read_retries=0)
    with pytest.raises(TargetUnavailableError):
        gw.check_targets()


def test_check_targets_other_errors_are_not_turned_into_missing():
    gw, _ = make([err(403, "insufficientPermissions")])
    with pytest.raises(AccessDeniedError):
        gw.check_targets()
    gw, _ = make([httplib2.ServerNotFoundError("dns")])
    with pytest.raises(NetworkError):
        gw.check_targets()
    gw, _ = make([ok(cal_entry()), err(503)])
    with pytest.raises(TransientError):
        gw.check_targets()


def test_prebuilt_services_can_be_injected():
    class Boom:
        def __getattr__(self, name):  # pragma: no cover - must never be touched
            raise AssertionError("unexpected service access")

    gw = RealGoogleGateway(creds(), calendar_id=CAL, tasklist_id=LIST, calendar_service=Boom(), tasks_service=Boom())
    assert isinstance(gw, RealGoogleGateway)
