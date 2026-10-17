"""Real Google gateway (Calendar v3 + Tasks v1) and the translation of API/transport errors.

Sources (all read on 2026-10-04, nothing here is guessed):
- Calendar error reasons and retry advice (403 rateLimitExceeded / userRateLimitExceeded / quotaExceeded
  are rate limits and are retried with backoff exactly like 429; 404, 409 duplicate, 410, 5xx):
  https://developers.google.com/workspace/calendar/api/guides/errors
- calendarList.get (accessRole, primary, scope calendar.calendarlist.readonly):
  https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/get
- events.list (privateExtendedProperty "name=value", showDeleted, nextPageToken, maxResults<=2500):
  https://developers.google.com/workspace/calendar/api/v3/reference/events/list
- events.insert (own id: lowercase a-v and 0-9, 5..1024 chars, unique per calendar):
  https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
- tasks.list (maxResults<=100, showCompleted/showHidden/showDeleted; "showHidden must also be True to show
  tasks completed in first party clients"): https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/list
- tasklists.get / list: https://developers.google.com/workspace/tasks/reference/rest/v1/tasklists/list
- Retry helper of the client library (what `execute(num_retries=n)` retries internally): installed
  `googleapiclient.http._retry_request` / `_should_retry_response` (5xx, 429, 403 rateLimit reasons,
  SSL/timeout/connection errors, with sleeps) - therefore inserts always use `num_retries=0`.

Network access: the services are built with `static_discovery=True` (discovery documents ship with the
installed google-api-python-client, nothing is downloaded) and an `httplib2.Http(timeout=...)` wrapped by
`google_auth_httplib2.AuthorizedHttp`, which adds/refreshes the OAuth token.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from typing import Any, Callable

import google_auth_httplib2
import httplib2
from google.auth import exceptions as auth_exceptions
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import (
    AccessDeniedError,
    BadRequestError,
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
from .google_targets import TargetsUnavailableError
from .google_types import PROP_UID, TargetCheck
from .models import Target

WRITE_ROLES = ("owner", "writer")
_MAX_PAGES = 1000  # hard stop against a server that never ends paging

# 403 reasons that are really rate limits (Calendar errors page: "functionally similar [to 429]"). Lower-case,
# also the Google-RPC style spellings that newer endpoints use in `error.status` / `error.details[].reason`.
_RATE_REASONS = frozenset(
    {
        "ratelimitexceeded",
        "userratelimitexceeded",
        "quotaexceeded",
        "rate_limit_exceeded",
        "resource_exhausted",
    }
)

REAUTH_HINT = (
    "Bitte 'schulsync setup' erneut ausführen, um Google neu zu erlauben. Mögliche Gründe: Die Google-App hat den "
    "Status 'Testing' (Anmeldung läuft nach 7 Tagen ab), die Anmeldung wurde 6 Monate nicht genutzt, der Zugriff "
    "wurde im Google-Konto widerrufen, oder (nur bei Gmail-Berechtigungen) das Passwort wurde geändert. "
    "Unbegrenzte Gültigkeit kann nicht garantiert werden."
)


class _SingleShotConnection:
    """Connection proxy for POST: httplib2 re-sends a request when the answer is lost (BadStatusLine/RemoteDisconnected
    on the first try) or when sending raises an HTTPException (httplib2.Http._conn_request, 0.32). tasks.insert and
    tasklists.insert have no idempotency key, so that would create duplicates. Both failures are turned into a
    ConnectionError, which httplib2 does not retry (and which the translation maps to OutcomeUnknownError)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def request(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._conn.request(*args, **kwargs)
        except http.client.HTTPException as exc:
            self._conn.close()
            raise ConnectionError(type(exc).__name__) from None

    def getresponse(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._conn.getresponse(*args, **kwargs)
        except (http.client.BadStatusLine, http.client.ResponseNotReady) as exc:
            self._conn.close()
            raise ConnectionError(type(exc).__name__) from None


class _NoResendHttp(httplib2.Http):
    """httplib2.Http that never re-sends a POST on its own (see _SingleShotConnection)."""

    def _conn_request(self, conn: Any, request_uri: str, method: str, body: Any, headers: Any) -> Any:
        if method == "POST":
            conn = _SingleShotConnection(conn)
        return super()._conn_request(conn, request_uri, method, body, headers)


class RequestRejectedError(BadRequestError):
    """400/other 4xx: Google rejects the request itself (our body is wrong). Permanent, never retried.

    Subclass of the shared errors.BadRequestError so the engine can catch the shared class.
    """


# --------------------------------------------------------------------------------------------------------------
# error translation


def _reasons(exc: HttpError) -> set[str]:
    """All machine reasons Google put into the error body (lower-case): error.errors[].reason,
    error.details[].reason and error.status. Tolerates every malformed shape."""
    found: set[str] = set()
    try:
        data = json.loads(exc.content.decode("utf-8"))
        err = data["error"] if isinstance(data, dict) else {}
        for item in err.get("errors") or []:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                found.add(item["reason"].lower())
        for item in err.get("details") or []:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                found.add(item["reason"].lower())
        if isinstance(err.get("status"), str):
            found.add(err["status"].lower())
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        pass
    return found


def _http_detail(exc: HttpError, reasons: set[str]) -> str:
    # status + reasons only; the body text is not copied (it can echo request data)
    return f"HTTP {exc.status_code}" + (f" ({', '.join(sorted(reasons))})" if reasons else "")


def translate_refresh_error(exc: auth_exceptions.RefreshError, op: str) -> SchulSyncError:
    """RefreshError -> GoogleAuthError (invalid_grant & co.), except a retryable token-endpoint failure."""
    # google-auth drops `retryable` for non-JSON error bodies (reauth.refresh_grant), and a non-JSON answer from
    # the token endpoint (HTML error page, proxy, gateway) is not an OAuth verdict on the token anyway: only a
    # structured OAuth error ({"error": ...}, args[1]) means "the grant is bad".
    structured = len(exc.args) > 1 and isinstance(exc.args[1], dict)
    if getattr(exc, "retryable", False) or not structured:
        return NetworkError(
            f"Google-Anmeldeserver vorübergehend nicht erreichbar ({op}).",
            hint="Wird beim nächsten Versuch automatisch wiederholt.",
            detail=type(exc).__name__,
        )
    code = ""
    if len(exc.args) > 1 and isinstance(exc.args[1], dict):
        raw = exc.args[1].get("error")
        code = raw if isinstance(raw, str) else ""
    if code == "invalid_client":
        return GoogleAuthError(
            "Google lehnt die gespeicherten App-Zugangsdaten (OAuth-Client) ab.",
            hint="Bitte 'schulsync setup' erneut ausführen und die OAuth-Client-Datei prüfen.",
            detail=code,
        )
    return GoogleAuthError(
        "Die Google-Anmeldung ist abgelaufen oder wurde widerrufen.",
        hint=REAUTH_HINT,
        detail=code or type(exc).__name__,  # never the exception text: it may quote token endpoint data
    )


def translate_http_error(exc: BaseException, *, op: str, write: bool = False) -> SchulSyncError:
    """Map an API/transport exception to a SchulSyncError (returned, not raised).

    `op` is a short German description ("Termin anlegen"), used in the user-visible message.
    `write=True` means a NON-idempotent create (insert): whenever the request may have reached Google but the
    answer is missing or a server error (5xx, timeout, connection reset, unreadable answer) the result is
    `OutcomeUnknownError`, so the caller reconciles by project id before creating again. Errors that prove that
    nothing was processed (DNS failure, refused connection, certificate error, 401/403/429/4xx) stay normal.
    Messages never contain URLs, headers or tokens.
    """
    if isinstance(exc, HttpError):
        status = int(exc.status_code)
        reasons = _reasons(exc)
        detail = _http_detail(exc, reasons)
        if status == 401:
            return GoogleAuthError(
                f"Google hat die Anmeldung abgelehnt ({op}).", hint=REAUTH_HINT, detail=detail
            )
        if status == 403:
            if reasons & _RATE_REASONS:
                return TransientError(
                    f"Google-Limit vorübergehend erreicht ({op}).",
                    hint="Wird später automatisch erneut versucht.",
                    detail=detail,
                )
            return AccessDeniedError(
                f"Google verweigert den Zugriff ({op}).",
                hint="Rechte des Kontos auf Kalender/Aufgabenliste prüfen und prüfen, ob die Google Calendar API "
                "und die Google Tasks API im Cloud-Projekt aktiviert sind; sonst 'schulsync setup' erneut ausführen.",
                detail=detail,
            )
        if status in (404, 410):
            return NotFoundError(f"Nicht gefunden bei Google ({op}).", detail=detail)
        if status in (409, 412):
            return ConflictError(f"Eintrag existiert bereits bzw. wurde geändert ({op}).", detail=detail)
        if status in (408, 429):
            return TransientError(
                f"Google ist vorübergehend nicht erreichbar oder drosselt ({op}).",
                hint="Wird später automatisch erneut versucht.",
                detail=detail,
            )
        if status >= 500:
            if write:
                return OutcomeUnknownError(
                    f"Unklar, ob Google den Eintrag angelegt hat ({op}, Serverfehler).",
                    hint="Es wird vor einem erneuten Anlegen abgeglichen, damit keine Dubletten entstehen.",
                    detail=detail,
                )
            return TransientError(
                f"Google meldet einen Serverfehler ({op}).",
                hint="Wird später automatisch erneut versucht.",
                detail=detail,
            )
        return RequestRejectedError(
            f"Google hat die Anfrage abgelehnt ({op}).",
            hint="Das ist ein Programmfehler oder eine geänderte Schnittstelle; bitte melden.",
            detail=detail,
        )

    if isinstance(exc, auth_exceptions.RefreshError):
        return translate_refresh_error(exc, op)

    # TransportError comes from the token refresh (before the API request was sent, or after a 401 answer).
    if isinstance(exc, auth_exceptions.TransportError):
        return NetworkError(
            f"Keine Verbindung zu Google ({op}).",
            hint="Internetverbindung prüfen; es wird später automatisch erneut versucht.",
            detail=type(exc).__name__,
        )

    # Failures that prove the request was never delivered (name resolution, refused connection, TLS trust).
    never_sent = (
        isinstance(exc, (httplib2.ServerNotFoundError, socket.gaierror, ConnectionRefusedError))
        or isinstance(exc, ssl.SSLCertVerificationError)
    )
    transport = (OSError, httplib2.HttpLib2Error, http.client.HTTPException, ValueError)
    if isinstance(exc, transport):  # OSError covers socket.timeout/TimeoutError/ssl.SSLError/ConnectionError
        detail = type(exc).__name__
        if write and not never_sent:
            return OutcomeUnknownError(
                f"Unklar, ob Google den Eintrag angelegt hat ({op}, Verbindung unterbrochen).",
                hint="Es wird vor einem erneuten Anlegen abgeglichen, damit keine Dubletten entstehen.",
                detail=detail,
            )
        timeout = isinstance(exc, TimeoutError)
        return NetworkError(
            f"Zeitüberschreitung bei Google ({op})." if timeout else f"Keine Verbindung zu Google ({op}).",
            hint="Internetverbindung prüfen; es wird später automatisch erneut versucht.",
            detail=detail,
        )
    return SchulSyncError(f"Unerwarteter Fehler beim Zugriff auf Google ({op}).", detail=type(exc).__name__)


# Everything an `execute()` call can raise that we know how to translate. Anything else (TypeError, ...) is a
# bug and propagates untouched. ValueError covers json.JSONDecodeError for an unreadable 200 answer.
_TRANSLATABLE = (
    HttpError,
    auth_exceptions.GoogleAuthError,
    OSError,
    httplib2.HttpLib2Error,
    http.client.HTTPException,
    ValueError,
)


def call_api(
    request: Any, *, op: str, write: bool = False, retries: int = 0, missing_ok: bool = False
) -> Any:
    """Execute a googleapiclient request and raise translated errors.

    `retries` goes to `execute(num_retries=...)` (library-internal retry with sleeps) - only for reads and
    idempotent writes. `missing_ok=True` returns None for 404/410 instead of raising.
    """
    try:
        return request.execute(num_retries=retries)
    except HttpError as exc:
        if missing_ok and int(exc.status_code) in (404, 410):
            return None
        raise translate_http_error(exc, op=op, write=write) from None
    except _TRANSLATABLE as exc:
        raise translate_http_error(exc, op=op, write=write) from None


def paged(make_request: Callable[[str | None], Any], *, op: str, retries: int, key: str = "items") -> list[dict[str, Any]]:
    """Collect all pages. A repeated page token or a page flood raises IncompleteDataError (never a short list)."""
    out: list[dict[str, Any]] = []
    token: str | None = None
    seen: set[str] = set()
    for _ in range(_MAX_PAGES):
        page = call_api(make_request(token), op=op, retries=retries)
        out.extend(page.get(key) or [])
        token = page.get("nextPageToken")
        if not token:
            return out
        if token in seen:
            break
        seen.add(token)
    raise IncompleteDataError(f"Google lieferte die Liste nicht vollständig ({op}).", detail="page token loop")


# --------------------------------------------------------------------------------------------------------------
# gateway


class RealGoogleGateway:
    """`google_types.GoogleGateway` on the official client libraries.

    `credentials`: google.oauth2.credentials.Credentials. `timeout`: socket timeout in seconds.
    `read_retries`: library-internal retries for reads and idempotent writes (patch, delete); inserts never retry.
    Tests inject `http` (an httplib2-like transport, e.g. HttpMockSequence; still wrapped by AuthorizedHttp) or
    prebuilt `calendar_service` / `tasks_service`.
    """

    def __init__(
        self,
        credentials: Any,
        *,
        calendar_id: str,
        tasklist_id: str,
        calendar_name: str = "Schule",
        tasklist_title: str = "Schule",
        timeout: float = 30.0,
        read_retries: int = 2,
        http: Any = None,
        calendar_service: Any = None,
        tasks_service: Any = None,
    ) -> None:
        self.calendar_id = calendar_id
        self.tasklist_id = tasklist_id
        self.calendar_name = calendar_name
        self.tasklist_title = tasklist_title
        self.read_retries = read_retries
        # httplib2.Http is not thread-safe: each service gets its own transport when we build it.
        self._calendar = calendar_service or self._build("calendar", "v3", credentials, timeout, http)
        self._tasks = tasks_service or self._build("tasks", "v1", credentials, timeout, http)

    @staticmethod
    def _build(name: str, version: str, credentials: Any, timeout: float, http: Any) -> Any:
        inner = http if http is not None else _NoResendHttp(timeout=timeout)
        return build(
            name,
            version,
            http=google_auth_httplib2.AuthorizedHttp(credentials, http=inner),
            static_discovery=True,
            cache_discovery=False,
        )

    # ---- targets

    def _require_event_target(self) -> None:
        """Never the main calendar and never an empty id (ANFORDERUNGEN 4); also guards writes without check_targets."""
        if not self.calendar_id or self.calendar_id.strip().lower() == "primary":
            raise TargetUnavailableError(
                f"Kalender '{self.calendar_name}' ist nicht eingerichtet.",
                hint="Bitte 'schulsync setup' ausführen. Der Hauptkalender wird nie verwendet.",
            )

    def check_targets(self) -> TargetCheck:
        """Calendar first, then the task list; stops at the first problem. The error names the failed target
        (`TargetsUnavailableError`) so the engine defers only the affected writes (ANFORDERUNGEN 4)."""
        try:
            self._require_event_target()
        except TargetUnavailableError as exc:
            raise TargetsUnavailableError({Target.CALENDAR: exc.to_info()}) from None
        if not self.tasklist_id:
            info = TargetUnavailableError(
                f"Aufgabenliste '{self.tasklist_title}' ist nicht eingerichtet.", hint="Bitte 'schulsync setup' ausführen."
            ).to_info()
            raise TargetsUnavailableError({Target.TASKS: info})  # the calendar was not looked at: it is not verified either
        try:
            entry = self._calendar_entry()
        except TargetUnavailableError as exc:
            raise TargetsUnavailableError({Target.CALENDAR: exc.to_info()}) from None
        try:
            tasklist = self._tasklist_entry()
        except TargetUnavailableError as exc:
            raise TargetsUnavailableError({Target.TASKS: exc.to_info()}, verified=(Target.CALENDAR,)) from None
        return TargetCheck(
            calendar_id=self.calendar_id,
            calendar_summary=str(entry.get("summary", "")),
            tasklist_id=self.tasklist_id,
            tasklist_title=str(tasklist.get("title", "")),
        )

    def _calendar_entry(self) -> dict[str, Any]:
        """The calendarList entry of the configured calendar, if it is usable (exists, not the main calendar, writable)."""
        cal_name = self.calendar_name
        try:
            entry = call_api(
                self._calendar.calendarList().get(calendarId=self.calendar_id),
                op="Kalender prüfen",
                retries=self.read_retries,
            )
        except NotFoundError as exc:
            raise TargetUnavailableError(
                f"Kalender '{cal_name}' nicht gefunden.",
                hint=f"Kalender '{cal_name}' in Google Kalender anlegen/wiederherstellen und "
                "'schulsync setup' erneut ausführen. Es wird nie auf einen anderen Kalender ausgewichen.",
                detail=exc.detail,
            ) from None
        if entry.get("primary") is True:
            raise TargetUnavailableError(
                f"Der gespeicherte Kalender ist der Hauptkalender des Kontos ('{entry.get('summary', '')}'). "
                "Der Hauptkalender wird nie verwendet.",
                hint=f"Einen eigenen Kalender '{cal_name}' anlegen und 'schulsync setup' erneut ausführen.",
            )
        if entry.get("deleted") is True:
            raise TargetUnavailableError(
                f"Kalender '{cal_name}' wurde gelöscht.",
                hint=f"Kalender '{cal_name}' neu anlegen und 'schulsync setup' erneut ausführen.",
            )
        if entry.get("accessRole") not in WRITE_ROLES:
            raise TargetUnavailableError(
                f"Keine Schreibrechte auf den Kalender '{cal_name}' (Rolle: {entry.get('accessRole', 'unbekannt')}).",
                hint="Das Konto braucht die Rolle 'Besitzer' oder 'Bearbeiter' auf diesem Kalender.",
            )
        return entry

    def _tasklist_entry(self) -> dict[str, Any]:
        reinstall = "Bitte in Google Kalender/Aufgaben prüfen und 'schulsync setup' erneut ausführen."
        try:
            return call_api(
                self._tasks.tasklists().get(tasklist=self.tasklist_id),
                op="Aufgabenliste prüfen",
                retries=self.read_retries,
            )
        except NotFoundError as exc:
            raise TargetUnavailableError(
                f"Aufgabenliste '{self.tasklist_title}' nicht gefunden.",
                hint=f"{reinstall} Es wird nie auf eine andere Liste ausgewichen.",
                detail=exc.detail,
            ) from None

    # ---- calendar

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        return call_api(
            self._calendar.events().get(calendarId=self.calendar_id, eventId=event_id),
            op="Termin lesen",
            retries=self.read_retries,
            missing_ok=True,
        )

    def insert_event(self, body: dict[str, Any]) -> dict[str, Any]:
        self._require_event_target()
        if not body.get("id"):
            raise ValueError("insert_event needs a deterministic 'id' in the body")
        return call_api(
            self._calendar.events().insert(calendarId=self.calendar_id, body=body),
            op="Termin anlegen",
            write=True,
            retries=0,
        )

    def patch_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_event_target()
        return call_api(
            self._calendar.events().patch(calendarId=self.calendar_id, eventId=event_id, body=body),
            op="Termin ändern",
            retries=self.read_retries,
        )

    def find_events_by_uid(self, uid: str) -> list[dict[str, Any]]:
        def request(token: str | None) -> Any:
            return self._calendar.events().list(
                calendarId=self.calendar_id,
                privateExtendedProperty=f"{PROP_UID}={uid}",
                showDeleted=True,
                maxResults=250,
                pageToken=token,
            )

        events = paged(request, op="Termine suchen", retries=self.read_retries)
        # Server filter is authoritative. Defensive client check: a live event must carry our uid; a cancelled
        # one may come back stripped of extendedProperties (Google only guarantees id+status for deleted events).
        return [
            e
            for e in events
            if (e.get("extendedProperties") or {}).get("private", {}).get(PROP_UID) == uid
            or (e.get("status") == "cancelled" and "extendedProperties" not in e)
        ]

    # ---- tasks

    def list_tasks(self) -> list[dict[str, Any]]:
        def request(token: str | None) -> Any:
            return self._tasks.tasks().list(
                tasklist=self.tasklist_id,
                showCompleted=True,
                showHidden=True,
                showDeleted=True,
                maxResults=100,
                pageToken=token,
            )

        return paged(request, op="Aufgaben lesen", retries=self.read_retries)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return call_api(
            self._tasks.tasks().get(tasklist=self.tasklist_id, task=task_id),
            op="Aufgabe lesen",
            retries=self.read_retries,
            missing_ok=True,
        )

    def insert_task(self, body: dict[str, Any]) -> dict[str, Any]:
        return call_api(
            self._tasks.tasks().insert(tasklist=self.tasklist_id, body=body),
            op="Aufgabe anlegen",
            write=True,
            retries=0,
        )

    def patch_task(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return call_api(
            self._tasks.tasks().patch(tasklist=self.tasklist_id, task=task_id, body=body),
            op="Aufgabe ändern",
            retries=self.read_retries,
        )

    def delete_task(self, task_id: str) -> None:
        call_api(
            self._tasks.tasks().delete(tasklist=self.tasklist_id, task=task_id),
            op="Aufgabe löschen",
            retries=self.read_retries,
            missing_ok=True,
        )
