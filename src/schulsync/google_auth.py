"""Google OAuth for an installed (desktop) app: one interactive authorization, then silent token renewal.

Sources:
- OAuth for installed apps / loopback redirect: https://developers.google.com/identity/protocols/oauth2/native-app
- Refresh token lifetime ("Testing" status -> 7 days, 6 months unused, revoked access, password change only
  with Gmail scopes, 100-token limit per client): https://developers.google.com/identity/protocols/oauth2#expiration
- `InstalledAppFlow.run_local_server(..., **kwargs)` forwards `access_type`/`prompt` to `authorization_url`
  (checked in the installed google-auth-oauthlib 1.5 source); `google.auth.exceptions.RefreshError` is raised by
  `Credentials.refresh` for token endpoint errors such as `invalid_grant`.
- calendarList.list/tasklists.list/insert: see google_client.py for the URLs.

Secrets (client JSON, token JSON) live in a `SecretStore` (DPAPI in production). Nothing here logs or prints them.
The authorization URL printed by the console flow is not a secret (client id + state), the tokens are never printed.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime
from typing import Any, Protocol

import google_auth_httplib2
import httplib2
from google.auth import exceptions as auth_exceptions
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from oauthlib.oauth2 import OAuth2Error

from .errors import (
    ConfigError,
    GoogleAuthError,
    NetworkError,
    TargetUnavailableError,
)
from .google_client import REAUTH_HINT, WRITE_ROLES, translate_refresh_error, call_api, paged, translate_http_error

# Minimal scope set (least privilege):
# - calendar.events: read/write events only; no access to calendar settings/sharing.
# - calendar.calendarlist.readonly: ONLY to find the calendar "Schule" (and verify role/primary) during setup and
#   check_targets (calendarList.get accepts it, see the calendarList.get reference).
# - tasks: Tasks API has no narrower read-write scope (tasks / tasks.readonly only).
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/tasks",
)

SECRET_CLIENT = "google_client"  # OAuth client JSON downloaded from the Google Cloud console
SECRET_TOKEN = "google_token"  # credentials_to_json() of the authorized user

SETUP_HINT = "Bitte 'schulsync setup' ausführen, um Google zu erlauben."


class SecretStore(Protocol):
    """Local view of secrets_store (get/set/delete by name)."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


# --------------------------------------------------------------------------------------------------------------
# credentials <-> JSON


def credentials_to_json(creds: Credentials) -> str:
    """Serialize token, refresh_token, token_uri, client_id, client_secret, scopes, expiry (naive UTC, 'Z')."""
    data: dict[str, Any] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
        "expiry": creds.expiry.isoformat() + "Z" if creds.expiry else None,
    }
    return json.dumps(data)


