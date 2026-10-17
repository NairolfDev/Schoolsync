"""Adversarial review tests for google_real (google_client.py / google_auth.py).

Every test here demonstrates a defect and is expected to FAIL against the current implementation.
Network: only loopback sockets owned by the test are used.
"""

from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest
from google.oauth2.credentials import Credentials

from schulsync import google_client
from schulsync.errors import GoogleAuthError, NetworkError, SchulSyncError
from schulsync.google_client import RealGoogleGateway

CAL = "schule0000000000000000000000000@group.calendar.google.com"
LIST = "MDAwMDAwMDAwMDAwMDAwMA"


def _creds() -> Credentials:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Credentials(
        token="dummy-access-token",
        refresh_token="dummy-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="dummy-client-id",
        client_secret="dummy-client-secret",
        expiry=now + timedelta(hours=1),
    )


class _DropServer:
    """Loopback server that reads each request completely and closes without answering (lost answer)."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.requests: list[bytes] = []
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        self.sock.settimeout(3)
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(1)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head, _, rest = data.partition(b"\r\n\r\n")
                n = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        n = int(line.split(b":")[1])
                while len(rest) < n:
                    rest += conn.recv(65536)
                self.requests.append(data + rest)
            except OSError:
                pass
            conn.close()

    def close(self) -> None:
        self.sock.close()


def test_insert_task_is_sent_only_once_when_the_answer_is_lost(monkeypatch):
    """DESIGN 6 / ANFORDERUNGEN 109-110: insert_task must never be repeated on suspicion (no idempotency key
    exists for tasks). The gateway passes num_retries=0, but httplib2 itself re-sends a request after a
    BadStatusLine on the first try (httplib2.Http._conn_request), so a lost answer duplicates the task."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    srv = _DropServer()
    real_build = google_client.build

    def build_local(name, version, **kw):
        return real_build(name, version, client_options={"api_endpoint": f"http://127.0.0.1:{srv.port}/"}, **kw)

    monkeypatch.setattr(google_client, "build", build_local)
    try:
        gw = RealGoogleGateway(_creds(), calendar_id=CAL, tasklist_id=LIST, timeout=3)
        with pytest.raises(SchulSyncError):
            gw.insert_task({"title": "Bearbeiten: Aufsatz", "notes": "schulsync-id: abc"})
    finally:
        srv.close()
    posts = [r for r in srv.requests if r.startswith(b"POST")]
    assert len(posts) == 1, f"the task POST reached the server {len(posts)} times"


# ---------------------------------------------------------------------------------------------- auth module


class _Secrets:
    def __init__(self, data):
        self.data = dict(data)

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value):
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)


def test_stored_token_without_refresh_token_demands_reauthorization_not_network_retry():
    """DESIGN 7.4 / ANFORDERUNGEN 1: permanent login problems must not look transient. A stored token JSON whose
    refresh_token is null cannot ever be refreshed; google-auth raises RefreshError with ONE argument (no OAuth
    body), which translate_refresh_error maps to NetworkError (retryable, 'Server voruebergehend nicht
    erreichbar') instead of GoogleAuthError with the re-authorization hint."""
    from googleapiclient.http import HttpMockSequence

    from schulsync import google_auth as ga

    expiry = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat() + "Z"
    token = json.dumps(
        {
            "token": "dummy-access-token",
            "refresh_token": None,
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "dummy-client-id",
            "client_secret": "dummy-client-secret",
            "scopes": list(ga.SCOPES),
            "expiry": expiry,
        }
    )
    with pytest.raises(SchulSyncError) as ei:
        ga.load_credentials(_Secrets({ga.SECRET_TOKEN: token}), http=HttpMockSequence([]))
    assert isinstance(ei.value, GoogleAuthError), f"got {type(ei.value).__name__} (retryable={ei.value.retryable})"


@pytest.mark.parametrize("config", ['{"installed": null}', '{"installed": []}', '{"installed": 5}'])
def test_authorize_interactive_malformed_installed_section_is_config_error(config):
    """A client JSON whose 'installed' value is not an object must give the German ConfigError, not a raw
    AttributeError traceback out of google-auth-oauthlib."""
    from schulsync import google_auth as ga
    from schulsync.errors import ConfigError

    with pytest.raises(ConfigError):
        ga.authorize_interactive(config, open_browser=False)


# ---------------------------------------------------------------------------------------------- gateway


def test_gateway_never_writes_to_the_primary_calendar_even_without_check_targets():
    """ANFORDERUNGEN 4: 'Niemals automatisch auf den Hauptkalender ausweichen'. check_targets guards 'primary', but
    insert_event on a gateway configured with calendar_id='primary' happily sends the write to the main calendar."""
    from googleapiclient.http import HttpMockSequence

    from schulsync.errors import TargetUnavailableError

    http = HttpMockSequence([({"status": "200", "content-type": "application/json"}, "{}")] * 3)
    gw = RealGoogleGateway(_creds(), calendar_id="primary", tasklist_id=LIST, http=http)
    with pytest.raises(TargetUnavailableError):
        gw.insert_event({"id": "ss" + "0" * 40, "summary": "Abgabe: x"})
    assert http.request_sequence == [], "a write to /calendars/primary/events was sent"
