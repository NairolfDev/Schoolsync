"""factories.py: production wiring. No network: sockets, credentials and the gateway class are replaced by spies."""

from __future__ import annotations

import json
import socket
from dataclasses import replace

import pytest

from schulsync import factories, report, runner
from schulsync.adapters.eduvidual import EduvidualAdapter
from schulsync.adapters.webuntis import WebUntisAdapter
from schulsync.config import Config, EduvidualConfig, GoogleConfig, RetryConfig, WebUntisConfig
from schulsync.errors import ConfigError, GoogleAuthError
from schulsync.google_auth import SECRET_CLIENT, SECRET_TOKEN
from schulsync.http import RequestsHttp
from schulsync.models import Source
from schulsync.redact import clear_registered_secrets, redact
from schulsync.secrets_store import MemorySecretStore

PASSWORD = "hunter2-dummy-edu"
PASSWORD_UNTIS = "hunter2-dummy-untis"


@pytest.fixture(autouse=True)
def _clean_redaction():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def config() -> Config:
    return Config(
        eduvidual=EduvidualConfig(username="schueler1"),
        webuntis=WebUntisConfig(username="schueler2"),
        google=GoogleConfig(calendar_id="cal-id-1", tasklist_id="list-id-1"),
        retry=RetryConfig(connect_timeout=7.0, read_timeout=21.0),
    )


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore({"eduvidual_password": PASSWORD, "webuntis_password": PASSWORD_UNTIS})


class RecordingStore(MemorySecretStore):
    """Counts reads: build_deps must not touch the store."""

    def __init__(self, initial=None):
        super().__init__(initial)
        self.reads: list[str] = []

    def get(self, name):
        self.reads.append(name)
        return super().get(name)


# --------------------------------------------------------------------------- build_deps (lazy)


def test_build_deps_is_lazy_and_reads_no_secret(paths, config):
    store = RecordingStore({"eduvidual_password": PASSWORD})
    deps = factories.build_deps(paths, config, store)
    assert store.reads == []
    assert deps.paths == paths and deps.config is config
    assert set(deps.adapter_factories) == {Source.EDUVIDUAL, Source.WEBUNTIS}
    assert callable(deps.gateway_factory) and callable(deps.network_ready)
    assert deps.open_report is report.open_report
    assert deps.state is None


def test_build_deps_only_contains_enabled_sources(paths, config, secrets):
    cfg = replace(config, webuntis=replace(config.webuntis, enabled=False))
    deps = factories.build_deps(paths, cfg, secrets)
    assert list(deps.adapter_factories) == [Source.EDUVIDUAL]


def test_adapter_factories_build_the_real_adapters(paths, config, secrets):
    deps = factories.build_deps(paths, config, secrets)
    edu = deps.adapter_factories[Source.EDUVIDUAL]()
    untis = deps.adapter_factories[Source.WEBUNTIS]()
    assert isinstance(edu, EduvidualAdapter) and edu.source == Source.EDUVIDUAL
    assert isinstance(untis, WebUntisAdapter) and untis.source == Source.WEBUNTIS


# --------------------------------------------------------------------------- adapters / secrets


@pytest.mark.parametrize("source", [Source.EDUVIDUAL, Source.WEBUNTIS])
def test_missing_password_is_a_config_error_with_setup_hint_not_a_crash(paths, config, source):
    deps = factories.build_deps(paths, config, MemorySecretStore())
    with pytest.raises(ConfigError) as ei:
        deps.adapter_factories[source]()
    assert "Passwort" in ei.value.message
    assert f"setup --{source.value}" in ei.value.hint


def test_missing_username_is_a_config_error(config, secrets):
    cfg = replace(config, eduvidual=replace(config.eduvidual, username="  "))
    with pytest.raises(ConfigError) as ei:
        factories.make_adapter(Source.EDUVIDUAL, cfg, secrets)
    assert "Benutzername" in ei.value.message and "setup --eduvidual" in ei.value.hint


