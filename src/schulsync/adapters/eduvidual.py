"""Eduvidual (Moodle) source adapter: assignments with due dates, read-only.

Everything here is rule based (no AI). The adapter only READS. Allowed requests:
GET pages, the login POSTs (``login/token.php`` or ``login/index.php``) and POSTs of
*read-only* Moodle functions (allow-list below; a guard refuses anything else).

Two access routes sit behind one small internal interface (``_Route``):

``webservice`` (preferred)
    ``POST login/token.php`` (service ``moodle_mobile_app``) -> token, then the REST
    protocol ``POST webservice/rest/server.php`` with ``wstoken``/``wsfunction``/
    ``moodlewsrestformat=json``.  Functions: ``core_webservice_get_site_info``,
    ``core_course_get_enrolled_courses_by_timeline_classification`` (classification
    ``inprogress``, ``limit``/``offset`` paging via ``nextoffset``) and
    ``mod_assign_get_assignments`` (id/cmid/name/duedate/intro per assignment; NOT limited
    to the Timeline, submitted assignments are included, ``duedate`` is the effective date
    of the user incl. overrides).

``web`` (fallback)
    ``login/index.php`` (form field ``logintoken``) -> ``MoodleSession`` cookie -> ``sesskey``
    from the inline ``M.cfg = {...}`` of a page -> ``POST lib/ajax/service.php?sesskey=..&info=..``
    with a JSON body ``[{"index":0,"methodname":..,"args":{..}}]``.  Only functions flagged
    ``ajax => true`` can be used this way; ``mod_assign_get_assignments`` is NOT flagged, so
    the assignments come from ``core_calendar_get_calendar_monthly_view`` (events of type
    ``due`` of module ``assign``; ``instance`` = course-module id, ``activityname`` = full name,
    ``description`` = intro, ``timestart`` = due date). No HTML detail page is scraped.

Sources for every interface assumption (Moodle source on GitHub, branches MOODLE_405_STABLE and
MOODLE_500_STABLE, read 2026-10-04; the Moodle version of Eduvidual itself is NOT known: its public
config has no ``release`` field): ``login/token.php``, ``webservice/rest/locallib.php`` (``generate_error``),
``lib/ajax/service.php``, ``lib/external/classes/external_api.php``
(``call_external_function``), ``lib/db/services.php`` and ``mod/assign/db/services.php``
(ajax flags and mobile service membership), ``mod/assign/externallib.php``,
``course/externallib.php``, ``calendar/classes/external/*``, ``calendar/lib.php``
(``calendar_get_view``), ``login/index.php``, ``lib/classes/output/requirements/
page_requirements_manager.php`` (``M.cfg``). Real unauthenticated probes of
https://www.eduvidual.at are listed in ``docs/datenzugriff_eduvidual.md``.

HONESTY: all tests use SYNTHETIC fixtures that mirror the documented structures; no
authenticated response was ever recorded. ``schulsync check`` on the user's PC is the real
verification.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, NoReturn, Protocol, Sequence
from urllib.parse import parse_qsl, urlsplit

from ..config import EduvidualConfig, RetryConfig
from ..errors import (
    AccessDeniedError,
    AuthError,
    ConfigError,
    ErrorInfo,
    IncompleteDataError,
    NotFoundError,
    SchemaError,
    SchulSyncError,
    TransientError,
)
from ..http import HttpClient, HttpResponse, RequestsHttp, classify_status, safe_url
from ..models import (
    Category,
    CategoryOutcome,
    CategoryStatus,
    FetchResult,
    ItemTime,
    Kind,
    Notice,
    ProbeReport,
    ProbeStep,
    Severity,
    Source,
    SourceItem,
    TrackedRef,
    make_uid,
)
from ..timeutil import UTC, Window, fetch_window, now_vienna, to_vienna

log = logging.getLogger("schulsync.eduvidual")

MOBILE_SERVICE = "moodle_mobile_app"
FN_SITE_INFO = "core_webservice_get_site_info"
FN_COURSES = "core_course_get_enrolled_courses_by_timeline_classification"
FN_ASSIGNMENTS = "mod_assign_get_assignments"
FN_SUBMISSION = "mod_assign_get_submission_status"  # read-only; lastattempt.submission.status / attemptnumber
FN_MONTH = "core_calendar_get_calendar_monthly_view"
FN_PUBLIC_CONFIG = "tool_mobile_get_public_config"

# Read-only guarantee: nothing outside these lists is ever sent (tests assert it as well).
READ_ONLY_WS_FUNCTIONS = frozenset({FN_SITE_INFO, FN_COURSES, FN_ASSIGNMENTS, FN_SUBMISSION})
READ_ONLY_AJAX_FUNCTIONS = frozenset({FN_COURSES, FN_MONTH})

COURSE_PAGE_SIZE = 50
MAX_COURSE_PAGES = 40
WEB_EXTRA_MONTHS = 2  # months scanned behind the latest relevant date when nothing is tracked
WEB_MAX_MONTHS = 24  # hard cap of the web route; with tracked (known open) entries the whole range is read (see months_to_scan)
SUPPORTED_MODULES = ("assign",)
_NO_DEADLINE_UNSEEN = (
    "Über die Weboberfläche sind Aufgaben ohne Abgabefrist nicht sichtbar: Der Kalender von Eduvidual kennt nur Aufgaben mit Frist. "
    "Solche Aufgaben können weder übernommen noch einzeln aufgeführt werden."
)

# Parameter names are compared in normalised form (lower case, only [a-z0-9]), so "api_key", "API-Key" and
# "apiKey" are the same. Substring matches catch access_token, id_token, client_secret, user_password, ...
_SENSITIVE_QUERY_EXACT = frozenset({"key", "pwd", "auth", "sig", "signature"})
_SENSITIVE_QUERY_PARTS = ("token", "secret", "passw", "sesskey", "apikey", "authkey", "accesskey", "privatekey")


def _is_sensitive_param(name: str) -> bool:
    norm = re.sub(r"[^a-z0-9]", "", name.lower())
    return norm in _SENSITIVE_QUERY_EXACT or any(part in norm for part in _SENSITIVE_QUERY_PARTS)


class RouteUnavailableError(SchulSyncError):
    """An access route cannot be used (service disabled, endpoint missing, functions not allowed).

    This is the ONLY condition that lets method "auto" fall back to the next route.
    """

    code = "route_unavailable"


class EduvidualError(SchulSyncError):
    """Moodle reported an error that has no more specific class."""

    code = "eduvidual_error"


class _LoginFailed(Exception):
    """Internal: credentials/login rejected. Carries the AuthError; never retried via another route."""

    def __init__(self, cause: AuthError):
        super().__init__(cause.message)
        self.cause = cause


# --------------------------------------------------------------------------- text helpers

_TAG_RE = re.compile(r"<[^>]*>")


def clean_text(value: str | None) -> str:
    """Plain single-line text: tags removed, entities resolved, whitespace normalised."""
    if not value:
        return ""
    return " ".join(html.unescape(_TAG_RE.sub("", value)).split())


def safe_http_url(url: str | None) -> str | None:
    """Return `url` if it is a plain http(s) URL without credentials or secret-like parameters."""
    if not url:
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return None
    if any(_is_sensitive_param(k) for k, _ in query):
        return None
    return url


_BLOCK_TAGS = frozenset(
    {"div", "ul", "ol", "table", "tr", "blockquote", "pre", "hr", "section", "article", "dl", "dt", "dd", "tbody", "thead"}
)
_PARA_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote", "pre"})


class _TextExtractor(HTMLParser):
    """Deterministic HTML -> plain text. Keeps text, line breaks and http(s) link URLs.

    Images, scripts, styles and attachments are not interpreted (dropped silently).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._pending_breaks = 0  # newlines waiting to be written before the next text
        self._skip = 0
        self._lists = 0  # nesting depth of ul/ol (nested lists need no blank line)
        self._in_li = False  # inside a list item: <p> etc. must not add blank lines between bullets
        self._after_marker = False  # "- " was just written; the item text must follow on the same line
        self._pre = 0  # inside <pre>: whitespace and line breaks are kept
        self._pre_fresh = False  # next data is the first of a <pre>; a leading newline is ignored (HTML rule)
        self._links: list[tuple[str | None, int]] = []

    def _newline(self, count: int) -> None:
        if self._after_marker:
            return
        self._pending_breaks = max(self._pending_breaks, count)

    def _write(self, text: str) -> None:
        if not text:
            return
        if self._pending_breaks and self._out:
            self._out.append("\n" * self._pending_breaks)
        self._pending_breaks = 0
        self._after_marker = False
        self._out.append(text)

    def _at_line_start(self) -> bool:
        return not self._out or self._out[-1].endswith("\n") or self._pending_breaks > 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br":
            self._newline(max(self._pending_breaks, 1) if self._pending_breaks else 1)
            if not self._out:
                return
            self._out.append("\n")  # an explicit <br> is always one line break
            self._pending_breaks = 0
            return
        if tag == "li":
            self._newline(1)
            self._write("- ")
            self._in_li = True
            self._after_marker = True
            return
        if tag in ("td", "th"):
            if self._out and not self._at_line_start() and not self._out[-1].endswith(" "):
                self._out.append(" ")
            return
        if tag == "a":
            href = safe_http_url(dict(attrs).get("href"))
            self._links.append((href, len(self._out)))
            return
        if tag in ("ul", "ol"):
            self._newline(1 if self._lists else 2)
            self._lists += 1
        elif tag in _PARA_TAGS:
            self._newline(1 if self._in_li else 2)
            if tag == "pre":
                self._pre += 1
                self._pre_fresh = True
        elif tag in _BLOCK_TAGS:
            self._newline(1)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a" and self._links:
            href, start = self._links.pop()
            visible = "".join(self._out[start:]).strip()
            if href and href not in visible:
                self._write(f" ({href})" if visible else href)
            return
        if tag in ("ul", "ol"):
            self._lists = max(0, self._lists - 1)
            self._in_li = False
            self._newline(1 if self._lists else 2)
        elif tag in _PARA_TAGS:
            if tag == "pre":
                self._pre = max(0, self._pre - 1)
            self._newline(1 if self._in_li else 2)
        elif tag == "li":
            self._in_li = False
            self._newline(1)
        elif tag in _BLOCK_TAGS or tag == "tr":
            self._newline(1)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._pre:
            text = data.replace("\r\n", "\n").replace("\r", "\n")
            if self._pre_fresh:
                text = text[1:] if text.startswith("\n") else text
                self._pre_fresh = False
            self._write(text)
            return
        text = re.sub(r"\s+", " ", data)
        if self._at_line_start():
            text = text.lstrip()
        self._write(text)

    def result(self) -> str:
        raw = "".join(self._out)
        lines = [ln.rstrip() for ln in raw.split("\n")]  # leading blanks are normalised on write; <pre> keeps its indentation
        out: list[str] = []
        for ln in lines:
            if ln == "" and (not out or out[-1] == ""):
                continue
            out.append(ln)
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)


