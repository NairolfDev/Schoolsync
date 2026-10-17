"""The REAL loopback OAuth flow of `google_auth.authorize_interactive` (ANFORDERUNGEN 4: regular one-time OAuth setup).

`tests/test_google_auth.py` replaces `InstalledAppFlow.run_local_server` with a stub, so the redirect server, the redirect
itself and the code exchange were never exercised and those tests "would pass with a broken real flow". Here the library's own
loopback server runs, a "browser" (an HTTP GET) follows the redirect exactly as Google's page would, and the code exchange goes
to a token endpoint on 127.0.0.1 that this test provides. Checked: the authorization URL that would be opened (scopes, offline
access, consent, PKCE, redirect), the token request (code, grant type, redirect, client, verifier), and what the program makes of
a good answer, a refusal ("Abbrechen") and a login where the user unticked a permission.

What stays fake: Google (both its consent page and its token endpoint). So this proves OUR wiring and error handling against the
library's real flow, not that Google accepts the client: `schulsync setup --google` on the real account does.
"""

from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from google_auth_oauthlib import flow as flow_module

from schulsync import google_auth as ga
from schulsync.errors import GoogleAuthError


class TokenEndpoint:
    """A token endpoint on 127.0.0.1 that records every request and answers with `reply`."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.requests: list[dict[str, list[str]]] = []
        self.authorization: list[str] = []  # the client authenticates with HTTP Basic (client id : client secret)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
                outer.requests.append(parse_qs(body))
                outer.authorization.append(self.headers.get("Authorization", ""))
                data = json.dumps(outer.reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:  # keep the test output quiet
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/token"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _client_json(token_uri: str) -> str:
    return json.dumps(
        {
            "installed": {
                "client_id": "1234-dummy.apps.googleusercontent.com",
                "client_secret": "dummy-client-secret-for-the-loopback-test",
                "auth_uri": "https://accounts.example.invalid/o/oauth2/auth",
                "token_uri": token_uri,
                "redirect_uris": ["http://localhost"],
            }
        }
    )


def _good_reply() -> dict:
    return {
        "access_token": "dummy-access-token-loopback",
        "refresh_token": "dummy-refresh-token-loopback",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": " ".join(ga.SCOPES),
    }


def _run_flow(monkeypatch, endpoint: TokenEndpoint, browser_query):
    """Run `authorize_interactive` in a thread, catch the URL it prints, and let the 'browser' answer with
    `browser_query(state)` (the query string Google's page would redirect to). Returns (auth_url, result_or_exception)."""
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")  # the token endpoint here is plain http on localhost
    printed: Queue[str] = Queue()
    monkeypatch.setattr(flow_module, "print", lambda message, *a, **k: printed.put(str(message)), raising=False)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ga.authorize_interactive, _client_json(endpoint.url), open_browser=False, port=0, timeout_seconds=30)
        try:
            message = printed.get(timeout=20)
        except Empty:
            pytest.fail(f"the flow printed no authorization URL; it ended with: {future.exception(timeout=5)!r}")
        auth_url = next(word for word in message.split() if word.startswith("https://accounts.example.invalid"))
        query = parse_qs(urlsplit(auth_url).query)
        redirect = query["redirect_uri"][0].replace("localhost", "127.0.0.1")  # the library's own redirect server
        requests.get(redirect + "?" + browser_query(query["state"][0]), timeout=20)  # what the browser does after consent
        try:
            return auth_url, future.result(timeout=30)
        except BaseException as exc:  # noqa: BLE001 - the tests look at the program's own error
            return auth_url, exc


@pytest.fixture
def endpoint():
    ep = TokenEndpoint(_good_reply())
    yield ep
    ep.close()


def test_the_real_loopback_flow_asks_for_offline_access_exactly_our_scopes_and_exchanges_the_code(monkeypatch, endpoint):
    auth_url, result = _run_flow(monkeypatch, endpoint, lambda state: f"state={state}&code=dummy-auth-code&scope=" + "+".join(ga.SCOPES))

    # the URL the user's browser is sent to
    query = parse_qs(urlsplit(auth_url).query)
    assert query["response_type"] == ["code"] and query["client_id"] == ["1234-dummy.apps.googleusercontent.com"]
    assert query["scope"] == [" ".join(ga.SCOPES)]  # the minimal set, nothing more
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]  # a refresh token, even for a returning user
    assert query["redirect_uri"][0].startswith("http://localhost:") and "code_challenge" in query  # loopback + PKCE

    # the exchange of the code at the token endpoint
    (token_request,) = endpoint.requests
    assert token_request["grant_type"] == ["authorization_code"] and token_request["code"] == ["dummy-auth-code"]
    assert token_request["redirect_uri"] == query["redirect_uri"] and "code_verifier" in token_request
    (basic,) = endpoint.authorization
    assert base64.b64decode(basic.removeprefix("Basic ")).decode() == (
        "1234-dummy.apps.googleusercontent.com:dummy-client-secret-for-the-loopback-test"
    )

    # what the program makes of it
    assert not isinstance(result, BaseException), repr(result)
    assert result.token == "dummy-access-token-loopback" and result.refresh_token == "dummy-refresh-token-loopback"
    assert set(ga.SCOPES) <= set(result.granted_scopes or result.scopes)
    stored = json.loads(ga.credentials_to_json(result))
    assert stored["refresh_token"] == "dummy-refresh-token-loopback" and stored["client_id"].startswith("1234-dummy")


def test_the_real_loopback_flow_turns_a_refusal_in_the_browser_into_a_german_error(monkeypatch, endpoint):
    """The user presses 'Abbrechen' on Google's page: the redirect carries `error=access_denied`. No code is exchanged."""
    _auth_url, result = _run_flow(monkeypatch, endpoint, lambda state: f"state={state}&error=access_denied")

    assert isinstance(result, GoogleAuthError), repr(result)
    assert "nicht bestätigt" in result.message and "schulsync setup" in (result.hint or "")
    assert endpoint.requests == []


def test_the_real_loopback_flow_notices_an_unticked_permission(monkeypatch):
    """Google answers with fewer scopes than asked for (the user unticked the Tasks box): setup must say so, not store half a login."""
    ep = TokenEndpoint({**_good_reply(), "scope": ga.SCOPES[0]})
    try:
        _auth_url, result = _run_flow(monkeypatch, ep, lambda state: f"state={state}&code=dummy-auth-code&scope=" + ga.SCOPES[0])
    finally:
        ep.close()

    assert isinstance(result, GoogleAuthError), repr(result)
    assert "Berechtigungen" in result.message and "Kästchen" in (result.hint or "")


def test_the_real_loopback_flow_without_a_refresh_token_is_refused(monkeypatch):
    """An answer without a refresh token would work for an hour and then never again: refused with the way out."""
    reply = {k: v for k, v in _good_reply().items() if k != "refresh_token"}
    ep = TokenEndpoint(reply)
    try:
        _auth_url, result = _run_flow(monkeypatch, ep, lambda state: f"state={state}&code=dummy-auth-code&scope=" + "+".join(ga.SCOPES))
    finally:
        ep.close()

    assert isinstance(result, GoogleAuthError), repr(result)
    assert "Refresh-Token" in result.message