def test_loaded_passwords_are_registered_for_redaction(config, secrets):
    factories.make_adapter(Source.EDUVIDUAL, config, secrets)
    factories.make_adapter(Source.WEBUNTIS, config, secrets)
    text = redact(f"login with {PASSWORD} and {PASSWORD_UNTIS}")
    assert PASSWORD not in text and PASSWORD_UNTIS not in text


def test_http_timeouts_come_from_retry_config(config, secrets):
    client = factories.default_http_factory(config)()
    assert isinstance(client, RequestsHttp) and client._timeout == (7.0, 21.0)
    untis = factories.make_adapter(Source.WEBUNTIS, config, secrets)
    assert untis._http._timeout == (7.0, 21.0)  # WebUntisAdapter has no `retry` argument: the factory carries the limits
    edu = factories.make_adapter(Source.EDUVIDUAL, config, secrets)
    assert edu._http_factory()._timeout == (7.0, 21.0)


def test_custom_http_factory_is_used(config, secrets):
    made = []

    def factory():
        made.append(1)
        return RequestsHttp(1.0, 2.0)

    adapter = factories.make_adapter(Source.WEBUNTIS, config, secrets, http_factory=factory)
    assert made == [1] and adapter._http._timeout == (1.0, 2.0)


# --------------------------------------------------------------------------- Google gateway


def test_gateway_factory_empty_ids_are_config_errors_with_setup_hint(paths, config, secrets):
    for cfg in (
        replace(config, google=replace(config.google, calendar_id="")),
        replace(config, google=replace(config.google, tasklist_id=" ")),
    ):
        deps = factories.build_deps(paths, cfg, secrets)
        with pytest.raises(ConfigError) as ei:
            deps.gateway_factory()
        assert "setup --google" in ei.value.hint and "Hauptkalender" in ei.value.hint


def test_gateway_factory_without_token_is_a_google_auth_error(paths, config, secrets):
    deps = factories.build_deps(paths, config, secrets)
    with pytest.raises(GoogleAuthError) as ei:
        deps.gateway_factory()
    assert "setup" in ei.value.hint


def test_gateway_factory_passes_ids_names_and_timeout(monkeypatch, paths, config):
    token = json.dumps({"refresh_token": "1//dummy-refresh-token-xyz", "client_secret": "dummy-client-secret-xyz"})
    store = MemorySecretStore({SECRET_TOKEN: token})
    calls = {}
    monkeypatch.setattr(factories, "load_credentials", lambda secrets, *, timeout: calls.update(timeout=timeout) or "CREDS")

    class SpyGateway:
        def __init__(self, creds, **kw):
            calls.update(creds=creds, **kw)

    monkeypatch.setattr(factories, "RealGoogleGateway", SpyGateway)
    gateway = factories.build_deps(paths, config, store).gateway_factory()
    assert isinstance(gateway, SpyGateway)
    assert calls == {
        "timeout": 21.0,
        "creds": "CREDS",
        "calendar_id": "cal-id-1",
        "tasklist_id": "list-id-1",
        "calendar_name": "Schule",
        "tasklist_title": "Schule",
    }
    masked = redact("x 1//dummy-refresh-token-xyz y dummy-client-secret-xyz")  # JSON leaf values are registered
    assert "dummy-refresh-token-xyz" not in masked and "dummy-client-secret-xyz" not in masked