def html_to_text(markup: str | None) -> str:
    """Convert Moodle HTML (assignment intro) to plain text. Empty input -> empty string."""
    if not markup:
        return ""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return parser.result()


# --------------------------------------------------------------------------- strict structure access


def _type_name(typ: type | tuple[type, ...]) -> str:
    names = {int: "Zahl", str: "Text", list: "Liste", dict: "Objekt", bool: "Wahrheitswert"}
    if isinstance(typ, tuple):
        return "/".join(names.get(t, t.__name__) for t in typ)
    return names.get(typ, typ.__name__)


def _schema_error(path: str, problem: str) -> SchemaError:
    return SchemaError(
        "Eduvidual hat eine Antwort in unerwarteter Form geliefert; die Schnittstelle hat sich vermutlich geändert.",
        hint="Bitte 'schulsync check' ausführen und die Meldung zusammen mit dem Pfad melden.",
        detail=f"{path}: {problem}",
    )


def require(obj: Any, key: str, typ: type | tuple[type, ...], path: str) -> Any:
    """Return obj[key] if present with the right type, else SchemaError naming the JSON path."""
    where = f"{path}.{key}" if path else key
    if not isinstance(obj, dict):
        raise _schema_error(path or "<root>", f"Objekt erwartet, {type(obj).__name__} erhalten")
    if key not in obj:
        raise _schema_error(where, "Feld fehlt")
    value = obj[key]
    types = typ if isinstance(typ, tuple) else (typ,)
    if isinstance(value, bool) and bool not in types:
        raise _schema_error(where, f"{_type_name(typ)} erwartet, Wahrheitswert erhalten")
    if not isinstance(value, types):
        raise _schema_error(where, f"{_type_name(typ)} erwartet, {type(value).__name__} erhalten")
    return value


def optional(obj: Mapping[str, Any], key: str, typ: type | tuple[type, ...], path: str) -> Any:
    """Like `require` but a missing/None value yields None."""
    if not isinstance(obj, dict) or obj.get(key) is None:
        return None
    return require(obj, key, typ, path)


# --------------------------------------------------------------------------- Moodle error mapping

_ACCOUNT_MESSAGES = {
    "usernotconfirmed": "Das Eduvidual-Konto ist noch nicht bestätigt.",
    "passwordisexpired": "Das Eduvidual-Kennwort ist abgelaufen und muss im Browser erneuert werden.",
    "restoredaccountresetpassword": "Das Eduvidual-Konto wurde wiederhergestellt; das Kennwort muss im Browser neu gesetzt werden.",
    "noguest": "Mit einem Gastzugang ist kein Zugriff möglich.",
    "wsaccessusersuspended": "Das Eduvidual-Konto ist gesperrt.",
    "wsaccessuserexpired": "Das Eduvidual-Konto ist abgelaufen.",
    "wsaccessuserdeleted": "Das Eduvidual-Konto existiert nicht mehr.",
    "wsaccessuserunconfirmed": "Das Eduvidual-Konto ist noch nicht bestätigt.",
    "wsaccessusernologin": "Für dieses Eduvidual-Konto ist keine Anmeldung erlaubt.",
}
_CREDENTIAL_CODES = frozenset({"invalidlogin", "wrongusernamepassword"})
_UNAVAILABLE_CODES = frozenset({"enablewsdescription", "servicenotavailable", "accessexception", "invalidtoken"})
_SESSION_CODES = frozenset({"servicerequireslogin", "invalidsesskey", "requireloginerror"})
_AUTH_HINT = (
    "Benutzername und Passwort prüfen (am besten im Browser auf eduvidual.at). Es wird nicht automatisch "
    "wiederholt, damit das Konto nicht gesperrt wird. Danach 'schulsync setup' bzw. 'schulsync check' ausführen."
)