def credentials_from_json(text: str) -> Credentials:
    try:
        data = json.loads(text)
        expiry = data.get("expiry")
        # A token that can never be refreshed (null/empty fields) is a permanent defect: reject it here, so it
        # asks for re-authorization instead of surfacing later as a retryable RefreshError.
        for key in ("refresh_token", "token_uri", "client_id", "client_secret"):
            if not isinstance(data[key], str) or not data[key]:
                raise ValueError(key)
        return Credentials(
            token=data.get("token"),
            refresh_token=data["refresh_token"],
            token_uri=data["token_uri"],
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            scopes=data.get("scopes") or None,
            expiry=datetime.fromisoformat(expiry.rstrip("Z")) if expiry else None,
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        # no exception text: it could quote parts of the stored token JSON
        raise GoogleAuthError(
            "Die gespeicherte Google-Anmeldung ist unlesbar oder unvollständig.", hint=SETUP_HINT
        ) from None


def save_credentials(creds: Credentials, secrets: SecretStore) -> None:
    secrets.set(SECRET_TOKEN, credentials_to_json(creds))


# --------------------------------------------------------------------------------------------------------------
# interactive authorization (setup)

_PROMPT = (
    "Öffne diese Adresse im Browser und erlaube SchulSync den Zugriff auf Kalender und Aufgaben:\n{url}"
)
_SUCCESS = "Anmeldung abgeschlossen. Du kannst dieses Fenster schließen und zu SchulSync zurückkehren."


def bundled_client_json() -> str | None:
    """OAuth client JSON that sits in the project folder (private repo): `google_client.json` or the
    `client_secret*.json` downloaded from Google. None if absent. Only meaningful for an editable install."""
    root = Path(__file__).resolve().parents[2]
    for path in [root / "google_client.json", *sorted(root.glob("client_secret*.json"))]:
        try:
            return path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
    return None


def authorize_interactive(
    client_config_json: str, *, open_browser: bool = True, port: int = 0, timeout_seconds: int | None = 600
) -> Credentials:
    """One-time browser authorization via loopback redirect. `port=0` picks a free port.

    `access_type=offline` makes Google issue a refresh token; `prompt=consent` forces a refresh token even when
    the app was authorized before. The OAuth client must be of type "Desktop app" (JSON key `installed`).
    """
    try:
        config = json.loads(client_config_json)
        if not isinstance(config, dict) or not isinstance(config.get("installed"), dict):
            raise ValueError("not an installed-app client")
        flow = InstalledAppFlow.from_client_config(config, scopes=list(SCOPES))
    except ValueError:
        raise ConfigError(
            "Die OAuth-Client-Datei ist ungültig.",
            hint="Im Google-Cloud-Projekt einen OAuth-Client vom Typ 'Desktop-App' erstellen und die JSON-Datei "
            "herunterladen.",
        ) from None
    try:
        creds = flow.run_local_server(
            host="localhost",
            port=port,
            open_browser=open_browser,
            authorization_prompt_message=_PROMPT,
            success_message=_SUCCESS,
            timeout_seconds=timeout_seconds,
            access_type="offline",
            prompt="consent",
        )
    except WSGITimeoutError:
        raise GoogleAuthError(
            "Zeitüberschreitung: Es wurde keine Anmeldung bei Google abgeschlossen.", hint=SETUP_HINT
        ) from None
    except OAuth2Error as exc:  # e.g. the user pressed "Abbrechen" (access_denied), invalid_grant on exchange
        raise GoogleAuthError(
            "Google hat die Anmeldung nicht bestätigt.", hint=SETUP_HINT, detail=type(exc).__name__
        ) from None
    except Warning:  # oauthlib raises Warning("Scope has changed ...") when the user unticks permissions
        raise GoogleAuthError(
            "Nicht alle benötigten Berechtigungen wurden erteilt.",
            hint="Beim Anmelden bitte alle Kästchen (Kalender und Aufgaben) aktivieren.",
        ) from None
    except OSError as exc:  # port in use, token endpoint unreachable (requests errors are OSError subclasses)
        raise NetworkError(
            "Die Anmeldung bei Google konnte nicht durchgeführt werden (Verbindung oder lokaler Port).",
            hint="Internetverbindung prüfen und erneut versuchen.",
            detail=type(exc).__name__,
        ) from None
    granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
    if not set(SCOPES) <= granted:
        raise GoogleAuthError(
            "Nicht alle benötigten Berechtigungen wurden erteilt.",
            hint="Beim Anmelden bitte alle Kästchen (Kalender und Aufgaben) aktivieren.",
        )
    if not creds.refresh_token:
        raise GoogleAuthError(
            "Google hat keinen dauerhaften Zugriff (Refresh-Token) ausgestellt.",
            hint="Zugriff von SchulSync im Google-Konto unter 'Sicherheit' entfernen und 'schulsync setup' wiederholen.",
        )
    return creds


# --------------------------------------------------------------------------------------------------------------
# load / refresh (normal runs)


def load_credentials(secrets: SecretStore, *, http: Any = None, timeout: float = 30.0) -> Credentials:
    """Load the stored token; refresh it when expired and persist the refreshed token via `secrets.set`.

    `http`: optional httplib2-like transport for the refresh call (tests). Errors:
    missing/corrupt secret -> GoogleAuthError (setup hint); RefreshError (invalid_grant, revoked, expired
    "Testing" token) -> GoogleAuthError with re-authorization hint; network problems -> NetworkError.
    The client JSON secret is not needed here: the token JSON carries client id/secret for the refresh.
    """
    text = secrets.get(SECRET_TOKEN)
    if not text:
        raise GoogleAuthError("Google ist noch nicht eingerichtet.", hint=SETUP_HINT)
    creds = credentials_from_json(text)
    if creds.valid:
        return creds
    try:
        creds.refresh(google_auth_httplib2.Request(http if http is not None else httplib2.Http(timeout=timeout)))
    except auth_exceptions.RefreshError as exc:
        raise translate_refresh_error(exc, "Anmeldung erneuern") from None
    except (auth_exceptions.TransportError, OSError, httplib2.HttpLib2Error) as exc:
        raise translate_http_error(exc, op="Anmeldung erneuern") from None
    save_credentials(creds, secrets)
    return creds


# --------------------------------------------------------------------------------------------------------------
# setup helpers (take googleapiclient service objects)


def _shown_name(entry: dict[str, Any]) -> str:
    # summaryOverride is what the user sees in their own calendar list when they renamed the entry
    return str(entry.get("summaryOverride") or entry.get("summary") or "").strip()


def find_calendar_by_name(service: Any, name: str) -> dict[str, Any]:
    """The one calendar the user sees as `name`. Never the primary calendar; needs owner/writer role.

    0 matches -> TargetUnavailableError (create the calendar first); >1 -> ConfigError listing the candidates.
    """
    entries = paged(
        lambda token: service.calendarList().list(showHidden=True, maxResults=250, pageToken=token),
        op="Kalender suchen",
        retries=2,
    )
    matches = [e for e in entries if _shown_name(e) == name.strip()]
    if not matches:
        raise TargetUnavailableError(
            f"Kein Kalender mit dem Namen '{name}' gefunden.",
            hint=f"Bitte zuerst in Google Kalender einen eigenen Kalender '{name}' anlegen und dann "
            "'schulsync setup' erneut ausführen.",
        )
    if len(matches) > 1:
        listing = "; ".join(
            f"'{_shown_name(e)}' (ID {e.get('id')}, Rolle {e.get('accessRole', '?')}"
            f"{', Hauptkalender' if e.get('primary') else ''})"
            for e in matches
        )
        raise ConfigError(
            f"Mehrere Kalender heißen '{name}': {listing}.",
            hint="Kalender umbenennen, sodass genau einer '{0}' heißt, und 'schulsync setup' wiederholen.".format(name),
        )
    cal = matches[0]
    if cal.get("primary") is True:
        raise TargetUnavailableError(
            f"Der Kalender '{name}' ist der Hauptkalender des Kontos und wird nie verwendet.",
            hint=f"Einen eigenen, separaten Kalender '{name}' anlegen.",
        )
    if cal.get("accessRole") not in WRITE_ROLES:
        raise TargetUnavailableError(
            f"Keine Schreibrechte auf den Kalender '{name}' (Rolle: {cal.get('accessRole', 'unbekannt')}).",
            hint="Das Konto braucht die Rolle 'Besitzer' oder 'Bearbeiter' auf diesem Kalender.",
        )
    return cal


class TasklistResult(tuple):
    """`(id, created)` for unpacking, plus `.note`: German info when several lists had the same title."""

    def __new__(cls, id: str, created: bool, note: str | None = None) -> "TasklistResult":
        self = super().__new__(cls, (id, created))
        self.note = note
        return self

    @property
    def id(self) -> str:
        return self[0]

    @property
    def created(self) -> bool:
        return self[1]


def find_or_create_tasklist(service: Any, title: str) -> TasklistResult:
    """Exact-title match over all pages; created via tasklists.insert (no retry) when none exists.

    Several equal titles: the list with the smallest id is chosen (deterministic, independent of API order) and
    `.note` says so.
    """
    lists = paged(
        lambda token: service.tasklists().list(maxResults=100, pageToken=token),
        op="Aufgabenlisten lesen",
        retries=2,
    )
    matches = sorted((t for t in lists if t.get("title") == title and t.get("id")), key=lambda t: t["id"])
    if matches:
        note = None
        if len(matches) > 1:
            note = (
                f"{len(matches)} Aufgabenlisten heißen '{title}'; es wird die mit der kleinsten ID verwendet "
                f"({matches[0]['id']}). Überzählige Listen bitte in Google Tasks entfernen."
            )
        return TasklistResult(matches[0]["id"], False, note)
    created = call_api(service.tasklists().insert(body={"title": title}), op="Aufgabenliste anlegen", write=True)
    return TasklistResult(created["id"], True)