def test_the_gateway_factory_registers_the_oauth_client_file_and_the_token_a_refresh_just_stored(monkeypatch, paths, config):
    """Both Google secrets must be known to the masking BEFORE anything can be logged: the OAuth client file (its client_secret
    appears in no token) and, after `load_credentials` refreshed the token, the NEW token the refresh stored. Each registration
    is pinned separately: dropping either one left the whole suite green."""
    client = json.dumps({"installed": {"client_id": "1234-dummy.apps.googleusercontent.com", "client_secret": "dummy-client-file-secret-qq"}})
    old_token = json.dumps({"refresh_token": "dummy-old-refresh-token-qq", "token": "dummy-old-access-token-qq"})
    store = MemorySecretStore({SECRET_CLIENT: client, SECRET_TOKEN: old_token})

    def refreshing_login(secrets, *, timeout):  # a refresh: the store now holds a new access token
        secrets.set(SECRET_TOKEN, json.dumps({"refresh_token": "dummy-old-refresh-token-qq", "token": "dummy-new-access-token-qq"}))
        return "CREDS"

    monkeypatch.setattr(factories, "load_credentials", refreshing_login)
    monkeypatch.setattr(factories, "RealGoogleGateway", lambda creds, **kw: object())
    factories.build_deps(paths, config, store).gateway_factory()

    masked = redact("a dummy-client-file-secret-qq b dummy-new-access-token-qq c dummy-old-refresh-token-qq")  # shapes no pattern knows
    for secret in ("dummy-client-file-secret-qq", "dummy-new-access-token-qq", "dummy-old-refresh-token-qq"):
        assert secret not in masked, secret


def test_build_google_services_builds_calendar_and_tasks(monkeypatch, config):
    built = []
    monkeypatch.setattr(
        factories.RealGoogleGateway, "_build", staticmethod(lambda name, ver, creds, timeout, http: built.append((name, ver, timeout)) or name)
    )
    assert factories.build_google_services("CREDS", config) == ("calendar", "tasks")
    assert built == [("calendar", "v3", 21.0), ("tasks", "v1", 21.0)]


# --------------------------------------------------------------------------- network probe


def test_network_ready_probes_the_configured_hosts_with_a_capped_timeout(monkeypatch, paths, config, secrets):
    seen = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def connect(address, timeout):
        seen.append((address, timeout))
        raise socket.gaierror(11001, "kein DNS") if address[0] != "htbla-grieskirchen.webuntis.com" else OSError  # all fail

    monkeypatch.setattr(runner.socket, "create_connection", connect)
    ready = factories.build_deps(paths, config, secrets).network_ready
    assert ready() is False
    assert [a for a, _ in seen] == [(h, 443) for h in ("www.googleapis.com", "www.eduvidual.at", "htbla-grieskirchen.webuntis.com")]
    assert all(t == 5.0 for _, t in seen)  # connect_timeout is 7, the probe is capped at 5 s

    monkeypatch.setattr(runner.socket, "create_connection", lambda address, timeout: Conn())
    assert ready() is True


def test_network_ready_ignores_disabled_sources(monkeypatch, paths, config, secrets):
    seen = []
    monkeypatch.setattr(runner.socket, "create_connection", lambda address, timeout: seen.append(address[0]) or (_ for _ in ()).throw(OSError()))
    cfg = replace(config, webuntis=replace(config.webuntis, enabled=False))
    assert factories.build_deps(paths, cfg, secrets).network_ready() is False
    assert seen == ["www.googleapis.com", "www.eduvidual.at"]


def test_expired_google_login_reopens_the_browser_login(monkeypatch):
    """Expired/revoked token -> authorize() is called with the client JSON and the new token is stored."""
    from schulsync import factories
    from schulsync.errors import GoogleAuthError
    from schulsync.config import Config, GoogleConfig
    from dataclasses import replace
    from schulsync.secrets_store import MemorySecretStore

    secrets = MemorySecretStore()
    secrets.set("google_client", "{}")
    calls = {"load": 0, "auth": 0, "saved": 0}

    def load(s, timeout=0):
        calls["load"] += 1
        if calls["load"] == 1:
            raise GoogleAuthError("abgelaufen")
        return object()

    monkeypatch.setattr(factories, "load_credentials", load)
    monkeypatch.setattr(factories, "save_credentials", lambda c, s: calls.__setitem__("saved", 1))
    monkeypatch.setattr(factories, "RealGoogleGateway", lambda *a, **k: "gw")
    cfg = replace(Config(), google=GoogleConfig(calendar_id="c", tasklist_id="t"))
    gw = factories.make_gateway(cfg, secrets, authorize=lambda client: calls.__setitem__("auth", 1) or object())
    assert gw == "gw" and calls == {"load": 2, "auth": 1, "saved": 1}