def _safe_code(code: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(code))[:60] or "unknown"


def _raise_moodle_error(code: Any, *, context: str) -> NoReturn:
    """Translate a Moodle errorcode (token.php / REST / ajax) into our error classes.

    Server messages are deliberately NOT copied (they may echo user data); only the code is.
    `context` is "token", "ws" or "ajax".
    """
    c = _safe_code(code)
    detail = f"Moodle-Fehlercode: {c}"
    if c in _CREDENTIAL_CODES:
        raise AuthError("Eduvidual hat Benutzername oder Passwort nicht akzeptiert.", hint=_AUTH_HINT, detail=detail)
    if c in _ACCOUNT_MESSAGES:
        raise AuthError(_ACCOUNT_MESSAGES[c], hint=_AUTH_HINT, detail=detail)
    if c in _SESSION_CODES:
        raise AuthError(
            "Die Eduvidual-Sitzung ist ungültig oder abgelaufen.",
            hint="Der Lauf kann erneut versucht werden; bleibt es dabei, 'schulsync check' ausführen.",
            detail=detail,
        )
    if c == "sitemaintenance":
        raise TransientError("Eduvidual befindet sich im Wartungsmodus.", hint="Später erneut versuchen.", detail=detail)
    if c in _UNAVAILABLE_CODES:
        raise RouteUnavailableError(
            "Diese Zugangsart ist bei Eduvidual nicht freigeschaltet.",
            hint="Das Programm versucht, die jeweils andere Zugangsart zu verwenden.",
            detail=detail,
        )
    if c in ("nopermissions", "requiredcapability", "sitepolicynotagreed"):
        raise AccessDeniedError(
            "Eduvidual verweigert den Zugriff (fehlende Rechte oder nicht bestätigte Nutzungsbedingungen).",
            hint="Einmal im Browser bei Eduvidual anmelden und eventuelle Bestätigungen abschließen.",
            detail=detail,
        )
    if c in ("invalidparameter", "missingparam", "invalid_parameter_exception"):
        raise SchemaError(
            "Eduvidual hat die Anfrage als ungültig abgelehnt; die Schnittstelle hat sich vermutlich geändert.",
            hint="Bitte 'schulsync check' ausführen und die Meldung melden.",
            detail=detail,
        )
    raise EduvidualError("Eduvidual hat einen Fehler gemeldet.", hint="Später erneut versuchen.", detail=detail)


def _looks_like_login_page(text: str) -> bool:
    return 'name="logintoken"' in text or "name='logintoken'" in text


def _json_body(resp: HttpResponse, what: str) -> Any:
    """Parse a JSON body; a redirect to/answer with the login page or HTML is a failure, never 'empty'."""
    path = urlsplit(resp.url).path if resp.url else ""
    # NOTE: only the login FORM page counts; login/token.php itself lives below /login/ and is a JSON endpoint.
    if path.endswith("/login/index.php") or _looks_like_login_page(resp.text[:200_000]):
        raise AuthError(
            f"{what}: Eduvidual hat statt der Daten die Anmeldeseite geliefert (Sitzung nicht gültig).",
            hint=_AUTH_HINT,
            detail=f"HTTP {resp.status} {safe_url(resp.url) if resp.url else ''}".strip(),
        )
    if resp.text.lstrip()[:1] == "<":
        raise SchemaError(
            f"{what}: Statt Daten kam eine Webseite (Fehler- oder Wartungsseite).",
            hint="Später erneut versuchen; bleibt es dabei, 'schulsync check' ausführen.",
            detail=f"HTTP {resp.status} {safe_url(resp.url) if resp.url else ''}".strip(),
        )
    return resp.json()


def _unwrap_ajax(body: Any, what: str) -> Any:
    """lib/ajax/service(-nologin).php answers with a list: [{"error":false,"data":..}] or
    [{"error":true,"exception":{"errorcode":..}}] (verified in service.php/external_api.php and a real
    unauthenticated response)."""
    if isinstance(body, dict) and ("error" in body or "errorcode" in body):  # request-level failure
        _raise_moodle_error(body.get("errorcode", "unknown"), context="ajax")
    if not isinstance(body, list) or not body:
        raise _schema_error(what, "Liste mit einem Ergebnis erwartet")
    entry = body[0]
    flag = require(entry, "error", bool, f"{what}[0]")
    if flag:
        exc = optional(entry, "exception", dict, f"{what}[0]") or {}
        _raise_moodle_error(exc.get("errorcode", "unknown"), context="ajax")
    return require(entry, "data", (dict, list), f"{what}[0]")


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """PHP-style form encoding used by the Moodle REST protocol: a[0]=1, a[b]=2."""
    out: dict[str, str] = {}
    if isinstance(value, Mapping):
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}[{k}]" if prefix else str(k)))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    elif isinstance(value, bool):
        out[prefix] = "1" if value else "0"
    else:
        out[prefix] = str(value)
    return out


def ts_to_vienna(ts: int) -> datetime:
    """Moodle timestamps are Unix epoch seconds (UTC) -> aware Europe/Vienna datetime."""
    return to_vienna(datetime.fromtimestamp(ts, tz=UTC))


def _try_ts_to_vienna(ts: int) -> datetime | None:
    """Like ts_to_vienna, but None for a timestamp that cannot be represented (absurd server value)."""
    try:
        return ts_to_vienna(ts)
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- raw data


@dataclass(frozen=True)
class _Course:
    id: int
    fullname: str
    shortname: str | None


@dataclass(frozen=True)
class _RawAssignment:
    cmid: int
    course_id: int
    course_name: str | None
    name: str | None
    duedate: int  # 0 = no due date
    intro: str | None  # HTML; None = not provided
    intro_hidden: bool = False  # webservice route: server did not return the intro field
    note: str | None = None
    assign_id: int | None = None  # instance id (webservice route only), needed for the submission status


@dataclass
class _RouteResult:
    assignments: list[_RawAssignment] = field(default_factory=list)
    problems: list[ErrorInfo] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    scanned: str = ""  # human readable description of what was read


def _parse_course_page(payload: Any, path: str) -> tuple[list[_Course], int]:
    courses = require(payload, "courses", list, path)
    next_offset = require(payload, "nextoffset", int, path)
    out: list[_Course] = []
    for i, c in enumerate(courses):
        p = f"{path}.courses[{i}]"
        out.append(_Course(require(c, "id", int, p), require(c, "fullname", str, p), optional(c, "shortname", str, p)))
    return out, next_offset


