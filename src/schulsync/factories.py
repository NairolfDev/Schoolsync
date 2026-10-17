"""Production wiring: real adapters, real Google gateway, network probe, report opener.

Everything is built lazily: `build_deps` itself performs no I/O and reads no secret. A missing password, a missing
Google login or empty target ids surface when the runner calls the factory, as a `SchulSyncError` (-> source/target
error in the report), never as a crash at start-up.

Secret names in the protected store: ``eduvidual_password``, ``webuntis_password`` (this module), ``google_client`` and
``google_token`` (google_auth). Every secret that is read is registered with `redact` first (`register_loaded_secrets`).

Note on the adapters' constructors (checked in the code, not from memory): `EduvidualAdapter(config, password,
http_factory=None, retry=None)` and `WebUntisAdapter(config, password, http_factory=None, *, clock=...)`; only the
first knows `retry`, so the timeouts of `RetryConfig` reach both through an explicit `http_factory`.
"""

from __future__ import annotations

from typing import Any, Callable

from . import report
from .adapters.eduvidual import EduvidualAdapter
from .adapters.webuntis import WebUntisAdapter
from .config import Config, Paths
from .errors import ConfigError
from .errors import GoogleAuthError
from .google_auth import (
    SECRET_CLIENT,
    SECRET_TOKEN,
    authorize_interactive,
    bundled_client_json,
    load_credentials,
    save_credentials,
)
from .google_client import RealGoogleGateway
from .google_types import GoogleGateway
from .http import HttpClient, RequestsHttp
from .models import Source, SourceAdapter
from .runner import Deps, default_network_ready, network_hosts
from .secrets_store import SecretStore, register_loaded_secrets

SECRET_EDUVIDUAL = "eduvidual_password"
SECRET_WEBUNTIS = "webuntis_password"
PASSWORD_SECRETS = {Source.EDUVIDUAL: SECRET_EDUVIDUAL, Source.WEBUNTIS: SECRET_WEBUNTIS}
SOURCE_LABEL = {Source.EDUVIDUAL: "Eduvidual", Source.WEBUNTIS: "WebUntis"}
_PROBE_TIMEOUT_CAP = 5.0  # seconds per host: the probe must stay short, the runner polls it repeatedly


# --------------------------------------------------------------------------- HTTP / adapters


def default_http_factory(config: Config) -> Callable[[], HttpClient]:
    r = config.retry
    return lambda: RequestsHttp(r.connect_timeout, r.read_timeout)


def make_adapter(
    source: Source,
    config: Config,
    secrets: SecretStore,
    *,
    http_factory: Callable[[], HttpClient] | None = None,
) -> SourceAdapter:
    """Build the real adapter of `source`. Raises ConfigError (German, with setup hint) for a missing user name or
    password; no network access happens here."""
    source = Source(source)
    label = SOURCE_LABEL[source]
    section = config.eduvidual if source is Source.EDUVIDUAL else config.webuntis
    setup_hint = f"Bitte 'schulsync setup --{source.value}' ausführen."
    if not section.username.strip():
        raise ConfigError(f"Für {label} ist kein Benutzername eingetragen.", hint=setup_hint)
    name = PASSWORD_SECRETS[source]
    if not register_loaded_secrets(secrets, [name]):  # reads the store (may raise ConfigError) and masks the value
        raise ConfigError(f"Für {label} ist kein Passwort gespeichert.", hint=setup_hint)
    password = secrets.get(name) or ""
    factory = http_factory or default_http_factory(config)
    if source is Source.EDUVIDUAL:
        return EduvidualAdapter(config.eduvidual, password, factory, retry=config.retry)
    return WebUntisAdapter(config.webuntis, password, factory)


# --------------------------------------------------------------------------- Google


def make_gateway(
    config: Config, secrets: SecretStore, authorize: Callable[[str], Any] = authorize_interactive
) -> GoogleGateway:
    """Real gateway for the configured calendar/task list. Empty ids -> ConfigError (never a fallback target);
    a missing/expired login -> GoogleAuthError from `load_credentials`."""
    g = config.google
    missing = [n for n, v in (("calendar_id", g.calendar_id), ("tasklist_id", g.tasklist_id)) if not v.strip()]
    if missing:
        raise ConfigError(
            f"Kalender '{g.calendar_name}' bzw. Aufgabenliste '{g.tasklist_title}' sind noch nicht eingerichtet "
            f"(leer in config.toml: {', '.join(missing)}).",
            hint="Bitte 'schulsync setup --google' ausführen. Der Hauptkalender wird nie verwendet.",
        )
    register_loaded_secrets(secrets, [SECRET_CLIENT, SECRET_TOKEN])
    try:
        creds = load_credentials(secrets, timeout=config.retry.read_timeout)
    except GoogleAuthError:
        # Login expired/revoked (e.g. Testing status after 7 days): open the Google login in the browser again.
        client = secrets.get(SECRET_CLIENT) or bundled_client_json()
        if not client:
            raise
        save_credentials(authorize(client), secrets)
        creds = load_credentials(secrets, timeout=config.retry.read_timeout)
    register_loaded_secrets(secrets, [SECRET_TOKEN])  # a refresh stores a new access token
    return RealGoogleGateway(
        creds,
        calendar_id=g.calendar_id,
        tasklist_id=g.tasklist_id,
        calendar_name=g.calendar_name,
        tasklist_title=g.tasklist_title,
        timeout=config.retry.read_timeout,
    )


def build_google_services(credentials: Any, config: Config) -> tuple[Any, Any]:
    """(calendar_service, tasks_service) for the setup wizard. Uses the gateway's own builder, which protects
    non-idempotent POSTs (tasklists.insert) against transport-level re-sends."""
    t = config.retry.read_timeout
    return (
        RealGoogleGateway._build("calendar", "v3", credentials, t, None),
        RealGoogleGateway._build("tasks", "v1", credentials, t, None),
    )


# --------------------------------------------------------------------------- Deps


def build_deps(
    paths: Paths,
    config: Config,
    secrets: SecretStore,
    *,
    http_factory: Callable[[], HttpClient] | None = None,
) -> Deps:
    """Production `runner.Deps`. No I/O, no secret is read before the runner calls a factory."""

    def adapter_factory(source: Source) -> Callable[[], SourceAdapter]:
        return lambda: make_adapter(source, config, secrets, http_factory=http_factory)

    enabled = {Source(name) for name in config.enabled_sources}
    return Deps(
        paths=paths,
        config=config,
        adapter_factories={s: adapter_factory(s) for s in Source if s in enabled},
        gateway_factory=lambda: make_gateway(config, secrets),
        open_report=report.open_report,
        # runner's probe (DNS + TCP 443, true as soon as ONE configured host or Google answers) with the configured hosts
        network_ready=lambda: default_network_ready(network_hosts(config), timeout=min(config.retry.connect_timeout, _PROBE_TIMEOUT_CAP)),
    )