class _Route(Protocol):
    name: str
    label: str

    def login(self) -> None: ...

    def course_page(self, limit: int, offset: int) -> Any: ...

    def assignments(self, courses: Sequence[_Course], window: Window, tracked_dates: Sequence[date]) -> _RouteResult: ...


# --------------------------------------------------------------------------- route 1: web service token


class _WebserviceRoute:
    name = "webservice"
    label = "Web-Service-Token (Moodle-App-Schnittstelle)"

    def __init__(self, http: HttpClient, base: str, username: str, password: str):
        self._http = http
        self._base = base
        self._username = username
        self._password = password
        self._token: str | None = None
        self.release: str | None = None

    # -- login ---------------------------------------------------------------------------
    def login(self) -> None:
        # Credentials only in the POST body, never in the URL (main branch even rejects GET).
        resp = self._http.request(
            "POST",
            f"{self._base}/login/token.php",
            data={"username": self._username, "password": self._password, "service": MOBILE_SERVICE},
        )
        # Moodle answers token errors with HTTP 200 + JSON (real probe); parse the body first.
        if resp.status == 404:
            raise RouteUnavailableError(
                "Der Token-Endpunkt von Eduvidual wurde nicht gefunden.", detail=f"HTTP 404 {safe_url(resp.url)}"
            )
        body: Any = None
        try:
            body = _json_body(resp, "Token-Anfrage")
        except AuthError as exc:
            # A login FORM instead of JSON says nothing about the password (it was never evaluated: proxy,
            # portal or maintenance page). Credential rejection arrives as a JSON errorcode (below), so this is
            # only "route unusable" and must not set login_error (that would block automatic runs, DESIGN 7.4).
            raise RouteUnavailableError(
                "Der Token-Endpunkt von Eduvidual hat statt einer Antwort die Anmeldeseite geliefert.", detail=exc.detail
            ) from exc
        except SchemaError as exc:
            if resp.status < 400:
                raise RouteUnavailableError(
                    "Der Token-Endpunkt von Eduvidual liefert keine JSON-Antwort.", detail=exc.detail
                ) from exc
        if isinstance(body, dict) and ("error" in body or "errorcode" in body):
            _raise_moodle_error(body.get("errorcode", "unknown"), context="token")
        if resp.status >= 400:
            try:
                classify_status(resp, "Token-Anfrage")
            except (AccessDeniedError, NotFoundError) as exc:
                raise RouteUnavailableError(
                    "Der Token-Endpunkt von Eduvidual ist nicht erreichbar (Zugriff verweigert).", detail=exc.detail
                ) from exc
        token = require(body, "token", str, "token-response")
        if not token:
            raise _schema_error("token-response.token", "leer")
        self._token = token
        try:
            info = self._call(FN_SITE_INFO, {})
        except AuthError as exc:
            # The token was issued, so the credentials are fine; REST answering with a login page means this
            # route is unusable (NOT "credentials rejected": that would block automatic runs for good).
            raise RouteUnavailableError(
                "Das REST-Protokoll von Eduvidual liefert trotz Token keine Daten.",
                hint="Es wird die Weboberfläche als Ersatz verwendet.",
                detail=exc.detail,
            ) from exc
        functions = {require(f, "name", str, f"site_info.functions[{i}]") for i, f in enumerate(require(info, "functions", list, "site_info"))}
        missing = sorted({FN_COURSES, FN_ASSIGNMENTS} - functions)
        if missing:
            raise RouteUnavailableError(
                "Eduvidual erlaubt über den Token nicht alle benötigten Funktionen.",
                hint="Es wird die Weboberfläche als Ersatz verwendet.",
                detail="nicht freigegeben: " + ", ".join(missing),
            )
        release = optional(info, "release", str, "site_info")
        self.release = release.split(" ")[0] if release else None

    # -- REST ----------------------------------------------------------------------------
    def _call(self, function: str, params: Mapping[str, Any]) -> Any:
        if function not in READ_ONLY_WS_FUNCTIONS:  # read-only guard
            raise EduvidualError("Interner Fehler: nicht erlaubte Funktion.", detail=function)
        data = {"wstoken": self._token or "", "wsfunction": function, "moodlewsrestformat": "json", **_flatten(params)}
        resp = self._http.request("POST", f"{self._base}/webservice/rest/server.php", data=data)
        if resp.status in (403, 404):
            raise RouteUnavailableError(
                "Das REST-Protokoll von Eduvidual ist nicht verfügbar.", detail=f"HTTP {resp.status} {safe_url(resp.url)}"
            )
        classify_status(resp, f"Abfrage {function}")
        body = _json_body(resp, f"Abfrage {function}")
        if isinstance(body, dict) and ("exception" in body or "errorcode" in body):
            _raise_moodle_error(body.get("errorcode", "unknown"), context="ws")
        if not isinstance(body, dict):
            raise _schema_error(function, "Objekt erwartet")
        return body

    def submission_status(self, assign_id: int) -> tuple[bool | None, int | None]:
        """(submitted?, attempt number). Unknown (None, None) if Moodle does not answer it; network errors propagate."""
        try:
            body = self._call(FN_SUBMISSION, {"assignid": assign_id})
        except TransientError:
            raise
        except SchulSyncError:
            return None, None
        last = body.get("lastattempt") if isinstance(body.get("lastattempt"), dict) else {}
        sub = last.get("submission") or last.get("teamsubmission") or {}
        status, attempt = sub.get("status"), sub.get("attemptnumber")
        return (status == "submitted" if isinstance(status, str) else None), (attempt if isinstance(attempt, int) else None)

    def course_page(self, limit: int, offset: int) -> Any:
        return self._call(FN_COURSES, {"classification": "inprogress", "limit": limit, "offset": offset})

    def assignments(self, courses: Sequence[_Course], window: Window, tracked_dates: Sequence[date]) -> _RouteResult:
        result = _RouteResult()
        ids = [c.id for c in courses]  # NEVER send an empty list: Moodle would return all courses
        body = self._call(FN_ASSIGNMENTS, {"courseids": ids})
        returned: set[int] = set()
        for ci, course in enumerate(require(body, "courses", list, FN_ASSIGNMENTS)):
            cp = f"{FN_ASSIGNMENTS}.courses[{ci}]"
            cid = require(course, "id", int, cp)
            returned.add(cid)
            cname = optional(course, "fullname", str, cp)
            for ai, a in enumerate(require(course, "assignments", list, cp)):
                ap = f"{cp}.assignments[{ai}]"
                result.assignments.append(
                    _RawAssignment(
                        cmid=require(a, "cmid", int, ap),
                        course_id=cid,
                        course_name=cname,
                        name=require(a, "name", str, ap),
                        duedate=require(a, "duedate", int, ap),
                        intro=optional(a, "intro", str, ap),
                        intro_hidden="intro" not in a,
                        assign_id=optional(a, "id", int, ap),
                    )
                )
        for wi, w in enumerate(body.get("warnings") or []):
            wp = f"{FN_ASSIGNMENTS}.warnings[{wi}]"
            item = require(w, "item", str, wp)
            if item == "course":
                result.problems.append(
                    IncompleteDataError(
                        "Ein Kurs konnte nicht gelesen werden (keine Zugriffsrechte).",
                        detail=f"Kurs-ID {w.get('itemid')}",
                    ).to_info()
                )
            else:  # module warnings are normal for activities hidden from students
                result.notices.append(
                    Notice(Severity.INFO, "module_not_readable", "Eine Aktivität ist für dich nicht lesbar (z. B. ausgeblendet) und wird übergangen.", Source.EDUVIDUAL)
                )
        for cid in sorted(set(ids) - returned):
            result.problems.append(
                IncompleteDataError("Ein Kurs fehlt in der Antwort von Eduvidual.", detail=f"Kurs-ID {cid}").to_info()
            )
        result.scanned = f"{len(courses)} Kurse über mod_assign_get_assignments"
        return result


# --------------------------------------------------------------------------- route 2: web session


class _LoginFormParser(HTMLParser):
    """Finds the `logintoken` of <form id="login"> (the real page also has a guest form)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_login = False
        self.has_login_form = False
        self.token_in_form: str | None = None
        self.token_any: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._in_login = a.get("id") == "login"
            self.has_login_form = self.has_login_form or self._in_login
        elif tag == "input" and a.get("name") == "logintoken" and a.get("value"):
            if self._in_login and self.token_in_form is None:
                self.token_in_form = a["value"]
            if self.token_any is None:
                self.token_any = a["value"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_login = False

    @property
    def token(self) -> str | None:
        return self.token_in_form or self.token_any


def parse_login_page(text: str) -> _LoginFormParser:
    parser = _LoginFormParser()
    parser.feed(text)
    parser.close()
    return parser


_MCFG_RE = re.compile(r"M\.cfg\s*=\s*")


def parse_m_cfg(text: str) -> dict[str, Any]:
    """Read the inline `M.cfg = {...};` object (contains sesskey, userId, siteId).

    Source: lib/classes/output/requirements/page_requirements_manager.php (`js_writer::set_variable('M.cfg', ...)`).
    The first occurrence that decodes to a JSON object wins (an earlier mention in a comment is skipped).
    """
    found = False
    for m in _MCFG_RE.finditer(text):
        found = True
        try:
            cfg, _ = json.JSONDecoder().raw_decode(text, m.end())
        except ValueError:
            continue
        if isinstance(cfg, dict):
            return cfg
    raise _schema_error("M.cfg", "kein gültiges JSON-Objekt" if found else "Konfigurationsobjekt der Seite nicht gefunden")


class _WebSessionRoute:
    name = "web"
    label = "Weboberfläche (Anmeldung wie im Browser)"

    def __init__(self, http: HttpClient, base: str, username: str, password: str):
        self._http = http
        self._base = base
        self._username = username
        self._password = password
        self._sesskey: str | None = None
        self._site_id = 1
        self.release = None

    def login(self) -> None:
        page = self._http.request("GET", f"{self._base}/login/index.php")
        classify_status(page, "Anmeldeseite")
        form = parse_login_page(page.text)
        if not form.token:
            raise _schema_error("login/index.php", "Feld 'logintoken' im Anmeldeformular nicht gefunden")
        resp = self._http.request(
            "POST",
            f"{self._base}/login/index.php",
            data={"anchor": "", "logintoken": form.token, "username": self._username, "password": self._password},
        )
        classify_status(resp, "Anmeldung")
        if urlsplit(resp.url).path.endswith("/login/index.php") or parse_login_page(resp.text).has_login_form:
            raise AuthError("Eduvidual hat Benutzername oder Passwort nicht akzeptiert.", hint=_AUTH_HINT, detail="Anmeldeseite erneut angezeigt")
        home = self._http.request("GET", f"{self._base}/my/")
        classify_status(home, "Startseite")
        cfg = parse_m_cfg(home.text)
        user_id = optional(cfg, "userId", int, "M.cfg")
        if not user_id:
            raise AuthError("Die Anmeldung bei Eduvidual hat keine gültige Sitzung ergeben.", hint=_AUTH_HINT, detail="M.cfg.userId = 0")
        sesskey = require(cfg, "sesskey", str, "M.cfg")
        if not sesskey:
            raise _schema_error("M.cfg.sesskey", "leer")
        self._sesskey = sesskey
        site_id = optional(cfg, "siteId", int, "M.cfg")
        self._site_id = site_id or 1

    def _ajax(self, function: str, args: Mapping[str, Any]) -> Any:
        if function not in READ_ONLY_AJAX_FUNCTIONS:  # read-only guard
            raise EduvidualError("Interner Fehler: nicht erlaubte Funktion.", detail=function)
        resp = self._http.request(
            "POST",
            f"{self._base}/lib/ajax/service.php",
            params={"sesskey": self._sesskey or "", "info": function},
            json=[{"index": 0, "methodname": function, "args": dict(args)}],
        )
        classify_status(resp, f"Abfrage {function}")
        return _unwrap_ajax(_json_body(resp, f"Abfrage {function}"), function)

    def course_page(self, limit: int, offset: int) -> Any:
        return self._ajax(FN_COURSES, {"classification": "inprogress", "limit": limit, "offset": offset})

    def assignments(self, courses: Sequence[_Course], window: Window, tracked_dates: Sequence[date]) -> _RouteResult:
        result = _RouteResult()
        by_id = {c.id: c for c in courses}
        months, truncated = months_to_scan(window, tracked_dates)
        if truncated:
            result.problems.append(
                IncompleteDataError(
                    "Der abgefragte Zeitraum wurde begrenzt; sehr weit entfernte Fristen werden nicht geprüft.",
                    detail=f"mehr als {WEB_MAX_MONTHS} Monate",
                ).to_info()
            )
        events: dict[int, dict[str, Any]] = {}
        failures: list[SchulSyncError] = []
        for year, month in months:
            try:
                data = self._ajax(
                    FN_MONTH,
                    {"year": year, "month": month, "courseid": self._site_id, "includenavigation": False, "mini": False, "day": 1},
                )
                self._collect_events(data, f"{FN_MONTH}[{year}-{month:02d}]", events)
            except (TransientError, AuthError, RouteUnavailableError):
                raise
            except SchulSyncError as exc:
                failures.append(exc)
                result.problems.append(
                    IncompleteDataError(f"Der Monat {month:02d}/{year} konnte nicht gelesen werden.", detail=exc.detail or exc.message).to_info()
                )
        if months and len(failures) == len(months):
            raise failures[-1]
        result.assignments = self._assignments_from_events(events, by_id, result)
        result.scanned = f"{len(months)} Kalendermonate ({months[0][1]:02d}/{months[0][0]} bis {months[-1][1]:02d}/{months[-1][0]})" if months else "keine Monate"
        return result

    @staticmethod
    def _collect_events(data: Any, path: str, into: dict[int, dict[str, Any]]) -> None:
        for wi, week in enumerate(require(data, "weeks", list, path)):
            for di, day in enumerate(require(week, "days", list, f"{path}.weeks[{wi}]")):
                for ei, ev in enumerate(require(day, "events", list, f"{path}.weeks[{wi}].days[{di}]")):
                    p = f"{path}.weeks[{wi}].days[{di}].events[{ei}]"
                    require(ev, "eventtype", str, p)
                    require(ev, "timestart", int, p)
                    into.setdefault(require(ev, "id", int, p), ev)  # multi-day events repeat per day

    @staticmethod
    def _assignments_from_events(events: Mapping[int, dict[str, Any]], courses: Mapping[int, _Course], result: _RouteResult) -> list[_RawAssignment]:
        """Group the 'due' events of assignments by course-module id and pick one date per assignment.

        Moodle creates one base event per assignment plus extra events for user/group overrides
        (mod/assign/lib.php, assign_update_events). User-override events carry courseid 0, so the exported
        event has NO ``course`` key (calendar/classes/external/event_exporter_base.php). The course is then
        taken from a sibling event; if none carries it, course_id=0 (= unknown, the adapter reports it).
        """
        candidates: dict[int, list[dict[str, Any]]] = {}
        for eid, ev in events.items():
            if ev.get("modulename") != "assign" or ev["eventtype"] != "due":
                continue
            p = f"{FN_MONTH}.event[{eid}]"
            candidates.setdefault(require(ev, "instance", int, p), []).append(ev)
        out: list[_RawAssignment] = []
        for cmid, evs in sorted(candidates.items()):
            chosen = evs[0]
            note = None
            if len({e["timestart"] for e in evs}) > 1:
                # Per-user/group overrides create extra 'due' events; prefer the user's own, else the earliest.
                own = [e for e in evs if e.get("userid")]
                chosen = sorted(own or evs, key=lambda e: e["timestart"])[0]
                note = "Eduvidual nennt mehrere Fristen für diese Aufgabe (Ausnahmeregelung?); verwendet wird die persönliche bzw. früheste."
                result.notices.append(
                    Notice(Severity.WARNING, "incomplete_data", f"Mehrere Fristen für „{clean_text(chosen.get('activityname'))}“ gefunden; bitte in Eduvidual prüfen.", Source.EDUVIDUAL)
                )
            p = f"{FN_MONTH}.event[{chosen['id']}]"
            cid = 0
            for e in evs:
                c = e.get("course")
                if isinstance(c, dict) and isinstance(c.get("id"), int) and not isinstance(c.get("id"), bool):
                    cid = c["id"]
                    break
            if cid and cid not in courses:
                continue  # course filtered out / not current
            out.append(
                _RawAssignment(
                    cmid=cmid,
                    course_id=cid,
                    course_name=courses[cid].fullname if cid else None,
                    name=require(chosen, "activityname", str, p),
                    duedate=chosen["timestart"],
                    intro=optional(chosen, "description", str, p),
                    note=note,
                )
            )
        return out


def months_to_scan(window: Window, tracked_dates: Sequence[date]) -> tuple[list[tuple[int, int]], bool]:
    """Calendar months the web route reads: from the day before the window to the latest relevant date plus
    WEB_EXTRA_MONTHS. The calendar is the ONLY place this route can see a deadline, so a known open assignment that was
    moved can only be found where the new date lies: as long as entries are tracked, the whole range up to WEB_MAX_MONTHS
    is read (ANFORDERUNGEN 2: "bekannte offene Einträge müssen auch außerhalb des Fensters weiter auf Änderungen geprüft
    werden"); the web route has no per-assignment lookup. Without tracked entries only the window matters.
    The second value is True if even that range does not reach a tracked date (cap)."""
    first = min([(window.start - timedelta(days=1)).date(), *tracked_dates])
    last = max([(window.end - timedelta(seconds=1)).date(), *tracked_dates]) + timedelta(days=1)
    start_idx = first.year * 12 + first.month - 1
    end_idx = last.year * 12 + last.month - 1 + WEB_EXTRA_MONTHS
    truncated = end_idx - start_idx + 1 > WEB_MAX_MONTHS
    if tracked_dates:
        end_idx = max(end_idx, start_idx + WEB_MAX_MONTHS - 1)
    if end_idx - start_idx + 1 > WEB_MAX_MONTHS:
        end_idx = start_idx + WEB_MAX_MONTHS - 1
    return [(i // 12, i % 12 + 1) for i in range(start_idx, end_idx + 1)], truncated


# --------------------------------------------------------------------------- adapter


@dataclass
class _Acquired:
    route: str
    route_label: str
    release: str | None
    unavailable: list[tuple[str, RouteUnavailableError]]
    courses_all: list[_Course]
    courses: list[_Course]
    result: _RouteResult
    problems: list[ErrorInfo]
    notices: list[Notice]
    submission_status: Callable[[int], tuple[bool | None, int | None]] | None = None  # None: route cannot tell


class EduvidualAdapter:
    """models.SourceAdapter for Eduvidual (Moodle). Reads assignments/deadlines of current courses."""

    source = Source.EDUVIDUAL

    def __init__(
        self,
        config: EduvidualConfig,
        password: str,
        http_factory: Callable[[], HttpClient] | None = None,
        retry: RetryConfig | None = None,
    ):
        self._cfg = config
        self._password = password
        self._base = config.base_url.rstrip("/")
        if http_factory is None:
            r = retry or RetryConfig()
            http_factory = lambda: RequestsHttp(r.connect_timeout, r.read_timeout)  # noqa: E731
        self._http_factory = http_factory

    def __repr__(self) -> str:  # never show the password
        return f"EduvidualAdapter(base={self._base!r}, method={self._cfg.method!r})"

    # -- route handling -------------------------------------------------------------------
    def _route_order(self) -> list[str]:
        return {"auto": ["webservice", "web"], "webservice": ["webservice"], "web": ["web"]}[self._cfg.method]

    def _new_route(self, name: str) -> _Route:
        cls = _WebserviceRoute if name == "webservice" else _WebSessionRoute
        return cls(self._http_factory(), self._base, self._cfg.username, self._password)

    def _acquire(self, window: Window, tracked_dates: Sequence[date]) -> _Acquired:
        if not self._cfg.username.strip() or not self._password:
            # Configuration problem (setup not done): no request, no credentials sent.
            raise ConfigError(
                "Für Eduvidual sind Benutzername und Passwort nicht vollständig eingerichtet.",
                hint="Bitte 'schulsync setup' ausführen.",
                detail="username oder password leer",
            )
        unavailable: list[tuple[str, RouteUnavailableError]] = []
        for name in self._route_order():
            route = self._new_route(name)
            try:
                route.login()
            except RouteUnavailableError as exc:
                log.info("Route %s nicht verfügbar: %s", name, exc.detail or exc.message)
                unavailable.append((name, exc))
                continue
            except AuthError as exc:
                # Credentials rejected: stop here, never retry them via another route (account lockout).
                raise _LoginFailed(exc) from exc
            try:
                return self._collect(route, window, tracked_dates, unavailable)
            except RouteUnavailableError as exc:
                log.info("Route %s liefert die Daten nicht: %s", name, exc.detail or exc.message)
                unavailable.append((name, exc))
        reasons = "; ".join(f"{n}: {e.detail or e.message}" for n, e in unavailable)
        raise RouteUnavailableError(
            "Eduvidual lässt keine der möglichen Zugangsarten zu.",
            hint="Bitte 'schulsync check' ausführen. Falls es dabei bleibt, ist ein Browser-Adapter der nächste Schritt.",
            detail=reasons,
        )

    def _collect(self, route: _Route, window: Window, tracked_dates: Sequence[date], unavailable: list) -> _Acquired:
        notices: list[Notice] = []
        courses_all, problems = self._list_courses(route)
        courses = self._filter_courses(courses_all, notices)
        for module in self._cfg.modules:
            if module not in SUPPORTED_MODULES:
                notices.append(
                    Notice(Severity.INFO, "unsupported_module", f"Aktivitätstyp „{module}“ wird noch nicht unterstützt und übergangen.", Source.EDUVIDUAL)
                )
        result = route.assignments(courses, window, tracked_dates) if courses else _RouteResult(scanned="keine Kurse")
        problems = [*problems, *result.problems]
        return _Acquired(route.name, route.label, getattr(route, "release", None), list(unavailable), courses_all, courses, result, problems, notices, getattr(route, "submission_status", None))

    @staticmethod
    def _list_courses(route: _Route) -> tuple[list[_Course], list[ErrorInfo]]:
        courses: list[_Course] = []
        seen: set[int] = set()
        problems: list[ErrorInfo] = []
        offset = 0
        pages = 0
        while True:
            if pages >= MAX_COURSE_PAGES:
                problems.append(IncompleteDataError("Die Kursliste ist unvollständig (zu viele Seiten).", detail=f"mehr als {MAX_COURSE_PAGES} Seiten").to_info())
                break
            try:
                page, next_offset = _parse_course_page(route.course_page(COURSE_PAGE_SIZE, offset), f"{FN_COURSES}")
            except (TransientError, AuthError, RouteUnavailableError):
                raise
            except SchulSyncError as exc:
                if not courses and pages == 0:
                    raise  # nothing read at all: failure, not "incomplete"
                problems.append(IncompleteDataError("Die Kursliste ist unvollständig (eine Seite konnte nicht gelesen werden).", detail=exc.detail or exc.message).to_info())
                break
            pages += 1
            if not page and next_offset <= offset:
                break  # exhausted (Moodle: nextoffset = offset + processed courses)
            for c in page:
                if c.id not in seen:
                    seen.add(c.id)
                    courses.append(c)
            if next_offset <= offset:
                problems.append(IncompleteDataError("Die Kursliste ist unvollständig (Seitenzähler kommt nicht weiter).", detail=f"offset {offset} -> {next_offset}").to_info())
                break
            offset = next_offset
        return courses, problems

    def _filter_courses(self, courses: list[_Course], notices: list[Notice]) -> list[_Course]:
        wanted = [w for w in (str(c).strip() for c in self._cfg.courses) if w]  # TOML may give integer course IDs
        if not wanted:
            return list(courses)
        selected: list[_Course] = []
        for w in wanted:
            hits = [c for c in courses if str(c.id) == w or clean_text(c.fullname) == clean_text(w)]
            if not hits:
                notices.append(
                    Notice(Severity.WARNING, "course_not_found", f"Der konfigurierte Kurs „{w}“ ist unter deinen aktuellen Kursen nicht vorhanden.", Source.EDUVIDUAL)
                )
            selected.extend(h for h in hits if h not in selected)
        return selected

    # -- SourceAdapter.fetch ----------------------------------------------------------------
    def fetch(self, window: Window, tracked: Sequence[TrackedRef], now: datetime) -> FetchResult:
        cat = Category.ASSIGNMENTS
        result = FetchResult(Source.EDUVIDUAL, fetched_at=now)
        mine = [t for t in tracked if t.source == Source.EDUVIDUAL and t.kind == Kind.ASSIGNMENT]
        tracked_ids = {t.source_id for t in mine}
        tracked_dates = [t.start.date() if isinstance(t.start, datetime) else t.start for t in mine]
        try:
            acq = self._acquire(window, tracked_dates)
        except _LoginFailed as lf:
            info = lf.cause.to_info()
            result.login_error = info
            result.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, error=info, detail="Anmeldung abgelehnt")
            return result
        except TransientError:
            raise
        except SchulSyncError as exc:
            result.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, error=exc.to_info())
            return result
        items, notices = self._build_items(acq, window, tracked_ids)
        result.items = items
        result.notices = [*acq.notices, *acq.result.notices, *notices]
        if acq.route != "webservice":
            # Moodle's calendar only knows assignments WITH a due date: the others are invisible on this route and can
            # never appear as `no_date`. ANFORDERUNGEN 3: a missing category must not vanish silently, so every run says it.
            result.notices.append(Notice(Severity.INFO, "no_date_unseen", _NO_DEADLINE_UNSEEN, Source.EDUVIDUAL))
        for name, exc in acq.unavailable:
            result.notices.append(
                Notice(Severity.INFO, "route_fallback", f"Zugangsart „{name}“ nicht nutzbar ({exc.detail or exc.message}); verwendet wurde „{acq.route}“.", Source.EDUVIDUAL)
            )
        status = CategoryStatus.OK if not acq.problems else CategoryStatus.INCOMPLETE
        detail = f"Zugang: {acq.route}; {len(acq.courses)} Kurse; {acq.result.scanned}; {len(acq.result.assignments)} Aufgaben gelesen"
        if acq.route != "webservice":
            detail += "; Aufgaben ohne Abgabefrist sind über diesen Zugang nicht sichtbar"
        if acq.problems:
            detail += "; unvollständig: " + "; ".join(p.message for p in acq.problems)
        result.categories[cat] = CategoryOutcome(cat, status, error=acq.problems[0] if acq.problems else None, detail=detail, item_count=len(items))
        if status == CategoryStatus.OK:
            returned = {i.source_id for i in items}
            for t in sorted(mine, key=lambda t: t.source_id):
                if t.source_id not in returned and not any(r.cmid and f"assign:{r.cmid}" == t.source_id for r in acq.result.assignments):
                    result.notices.append(
                        Notice(Severity.INFO, "tracked_not_found", "Ein bekannter Eintrag wurde diesmal nicht gefunden (kein Beleg für eine Absage; Eintrag bleibt unverändert).", Source.EDUVIDUAL, uid=t.uid)
                    )
        return result

    def _build_items(self, acq: _Acquired, window: Window, tracked_ids: set[str]) -> tuple[list[SourceItem], list[Notice]]:
        items: list[SourceItem] = []
        notices: list[Notice] = []
        for a in sorted(acq.result.assignments, key=lambda a: (a.duedate, a.cmid)):
            sid = f"assign:{a.cmid}"
            uid = make_uid(Source.EDUVIDUAL, Kind.ASSIGNMENT, sid)
            notes: list[str] = []
            title = clean_text(a.name)
            if not title:
                title = f"Aufgabe {a.cmid}"
                notes.append("Titel in Eduvidual nicht lesbar; Platzhalter verwendet.")
                notices.append(Notice(Severity.WARNING, "incomplete_data", f"Titel der Aufgabe {a.cmid} ist nicht lesbar.", Source.EDUVIDUAL, uid=uid))
            if not a.duedate:
                notices.append(
                    Notice(Severity.WARNING, "no_date", f"Aufgabe „{title}“ hat keine Abgabefrist und wird übersprungen.", Source.EDUVIDUAL, uid=uid, title=title)
                )
                continue
            due = _try_ts_to_vienna(a.duedate)
            if due is None:
                notices.append(Notice(Severity.WARNING, "incomplete_data", f"Abgabefrist der Aufgabe „{title}“ ist nicht lesbar; übersprungen.", Source.EDUVIDUAL, uid=uid, title=title))
                continue
            if not (window.contains(due) or sid in tracked_ids):
                continue
            if a.course_id == 0 and self._cfg.courses:
                notices.append(
                    Notice(Severity.WARNING, "incomplete_data", f"Der Kurs der Aufgabe „{title}“ ist über die Weboberfläche nicht bestimmbar; wegen der Kursauswahl übersprungen.", Source.EDUVIDUAL, uid=uid, title=title)
                )
                continue
            course = clean_text(a.course_name) or None
            if course is None:
                notes.append("Kursname in Eduvidual nicht lesbar.")
                notices.append(Notice(Severity.WARNING, "incomplete_data", f"Kursname der Aufgabe „{title}“ ist nicht lesbar.", Source.EDUVIDUAL, uid=uid, title=title))
            if a.intro_hidden:
                notes.append("Beschreibung wird von Eduvidual derzeit nicht angezeigt.")
                notices.append(Notice(Severity.INFO, "incomplete_data", f"Beschreibung der Aufgabe „{title}“ wird von Eduvidual nicht geliefert.", Source.EDUVIDUAL, uid=uid, title=title))
            if a.note:
                notes.append(a.note)
            submitted, attempt = (
                acq.submission_status(a.assign_id) if acq.submission_status and a.assign_id is not None else (None, None)
            )
            items.append(
                SourceItem(
                    source=Source.EDUVIDUAL,
                    source_id=sid,
                    kind=Kind.ASSIGNMENT,
                    title=title,
                    when=ItemTime(start=due),
                    course=course,
                    description=html_to_text(a.intro) or None,
                    url=safe_http_url(f"{self._base}/mod/assign/view.php?id={a.cmid}"),
                    notes=tuple(notes),
                    submitted=submitted,
                    attempt=attempt,
                )
            )
        return items, notices

    # -- SourceAdapter.check ------------------------------------------------------------------
    def check(self) -> ProbeReport:
        report = ProbeReport(Source.EDUVIDUAL)
        steps = report.steps
        cat = Category.ASSIGNMENTS
        try:
            cfg = self._public_config()
        except SchulSyncError as exc:
            steps.append(ProbeStep("Eduvidual erreichbar", False, _explain(exc)))
            report.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, error=exc.to_info())
            return report
        site = clean_text(str(cfg.get("sitename") or "")) or "Eduvidual"
        maintenance = bool(cfg.get("maintenanceenabled"))
        steps.append(
            ProbeStep("Eduvidual erreichbar", not maintenance, f"{site} antwortet" + (" – aber im Wartungsmodus." if maintenance else "."))
        )
        ws_on = bool(cfg.get("enablewebservices")) and bool(cfg.get("enablemobilewebservice"))
        method = self._cfg.method
        steps.append(
            ProbeStep(
                "Web-Service verfügbar",
                ws_on or method != "webservice",
                (
                    "Webservices und Mobile-Dienst sind aktiviert."
                    if ws_on
                    else "Webservices bzw. Mobile-Dienst sind abgeschaltet"
                    + (" – es wird die Weboberfläche verwendet." if method != "webservice" else ", die Einstellung method=\"webservice\" kann so nicht funktionieren.")
                ),
            )
        )
        window = fetch_window(now_vienna())
        try:
            acq = self._acquire(window, [])
        except _LoginFailed as lf:
            steps.append(ProbeStep("Anmeldung", False, f"{lf.cause.message} {lf.cause.hint or ''}".strip()))
            report.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, error=lf.cause.to_info())
            return report
        except SchulSyncError as exc:
            steps.append(ProbeStep("Anmeldung", False, _explain(exc)))
            report.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, error=exc.to_info())
            return report
        steps.append(ProbeStep("Anmeldung", True, f"Anmeldung erfolgreich. Zugang: {acq.route_label}." + (f" Moodle {acq.release}." if acq.release else "")))
        for name, exc in acq.unavailable:
            steps.append(ProbeStep(f"Zugangsart „{name}“", True, f"nicht nutzbar, Ersatz verwendet: {exc.detail or exc.message}"))
        filt = f" ({len(acq.courses)} nach Kursauswahl)" if self._cfg.courses else ""
        steps.append(ProbeStep("Kurse lesen", True, f"{len(acq.courses_all)} aktuelle Kurse gefunden{filt}."))
        raw = acq.result.assignments
        with_date = [a for a in raw if a.duedate]
        in_window = [a for a in with_date if (d := _try_ts_to_vienna(a.duedate)) is not None and window.contains(d)]
        no_date_note = "" if acq.route == "webservice" else " (Aufgaben ohne Frist sind über diesen Zugang nicht sichtbar)"
        problems = acq.problems
        steps.append(
            ProbeStep(
                "Aufgaben lesen",
                not problems,
                f"{len(raw)} Aufgaben gelesen, davon {len(with_date)} mit Frist, {len(in_window)} im Prüffenster{no_date_note}."
                + ("" if not problems else " Unvollständig: " + "; ".join(p.message for p in problems)),
            )
        )
        report.categories[cat] = CategoryOutcome(
            cat,
            CategoryStatus.OK if not problems else CategoryStatus.INCOMPLETE,
            error=problems[0] if problems else None,
            detail=f"Zugang: {acq.route}; {acq.result.scanned}",
            item_count=len(in_window),
        )
        return report

    def _public_config(self) -> dict[str, Any]:
        """Unauthenticated reachability probe: tool_mobile_get_public_config via service-nologin.php (GET, verified)."""
        http = self._http_factory()
        resp = http.request(
            "GET",
            f"{self._base}/lib/ajax/service-nologin.php",
            params={"args": json.dumps([{"index": 0, "methodname": FN_PUBLIC_CONFIG, "args": {}}])},
        )
        classify_status(resp, "Eduvidual-Startabfrage")
        data = _unwrap_ajax(_json_body(resp, "Eduvidual-Startabfrage"), FN_PUBLIC_CONFIG)
        if not isinstance(data, dict):
            raise _schema_error(FN_PUBLIC_CONFIG, "Objekt erwartet")
        return data


def _explain(exc: SchulSyncError) -> str:
    return f"{exc.message} {exc.hint or ''}".strip()
