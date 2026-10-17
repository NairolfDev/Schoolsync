"""WebUntis-Quelladapter (HTBLA Grieskirchen): Hausaufgaben, Prüfungen/Schularbeiten, Unterrichtsentfälle.

Rein lesend, ohne KI, mit dem persönlichen Schülerzugang (Benutzername/Passwort). Es gibt KEINE offizielle,
öffentlich dokumentierte Schüler-API für Hausaufgaben und Prüfungen; die Wege unten stützen sich auf Quellcode und
Dokumentation von Drittprojekten (am 2026-10-04 im Quelltext gelesen). Reale, nicht angemeldete Befunde stehen unten
und in docs/DESIGN.md Abschnitt 1. Ob die Wege mit einem echten Schülerkonto funktionieren, zeigt erst
`schulsync check` auf dem Zielsystem. Alles hier ist bisher NUR gegen synthetische Fixtures getestet
(tests/fixtures/webuntis, "synthetic, not recorded").

Quellen (jede Schnittstellenannahme hängt an mindestens einer davon; "unverified" = nur Annahme)
  [PYW]  python-webuntis, https://github.com/python-webuntis/python-webuntis (webuntis/session.py, utils/remote.py,
         errors.py, objects.py) und https://python-webuntis.readthedocs.io/
         - JSON-RPC `POST /WebUntis/jsonrpc.do?school=<school>`, Methode `authenticate` mit {user,password,client}
           -> {sessionId, personType, personId, klasseId}; Fehlercodes in utils/remote.py: -32601 (Methode fehlt),
           -8504 (falsche Zugangsdaten), -8520 (nicht angemeldet), -7004/-8507 (Datum nicht erlaubt);
           `getTimetable` (Elementtypen klasse 1, teacher 2, subject 3, room 4, student 5), `getSchoolyears`.
         - objects.py PeriodObject.code: nicht gesetzt (regulär), "cancelled" (Entfall) oder "irregular"
           (Supplierung/nicht geplantes Ereignis); Raum-/Lehrkraftwechsel tragen `orgid` an `ro`/`te`.
  [SCH]  SchoolUtils/WebUntis, https://github.com/SchoolUtils/WebUntis (src/base.ts, src/types.ts)
         - Datum als Ganzzahl yyyymmdd (20130314), Uhrzeit als Ganzzahl Hmm (800 = 08:00, 1350 = 13:50).
         - getTimetable: params {"options": {"element": {"id","type"}, "startDate", "endDate", "showLsText",
           "showSubstText", "showInfo", "klasseFields", ...}}; Stunde: id, date, startTime, endTime, kl/te/su/ro
           (Listen mit id/name/longname/orgid), code ('cancelled' | 'irregular'), info, substText.
         - `GET /WebUntis/api/homeworks/lessons?startDate&endDate` (nur Cookies JSESSIONID + schoolname) ->
           {"data": {"homeworks": [{id, lessonId, date, dueDate, text, remark, completed, attachments}]}}.
         - `GET /WebUntis/api/exams?startDate&endDate&klasseId=-1&withGrades=false` -> {"data": {"exams": [
           {id, examType, name, examDate, startTime, endTime, subject, teachers[], rooms[], text, ...}]}}.
         - Sitzungs-Cookies: `JSESSIONID` (aus der Anmeldeantwort) und `schoolname` = "_" + base64(school).
  [STG]  stundenglas, https://github.com/137-Trimethylxanthin/stundenglas (SCOUT.md, core/src/untis.rs), gegen
         WebUntis 2027.2.1 beschrieben (die Schule läuft laut DESIGN §1 auf 2027.2.4)
         - Formular-Anmeldung `POST /WebUntis/j_spring_security_check` (school, j_username, j_password, token="");
           Erfolg: 302 nach .../index.do; 302 woandershin = abgelehnt; {"state": "TOKEN_REQUIRED"} = Zwei-Faktor.
           Voraussetzung laut SCOUT.md: der Mandant bietet das Passwortformular an (hideWuLogin=false).
         - `GET /WebUntis/api/token/new` (Cookie) -> Bearer-JWT als Text (>= 40 Zeichen); jeder
           `/api/rest/view/v*`-Pfad antwortet OHNE Bearer mit 404 (kein 401/403!); `GET .../v1/app/data` ->
           user.person.id, currentSchoolYear.dateRange, permissions[].
         - `GET /api/rest/view/v1/timetable/entries?start=YYYY-MM-DD&end=YYYY-MM-DD&format=3&resourceType=STUDENT
           &resources=<personId>&periodTypes=&timetableType=MY_TIMETABLE` -> {"days":[{"gridEntries":[{ids[],
           duration{start,end} (naive lokale Wanduhrzeit), type NORMAL_TEACHING_PERIOD|EVENT, status
           REGULAR|CHANGED|CANCELLED, position1..7[{current{type,shortName,longName},removed}], lessonInfo,
           substitutionText}]}], "errors":[]}. SCOUT.md: ein ganzes Schuljahr in einer Anfrage möglich; format=3
           trennt Entfall (CANCELLED) von Supplierung (CHANGED); der Weg über JSON-RPC unterscheidet laut SCOUT.md
           eine Supplierung teils nicht von einer regulären Stunde (beide ohne code) - für Entfälle egal.
         - Klassische Endpunkte (Cookie, Datum yyyymmdd): homeworks/lessons mit `data.lessons[]` (Fach je Stunden-id),
           exams braucht `klasseId=-1` und `studentId=<personId>`. core/src/untis.rs liest Prüfungen in ZWEI
           beobachteten Formen (examDate+startTime/endTime ODER ISO startDateTime/endDateTime) und beide
           Umschläge ({"data":{...}} oder bare {"exams":[...]}/{"homeworks":[...]}); beides wird hier akzeptiert.
  Reale, nicht angemeldete Probe (2026-10-04, minimal, ohne Anmeldeversuch): `POST jsonrpc.do?school=htbla-grieskirchen`
  (Methode getLatestImportTime) -> HTTP 200 mit {"error":{"code":-8520,"message":"not authenticated"}};
  `GET /api/exams` und `/api/homeworks/lessons` ohne Sitzung -> 302 auf /WebUntis/index.do; Startseite
  `GET /WebUntis/?school=htbla-grieskirchen` -> 200, setzt `JSESSIONID`, `schoolname`, `Tenant-Id`, enthält
  `hideWuLogin:false`, `csrfHeader:"X-CSRF-TOKEN"`. NICHT geprüft (Zugangsdaten nötig): alles hinter der Anmeldung.

Wege je Kategorie (Reihenfolge = Vorzug; `check()` meldet den tatsächlich genutzten)
  Anmeldung:      A) JSON-RPC `authenticate` [PYW,SCH] -> B) Formular `j_spring_security_check` [STG].
                  B nur, wenn jsonrpc.do NICHT als JSON-RPC antwortet (404/405/Weiterleitung/kein JSON/-32601); jede
                  inhaltliche Ablehnung beendet die Anmeldung. Es gibt höchstens EINEN Anmeldeversuch pro Adapter
                  (kein Sperrrisiko, kein Wiederholen). Das Formular sendet keinen CSRF-Header (stundenglas tut das
                  auch); ob dieser Mandant ihn verlangt, ist unverified (Folge: HTTP 403 -> Anmeldefehler im Bericht).
  Hausaufgaben:   REST `/api/homeworks/lessons` (Cookie) [SCH,STG]. Kein zweiter Weg belegt.
  Prüfungen:      REST `/api/exams` mit klasseId=-1 (Cookie) [SCH,STG]. (REST-View `/rest/view/v1/exams` wird NICHT
                  genutzt: Antwortform nicht belegt.)
  Unterrichtsentfall: 1) JSON-RPC `getTimetable` für das eigene Schülerelement [PYW,SCH]; 2) bei dauerhaft nicht
                  nutzbarem Weg 1 (JSON-RPC-Fehler oder 404; NICHT bei abgelaufener Sitzung/Datum): REST
                  `timetable/entries` mit Bearer [STG].

Fachregeln
  - Entfall = Stunde mit code "cancelled" (JSON-RPC) bzw. status CANCELLED und type NORMAL_TEACHING_PERIOD (REST).
    "irregular"/CHANGED (Supplierung, Verlegung, Raum-/Lehrkraftwechsel) und reguläre Stunden werden NIE übernommen.
    Eine Verlegung erscheint (nach [PYW] "irregular" = nicht geplante Stunde) am alten Termin als Entfall: das ist
    ein echter Unterrichtsentfall dieses Zeitfensters und wird so gemeldet (unverified: wie Untis Verlegungen codiert).
  - Stabile IDs: Hausaufgabe = `id`; Prüfung = `id`; Entfall = "<Stunden-id>-<yyyymmdd>-<HHMM>" (JSON-RPC `id` bzw.
    erste REST-`ids`). Eine verschobene Stunde ist damit ein neuer Entfall; Hausaufgaben-/Prüfungs-IDs bleiben bei
    Terminänderung gleich. unverified: ob Untis Stunden-IDs nach einem Neuimport stabil hält und ob JSON-RPC-`id`
    und REST-`ids` dieselben Zahlen sind (bei einem Routenwechsel könnte ein Entfall einmal doppelt erscheinen).
  - `withdrawn`: nur wenn eine verfolgte Entfallstunde (gleiche Kennung) im abgerufenen Stundenplan ausdrücklich
    ohne Entfall steht (JSON-RPC: Stunde vorhanden mit code fehlend/"irregular"; REST: status REGULAR/CHANGED).
    Unbekannte Statuswerte, Abwesenheit, leere Listen und Fehler sind nie ein Beleg. Für Hausaufgaben/Prüfungen gibt
    es kein Rücknahmesignal -> nie `withdrawn`.
  - Abgerufener Bereich je Kategorie = Vereinigung aus Prüffenster und den Datumsbereichen der verfolgten Einträge
    (überlappende/angrenzende Bereiche werden verschmolzen), zurückgegeben werden nur Einträge im Fenster ODER mit
    verfolgter Kennung. Hausaufgaben/Prüfungen: gibt es verfolgte Einträge dieser Art, reicht der Bereich zusätzlich
    bis zum Schuljahresende (31.08.), damit eine verschobene Frist auffällt. Prüfungen/Hausaufgaben ohne Datum werden
    nicht geliefert (Notice `no_date`); Notices entstehen nur für gelieferte Einträge. Lange Bereiche werden in Stücke von höchstens 62 Tagen geteilt (defensiv; ein Limit ist in den
    Quellen nicht belegt). JSON-RPC-Zeiträume werden auf die Schuljahre aus `getSchoolyears` zugeschnitten
    (-7004/-8507 = Datum nicht erlaubt [PYW]). Eine Paginierung der Endpunkte ist nirgends belegt; abgeschnittene
    Antworten lassen sich daher nicht erkennen (unverified). Fehler im REST-Stundenplan (`errors` nicht leer) und
    -7004/-8507 gelten als unvollständig (INCOMPLETE).
  - Hausaufgaben-Titel: erste nichtleere Zeile des Hausaufgabentexts (max. 100 Zeichen, an Wortgrenze gekürzt + "…");
    Fach = Fach der zugehörigen Stunde (`lessons[].subject`); der VOLLE Text steht in der Beschreibung. Wird die erste
    Zeile gekürzt: Hinweis + Notice `incomplete_data`. Ohne Text: Titel "Hausaufgabe <Fach>" + Warnung. Fehlt
    `dueDate`: Eintrag NICHT liefern, Notice `no_date` (das Zuweisungsdatum `date` ist keine Frist).
  - Hausaufgaben werden ab 28 Tage VOR dem Bereichsbeginn abgerufen und clientseitig nach dueDate gefiltert, weil
    unverified ist, ob der Server nach Zuweisungs- oder Fälligkeitsdatum filtert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Sequence

from ..errors import (
    AccessDeniedError,
    AuthError,
    ErrorInfo,
    IncompleteDataError,
    NotFoundError,
    SchemaError,
    SchulSyncError,
    TransientError,
)
from ..config import WebUntisConfig
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
from ..timeutil import VIENNA, Window, local_midnight, make_local, now_vienna, to_vienna

CLIENT_ID = "SchulSync"
MAX_SPAN_DAYS = 62
# unverified: ob /api/homeworks/lessons nach Zuweisungs- oder Fälligkeitsdatum filtert [SCH nennt es nicht];
# deshalb wird defensiv weiter zurück gelesen und clientseitig nach dueDate gefiltert.
HOMEWORK_LOOKBACK_DAYS = 28
TITLE_MAX = 100
STUDENT_ELEMENT_TYPE = 5  # [PYW] element_type_table: 5 = student

# Read-only guarantee (ANFORDERUNGEN 3: school applications are only read): nothing but these JSON-RPC methods is ever sent;
# `_rpc` refuses everything else before a request is made. `authenticate` is the login itself (it creates the session, like
# the login form); the other two only read. The GET routes cannot change anything and the only POSTs are these methods and the
# login form.
READ_ONLY_RPC_METHODS = frozenset({"authenticate", "getSchoolyears", "getTimetable"})

_NO_PERIOD = "kein Zeitraum innerhalb der Schuljahre"  # every requested range lies outside the school years of WebUntis

ROUTE_LOGIN_RPC = "JSON-RPC authenticate"
ROUTE_LOGIN_FORM = "Webformular (j_spring_security_check)"
ROUTE_HOMEWORK = "REST /api/homeworks/lessons"
ROUTE_EXAMS = "REST /api/exams"
ROUTE_TT_RPC = "JSON-RPC getTimetable"
ROUTE_TT_REST = "REST timetable/entries (Bearer)"

_CATEGORY_NAMES = {"homework": Category.HOMEWORK, "exams": Category.EXAMS, "cancellations": Category.CANCELLATIONS}
_CATEGORY_LABEL = {
    Category.HOMEWORK: "Hausaufgaben",
    Category.EXAMS: "Prüfungen/Schularbeiten",
    Category.CANCELLATIONS: "Unterrichtsentfälle",
}

# --------------------------------------------------------------------------- interne Fehler


class SessionExpiredError(SchulSyncError):
    """Die Sitzung ist während des Abrufs abgelaufen oder die Anmeldung wurde nicht akzeptiert."""

    code = "session_expired"


class ForbiddenMethodError(SchulSyncError):
    """Internal error: something asked for a JSON-RPC method outside `READ_ONLY_RPC_METHODS`. Nothing was sent."""

    code = "internal"


class _RpcUnavailable(Exception):
    """jsonrpc.do antwortet nicht als JSON-RPC (404/405/kein JSON/-32601): nur dann ist die Formular-Anmeldung erlaubt."""


class _RpcError(SchulSyncError):
    """JSON-RPC-Fehlerobjekt mit Code (der Servertext wird bewusst nicht übernommen)."""

    code = "rpc_error"

    def __init__(self, rpc_code: int | None, what: str, reason: str | None = None):
        # `reason`: verständlicher deutscher Grund ohne Code (z. B. fehlende Personenkennung); sonst der Fehlercode
        self.reason = reason or (f"Fehlercode {rpc_code}" if rpc_code is not None else "Fehler ohne Code")
        super().__init__(
            f"{what}: WebUntis hat die Anfrage abgelehnt ({self.reason}).",
            detail=f"JSON-RPC: {self.reason}",
        )
        self.rpc_code = rpc_code


# --------------------------------------------------------------------------- strikte Strukturprüfung


def _schema(path: str, expected: str) -> SchemaError:
    return SchemaError(
        f"Unerwartete Antwortstruktur von WebUntis ({expected}).",
        hint="WebUntis hat sein Datenformat möglicherweise geändert. Bitte 'schulsync check' ausführen.",
        detail=f"JSON-Pfad: {path}",
    )


_KIND_NAMES: dict[type, str] = {dict: "Objekt", list: "Liste", str: "Text", int: "Ganzzahl"}


def _is(value: Any, kind: type) -> bool:
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, kind)


def require(obj: Any, key: str, kind: type, path: str) -> Any:
    """Pflichtfeld `key` mit Typ `kind`; sonst SchemaError mit JSON-Pfad (ohne Datenwerte)."""
    if not isinstance(obj, dict):
        raise _schema(path, "Objekt erwartet")
    if key not in obj or obj[key] is None:
        raise _schema(f"{path}.{key}", f"Feld fehlt, erwartet: {_KIND_NAMES[kind]}")
    if not _is(obj[key], kind):
        raise _schema(f"{path}.{key}", f"falscher Typ, erwartet: {_KIND_NAMES[kind]}")
    return obj[key]


def expect_field_somewhere(entries: Sequence[Any], keys: Sequence[str], path: str) -> None:
    """Structure-change heuristic for an OPTIONAL date field (ANFORDERUNGEN 3: a changed page structure must raise a visible
    error, not turn into "no entry has a date"). A single entry may legitimately lack its date (that stays the notice
    `no_date`), but if an answer with several entries has NONE of them carrying the field (or its documented alternative)
    at all, the platform renamed or dropped it: `SchemaError` with the JSON path, no data values. An undated entry is
    delivered with the key present and 0, as in all known answers.

    ponytail: a lone entry cannot be told apart from a legitimately undated one; it stays a `no_date` warning."""
    if len(entries) >= 2 and all(isinstance(e, dict) and not any(k in e for k in keys) for e in entries):
        raise _schema(f"{path}[*].{keys[0]}", "Feld fehlt in allen Einträgen")


def optional(obj: Any, key: str, kind: type, path: str, default: Any = None) -> Any:
    """Optionales Feld: fehlt/None -> default; falscher Typ -> SchemaError."""
    if not isinstance(obj, dict):
        raise _schema(path, "Objekt erwartet")
    value = obj.get(key)
    if value is None:
        return default
    if not _is(value, kind):
        raise _schema(f"{path}.{key}", f"falscher Typ, erwartet: {_KIND_NAMES[kind]}")
    return value


# --------------------------------------------------------------------------- Text und Zeit


_KNOWN_TAGS = (
    "p|div|br|li|ul|ol|tr|td|th|table|thead|tbody|h[1-6]|b|i|u|s|em|strong|a|span|font|pre|code|blockquote|hr|sub|sup"
    "|script|style"
)


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    _SKIP = {"script", "style"}  # Inhalt wird nie übernommen (und nirgends ausgeführt)

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    @staticmethod
    def _known(tag: str) -> bool:
        return re.fullmatch(_KNOWN_TAGS, tag) is not None

    def _keep_verbatim(self, raw: str | None) -> None:
        # Kein HTML-Tag (z. B. "List<String>" in gemischtem Text): unverändert übernehmen
        if not self._skip:
            self.parts.append(raw or "")

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if not self._known(tag):
            self._keep_verbatim(self.get_starttag_text())
        elif tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:  # <br/> zählt als EIN Zeilenumbruch
        if not self._known(tag):
            self._keep_verbatim(self.get_starttag_text())
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._known(tag):
            self._keep_verbatim(f"</{tag}>")
        elif tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK and tag not in ("br", "li"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


# Nur echte HTML-Tags (bekannter Name, Attribute nur als name=wert) lösen die HTML-Umwandlung aus. Klartext wie
# "List<String>" oder "a<b und c>d" (Hausaufgaben einer HTL) bleibt unverändert (ANFORDERUNGEN 5).
_TAG_RE = re.compile(
    r"</?(?:" + _KNOWN_TAGS + r")(?:\s+[A-Za-z_:][-\w:.]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'>]+))*\s*/?>",
    re.IGNORECASE,
)


def clean_text(value: str | None) -> str:
    """Whitespace normalisieren; HTML (nur wenn echte Tags vorkommen) deterministisch in Klartext wandeln.

    Zeilenumbrüche bleiben erhalten (mehrere Leerzeilen -> eine), sonst wird nichts gedeutet oder gekürzt.
    """
    if not value:
        return ""
    s = value
    if _TAG_RE.search(s):
        parser = _TextExtractor()
        parser.feed(s)
        parser.close()
        s = "".join(parser.parts)
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    out: list[str] = []
    for line in (" ".join(raw.split()) for raw in s.split("\n")):
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def parse_ymd(value: Any, path: str) -> date:
    """Ganzzahl yyyymmdd [PYW] -> date."""
    if not _is(value, int):
        raise _schema(path, "Datum als Ganzzahl yyyymmdd erwartet")
    s = f"{value:08d}"
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        raise _schema(path, "ungültiges Datum") from None


def parse_hmm(value: Any, path: str) -> time:
    """Ganzzahl Hmm [PYW] (800 -> 08:00, 1350 -> 13:50) -> time."""
    if not _is(value, int) or value < 0:
        raise _schema(path, "Uhrzeit als Ganzzahl Hmm erwartet")
    hour, minute = divmod(value, 100)
    if hour > 23 or minute > 59:
        raise _schema(path, "ungültige Uhrzeit")
    return time(hour, minute)


def _ymd(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def _hmm_key(dt: datetime) -> str:
    return f"{dt.hour:02d}{dt.minute:02d}"


def _de_date(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


# --------------------------------------------------------------------------- Datumsbereiche


def _window_days(window: Window) -> tuple[date, date]:
    """Erster und letzter (inklusiver) Kalendertag des halboffenen Fensters."""
    return to_vienna(window.start).date(), to_vienna(window.end - timedelta(microseconds=1)).date()


def _ref_days(ref: TrackedRef) -> tuple[date, date]:
    first = to_vienna(ref.start).date() if isinstance(ref.start, datetime) else ref.start
    last = to_vienna(ref.end).date() if ref.end is not None else first
    return first, max(first, last)


def merge_ranges(ranges: Sequence[tuple[date, date]]) -> list[tuple[date, date]]:
    """Vereinigung; überlappende und unmittelbar angrenzende Bereiche werden verschmolzen."""
    merged: list[tuple[date, date]] = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def chunk_range(first: date, last: date, span: int = MAX_SPAN_DAYS) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cur = first
    while cur <= last:
        end = min(last, cur + timedelta(days=span - 1))
        out.append((cur, end))
        cur = end + timedelta(days=1)
    return out


def _clip(ranges: Sequence[tuple[date, date]], years: Sequence[tuple[date, date]]) -> list[tuple[date, date]]:
    pieces = [
        (max(a, ys), min(b, ye)) for a, b in ranges for ys, ye in years if max(a, ys) <= min(b, ye)
    ]
    return merge_ranges(pieces)


# --------------------------------------------------------------------------- Normalisierte Zwischenform


@dataclass(frozen=True)
class _Lesson:
    """Eine Stunde des eigenen Stundenplans (routenunabhängig)."""

    key: str  # "<Stunden-id>-<yyyymmdd>-<Hmm>"
    start: datetime
    end: datetime
    status: str  # cancelled | regular | irregular | unknown
    subject: str | None
    teacher: str | None
    room: str | None
    texts: tuple[str, ...] = ()
    merged: int = 1


@dataclass
class _CatResult:
    items: list[SourceItem] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    withdrawn: dict[str, str] = field(default_factory=dict)
    detail: str = ""
    status: CategoryStatus = CategoryStatus.OK


def _names(elements: Any, path: str) -> str | None:
    """JSON-RPC-Elementliste (su/te/ro) -> "Langname bzw. Name, ..." oder None."""
    if elements is None:
        return None
    if not isinstance(elements, list):
        raise _schema(path, "Liste erwartet")
    names: list[str] = []
    for i, el in enumerate(elements):
        longname = clean_text(optional(el, "longname", str, f"{path}[{i}]", ""))
        name = clean_text(optional(el, "name", str, f"{path}[{i}]", ""))
        if longname or name:
            names.append(longname or name)
    return ", ".join(names) or None


def lessons_from_rpc(result: Any, path: str = "$.result") -> list[_Lesson]:
    """[PYW,SCH] getTimetable-Ergebnis -> Stunden. Datum/Zeit werden strikt geprüft."""
    if not isinstance(result, list):
        raise _schema(path, "Liste erwartet")
    out: list[_Lesson] = []
    for i, raw in enumerate(result):
        p = f"{path}[{i}]"
        lesson_id = require(raw, "id", int, p)
        day = parse_ymd(require(raw, "date", int, p), f"{p}.date")
        t0 = parse_hmm(require(raw, "startTime", int, p), f"{p}.startTime")
        t1 = parse_hmm(require(raw, "endTime", int, p), f"{p}.endTime")
        code = optional(raw, "code", str, p, "")
        status = {"": "regular", "cancelled": "cancelled", "irregular": "irregular"}.get(code, "unknown")
        texts = []
        for label, key in (("Vertretungstext", "substText"), ("Info", "info")):
            text = clean_text(optional(raw, key, str, p, ""))
            if text:
                texts.append(f"{label}: {text}")
        out.append(
            _Lesson(
                key=f"{lesson_id}-{_ymd(day)}-{t0.hour:02d}{t0.minute:02d}",
                start=make_local(day, t0),
                end=make_local(day, t1),
                status=status,
                subject=_names(raw.get("su"), f"{p}.su"),
                teacher=_names(raw.get("te"), f"{p}.te"),
                room=_names(raw.get("ro"), f"{p}.ro"),
                texts=tuple(texts),
            )
        )
    return out


def _local_iso(value: str, path: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise _schema(path, "ISO-Zeitangabe erwartet") from None
    return to_vienna(dt) if dt.tzinfo else dt.replace(tzinfo=VIENNA)  # [STG] lokale Wanduhrzeit ohne Offset


def lessons_from_rest(payload: Any) -> list[_Lesson]:
    """[STG] timetable/entries -> Stunden. Nur type NORMAL_TEACHING_PERIOD zählt als Unterricht."""
    errors = optional(payload, "errors", list, "$", [])
    if errors:
        raise IncompleteDataError(
            "WebUntis meldet Fehler im Stundenplan; der Abruf ist unvollständig.",
            hint="Später erneut versuchen; 'schulsync check' ausführen.",
            detail=f"{len(errors)} Fehlereintrag(e) in $.errors",
        )
    days = require(payload, "days", list, "$")
    out: list[_Lesson] = []
    for di, day in enumerate(days):
        entries = optional(day, "gridEntries", list, f"$.days[{di}]", [])
        for ei, entry in enumerate(entries):
            p = f"$.days[{di}].gridEntries[{ei}]"
            if optional(entry, "type", str, p, "") != "NORMAL_TEACHING_PERIOD":
                continue  # Ereignisse/Prüfungseinträge sind keine Unterrichtsstunden
            ids = require(entry, "ids", list, p)
            if not ids or not all(_is(x, int) for x in ids):
                raise _schema(f"{p}.ids", "nichtleere Liste von Ganzzahlen erwartet")
            duration = require(entry, "duration", dict, p)
            start = _local_iso(require(duration, "start", str, f"{p}.duration"), f"{p}.duration.start")
            end = _local_iso(require(duration, "end", str, f"{p}.duration"), f"{p}.duration.end")
            status = {"REGULAR": "regular", "CANCELLED": "cancelled", "CHANGED": "irregular"}.get(
                optional(entry, "status", str, p, ""), "unknown"
            )
            by_type: dict[str, list[str]] = {}
            for n in range(1, 8):
                for si, slot in enumerate(optional(entry, f"position{n}", list, p, [])):
                    sp = f"{p}.position{n}[{si}]"
                    cur, rem = optional(slot, "current", dict, sp), optional(slot, "removed", dict, sp)
                    # [STG] bei Änderungen steht der abwesende Teil unter `removed`; "?" = nicht ermittelt
                    pick = cur if cur and optional(cur, "shortName", str, sp, "") not in ("", "?") else (rem or cur)
                    if pick:
                        name = clean_text(optional(pick, "longName", str, sp, "") or optional(pick, "shortName", str, sp, ""))
                        if name and name != "?":
                            by_type.setdefault(optional(pick, "type", str, sp, ""), []).append(name)
            texts = []
            for label, key in (("Vertretungstext", "substitutionText"), ("Info", "lessonInfo")):
                text = clean_text(optional(entry, key, str, p, ""))
                if text:
                    texts.append(f"{label}: {text}")
            out.append(
                _Lesson(
                    key=f"{ids[0]}-{_ymd(start.date())}-{_hmm_key(start)}",
                    start=start,
                    end=end,
                    status=status,
                    subject=", ".join(by_type.get("SUBJECT", [])) or None,
                    teacher=", ".join(by_type.get("TEACHER", [])) or None,
                    room=", ".join(by_type.get("ROOM", [])) or None,
                    texts=tuple(texts),
                    merged=len(ids),
                )
            )
    return out


# --------------------------------------------------------------------------- Titelregeln


def homework_title(text: str, course: str | None) -> tuple[str, bool, bool]:
    """Fester Titelregel (siehe Moduldoku). Rückgabe: (Titel, erste Zeile gekürzt, Text vorhanden)."""
    first = text.split("\n", 1)[0].strip() if text else ""
    if not first:
        return (f"Hausaufgabe {course}" if course else "Hausaufgabe"), False, False
    if len(first) <= TITLE_MAX:
        return first, False, True
    cut = first[:TITLE_MAX]
    if not first[TITLE_MAX].isspace() and " " in cut[40:]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip() + "…", True, True


def _envelope(payload: Any, key: str) -> tuple[dict, str]:
    """Antwortumschlag der klassischen Endpunkte: {"data": {<key>: [...]}} [SCH] oder, bei einem anderen Mandanten
    beobachtet, {"<key>": [...]} [STG: ExamEnvelope/HomeworkEnvelope]. Alles andere ist ein SchemaError."""
    if isinstance(payload, dict) and payload.get("data") is None and payload.get(key) is not None:
        return payload, "$"
    return require(payload, "data", dict, "$"), "$.data"


def _incomplete(code_title: str, message: str, kind: Kind, source_id: str, severity: Severity) -> Notice:
    return Notice(
        severity,
        "incomplete_data",
        message,
        Source.WEBUNTIS,
        make_uid(Source.WEBUNTIS, kind, source_id),
        code_title,
    )


# --------------------------------------------------------------------------- Adapter


@dataclass
class _Session:
    route: str = ""
    person_id: int | None = None
    person_type: int | None = None
    rest_person_id: int | None = None  # user.person.id aus REST app/data (für die REST-Routen)
    bearer: str | None = None
    rpc_usable: bool = True  # False: jsonrpc.do sprach kein JSON-RPC (Anmeldung per Webformular)
    cookie_matches: bool | None = None  # JSESSIONID im Cookiespeicher == sessionId der Anmeldeantwort


class WebUntisAdapter:
    """Lesender WebUntis-Adapter (models.SourceAdapter). Eine Instanz = höchstens ein Anmeldeversuch."""

    source = Source.WEBUNTIS

    def __init__(
        self,
        config: WebUntisConfig,
        password: str,
        http_factory: Callable[[], HttpClient] | None = None,
        *,
        clock: Callable[[], datetime] = now_vienna,
    ):
        self._clock = clock
        self._cfg = config
        self._password = password
        self._http: HttpClient = (http_factory or RequestsHttp)()
        self._base = config.base_url.rstrip("/")
        self._school = config.school
        self._session: _Session | None = None
        self._login_failure: SchulSyncError | None = None  # nicht-vorübergehende Ablehnung: nie erneut versuchen
        self._school_years: list[tuple[date, date]] | None | bool = False  # False = noch nicht gelesen
        self._reached = False

    # ------------------------------------------------------------------ Öffentliche API

    def check(self) -> ProbeReport:
        report = ProbeReport(self.source)
        try:
            self._reach()
            report.steps.append(ProbeStep("Erreichbarkeit", True, f"WebUntis antwortet ({safe_url(self._base)})."))
        except SchulSyncError as exc:
            report.steps.append(ProbeStep("Erreichbarkeit", False, exc.message))
            return report
        try:
            session = self._ensure_login()
        except SchulSyncError as exc:
            report.steps.append(ProbeStep("Anmeldung", False, exc.message))
            return report
        login_detail = f"Angemeldet über: {session.route}."
        if session.cookie_matches is False:
            login_detail += (
                " Hinweis: Das Sitzungscookie weicht von der Sitzungskennung der Anmeldeantwort ab; "
                "falls Hausaufgaben/Prüfungen unten fehlschlagen, ist das die wahrscheinliche Ursache."
            )
        report.steps.append(ProbeStep("Anmeldung", True, login_detail))
        if session.person_id is not None:
            report.steps.append(ProbeStep("Schülerkennung", True, "Die eigene Personenkennung wurde ermittelt."))
        else:
            report.steps.append(ProbeStep("Schülerkennung", False, "Keine Personenkennung gefunden."))
        now = self._clock()
        today = to_vienna(now).date()
        window = Window(local_midnight(today), local_midnight(today + timedelta(days=7)))
        try:
            result = self._fetch_categories(window, (), now)
        except SchulSyncError as exc:  # z. B. Netzfehler mitten im Abruf
            report.steps.append(ProbeStep("Abruf", False, exc.message))
            return report
        report.categories = dict(result.categories)
        for cat, outcome in result.categories.items():
            if outcome.status == CategoryStatus.DISABLED:
                report.steps.append(ProbeStep(_CATEGORY_LABEL[cat], True, "Per Konfiguration abgeschaltet."))
                continue
            text = outcome.detail or (outcome.error.message if outcome.error else outcome.status.value)
            if outcome.error and outcome.status != CategoryStatus.OK:
                text = f"{outcome.error.message} {outcome.detail or ''}".strip()
            report.steps.append(ProbeStep(_CATEGORY_LABEL[cat], outcome.ok, text))
        return report

    def fetch(self, window: Window, tracked: Sequence[TrackedRef], now: datetime) -> FetchResult:
        try:
            self._ensure_login()
        except TransientError:
            raise
        except SchulSyncError as exc:
            info = exc.to_info()
            result = FetchResult(self.source, now, login_error=info)
            for cat in _CATEGORY_LABEL:
                if self._enabled(cat):
                    result.categories[cat] = CategoryOutcome(
                        cat, CategoryStatus.FAILED, info, "Anmeldung bei WebUntis fehlgeschlagen."
                    )
                else:
                    result.categories[cat] = CategoryOutcome(cat, CategoryStatus.DISABLED)
            return result
        return self._fetch_categories(window, tracked, now)

    # ------------------------------------------------------------------ Kategorien

    def _enabled(self, cat: Category) -> bool:
        return any(_CATEGORY_NAMES.get(name) == cat for name in self._cfg.categories)

    def _fetch_categories(self, window: Window, tracked: Sequence[TrackedRef], now: datetime) -> FetchResult:
        result = FetchResult(self.source, now)
        mine = [t for t in tracked if t.source == self.source]
        handlers: dict[Category, Callable[[Window, Sequence[TrackedRef]], _CatResult]] = {
            Category.HOMEWORK: self._homework,
            Category.EXAMS: self._exams,
            Category.CANCELLATIONS: self._cancellations,
        }
        kinds = {Category.HOMEWORK: Kind.HOMEWORK, Category.EXAMS: Kind.EXAM, Category.CANCELLATIONS: Kind.CANCELLATION}
        expired: ErrorInfo | None = None
        for cat, handler in handlers.items():
            if not self._enabled(cat):
                result.categories[cat] = CategoryOutcome(cat, CategoryStatus.DISABLED)
                continue
            if expired is not None:  # Sitzung tot: keine weiteren Anfragen, ehrlich als Fehler melden
                result.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, expired, "Sitzung abgelaufen.")
                continue
            refs = [t for t in mine if t.kind == kinds[cat]]
            try:
                res = handler(window, refs)
            except TransientError:
                raise
            except SessionExpiredError as exc:
                self._session = None  # frische Anmeldung bei einem späteren Abruf ist erlaubt (Zugang war gültig)
                expired = exc.to_info()
                result.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, expired, "Sitzung abgelaufen.")
                continue
            except AccessDeniedError as exc:
                info = ErrorInfo(
                    exc.code,
                    f"Kein Zugriff auf {_CATEGORY_LABEL[cat]} in WebUntis (fehlende Rechte des Schülerkontos).",
                    "Die anderen Kategorien sind nicht betroffen. Rechte in WebUntis bzw. bei der Schule prüfen.",
                    exc.detail,
                )
                result.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, info)
                continue
            except IncompleteDataError as exc:
                result.categories[cat] = CategoryOutcome(cat, CategoryStatus.INCOMPLETE, exc.to_info())
                continue
            except SchulSyncError as exc:
                result.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, exc.to_info())
                continue
            result.items.extend(res.items)
            result.notices.extend(res.notices)
            result.withdrawn.update(res.withdrawn)
            result.categories[cat] = CategoryOutcome(cat, res.status, None, res.detail, len(res.items))
        return result

    @staticmethod
    def _plan(window: Window, refs: Sequence[TrackedRef], *, to_year_end: bool = False) -> list[tuple[date, date]]:
        """Abrufbereiche = Vereinigung aus Fenster und Bereichen der verfolgten Einträge.

        `to_year_end` (Hausaufgaben/Prüfungen): gibt es verfolgte Einträge, wird zusätzlich ab Fensterbeginn bis zum Ende
        des Schuljahres gelesen. Nur so fällt eine verschobene Frist/Prüfung auf, deren neues Datum weder im Fenster
        noch im alten Datumsbereich liegt (ANFORDERUNGEN 2). Schuljahresende = 31.08. (Annahme: Österreich).
        """
        first, last = _window_days(window)
        ranges = [(first, last), *(_ref_days(r) for r in refs)]
        if to_year_end and refs:
            ranges.append((first, max(last, date(last.year + (last.month >= 9), 8, 31))))
        return merge_ranges(ranges)

    @staticmethod
    def _wanted(item: SourceItem, window: Window, refs: Sequence[TrackedRef]) -> bool:
        return item.uid in {r.uid for r in refs} or window.overlaps(item.when.start, item.when.end)

    @staticmethod
    def _sorted(items: dict[str, SourceItem]) -> list[SourceItem]:
        return sorted(items.values(), key=lambda i: (i.when.start_date, i.uid))

    # -- Hausaufgaben [SCH,STG]

    def _homework(self, window: Window, refs: Sequence[TrackedRef]) -> _CatResult:
        res = _CatResult()
        items: dict[str, SourceItem] = {}
        requests = 0
        ranges = merge_ranges(
            [(a - timedelta(days=HOMEWORK_LOOKBACK_DAYS), b) for a, b in self._plan(window, refs, to_year_end=True)]
        )
        for first, last in ranges:
            for a, b in chunk_range(first, last):
                requests += 1
                payload = self._get_json(
                    "/WebUntis/api/homeworks/lessons",
                    {"startDate": _ymd(a), "endDate": _ymd(b)},
                    "WebUntis Hausaufgaben",
                )
                data, dp = _envelope(payload, "homeworks")  # [SCH,STG]
                homeworks = require(data, "homeworks", list, dp)
                expect_field_somewhere(homeworks, ("dueDate",), f"{dp}.homeworks")
                subjects: dict[int, str] = {}
                for li, lesson in enumerate(optional(data, "lessons", list, dp, [])):
                    lp = f"{dp}.lessons[{li}]"
                    subject = clean_text(optional(lesson, "subject", str, lp, ""))
                    if subject:
                        subjects[require(lesson, "id", int, lp)] = subject
                for hi, hw in enumerate(homeworks):
                    notices: list[Notice] = []
                    item = self._homework_item(hw, f"{dp}.homeworks[{hi}]", subjects, notices)
                    # Hinweise nur für gelieferte Einträge; fehlendes Datum (item None) lässt sich nicht zuordnen
                    if item is None or (self._wanted(item, window, refs) and item.uid not in items):
                        res.notices.extend(notices)
                    if item and self._wanted(item, window, refs):
                        items.setdefault(item.uid, item)
        res.items = self._sorted(items)
        res.detail = f"Weg: {ROUTE_HOMEWORK}; {len(res.items)} Einträge ({requests} Abruf(e))."
        return res

    def _homework_item(
        self, hw: Any, p: str, subjects: Mapping[int, str], notices: list[Notice]
    ) -> SourceItem | None:
        hw_id = require(hw, "id", int, p)
        text = clean_text(optional(hw, "text", str, p, ""))
        remark = clean_text(optional(hw, "remark", str, p, ""))
        lesson_id = optional(hw, "lessonId", int, p)
        course = subjects.get(lesson_id) if lesson_id is not None else None
        title, clipped, has_text = homework_title(text, course)
        due = hw.get("dueDate")
        if due in (None, 0):
            notices.append(
                Notice(
                    Severity.WARNING,
                    "no_date",
                    f"Hausaufgabe ohne Fälligkeitsdatum übersprungen: «{title}».",
                    Source.WEBUNTIS,
                    make_uid(Source.WEBUNTIS, Kind.HOMEWORK, str(hw_id)),
                    title,
                )
            )
            return None
        due_date = parse_ymd(due, f"{p}.dueDate")
        notes: list[str] = []
        if clipped:
            notes.append("Titel gekürzt (erste Zeile des Hausaufgabentextes); der vollständige Text steht in der Beschreibung.")
            notices.append(
                _incomplete(title, f"Hausaufgabentitel gekürzt: «{title}». Vollständiger Text in der Beschreibung.",
                            Kind.HOMEWORK, str(hw_id), Severity.INFO)
            )
        if not has_text:
            notes.append("Kein Hausaufgabentext in WebUntis vorhanden.")
            notices.append(
                _incomplete(title, f"Hausaufgabe ohne Text: «{title}».", Kind.HOMEWORK, str(hw_id), Severity.WARNING)
            )
        if optional(hw, "attachments", list, p, []):
            notes.append("In WebUntis sind Anhänge vorhanden; sie werden nicht übernommen.")
        description = "\n\n".join(x for x in (text, f"Anmerkung: {remark}" if remark else "") if x) or None
        return SourceItem(
            source=Source.WEBUNTIS,
            source_id=str(hw_id),
            kind=Kind.HOMEWORK,
            title=title,
            when=ItemTime(due_date),
            course=course,
            description=description,
            notes=tuple(notes),
        )

    # -- Prüfungen [SCH,STG]

    def _exams(self, window: Window, refs: Sequence[TrackedRef]) -> _CatResult:
        res = _CatResult()
        items: dict[str, SourceItem] = {}
        requests = 0
        person = self._require_session().person_id
        for first, last in self._plan(window, refs, to_year_end=True):
            for a, b in chunk_range(first, last):
                requests += 1
                params: dict[str, Any] = {"startDate": _ymd(a), "endDate": _ymd(b), "klasseId": "-1", "withGrades": "false"}
                if person is not None:
                    params["studentId"] = person
                payload = self._get_json("/WebUntis/api/exams", params, "WebUntis Prüfungen")
                data, dp = _envelope(payload, "exams")  # [SCH,STG]
                exams = require(data, "exams", list, dp)
                expect_field_somewhere(exams, ("examDate", "startDateTime"), f"{dp}.exams")
                for ei, raw in enumerate(exams):
                    notices: list[Notice] = []
                    item = self._exam_item(raw, f"{dp}.exams[{ei}]", notices)
                    if item is None or (self._wanted(item, window, refs) and item.uid not in items):
                        res.notices.extend(notices)
                    if item and self._wanted(item, window, refs):
                        items.setdefault(item.uid, item)
        res.items = self._sorted(items)
        res.detail = f"Weg: {ROUTE_EXAMS}; {len(res.items)} Einträge ({requests} Abruf(e))."
        return res

    @staticmethod
    def _string_list(raw: Any, key: str, p: str) -> str | None:
        values = optional(raw, key, list, p, [])
        names = [clean_text(v) for v in values if isinstance(v, str)]
        if len(names) != len(values):
            raise _schema(f"{p}.{key}", "Liste von Texten erwartet")
        return ", ".join(n for n in names if n) or None

    @staticmethod
    def _exam_when(start: datetime, end: datetime | None, notes: list[str]) -> ItemTime:
        if end is not None and end <= start:
            end = None
            notes.append("Die Endzeit der Prüfung in WebUntis ist unplausibel und wurde nicht übernommen.")
        return ItemTime(start, end)

    def _exam_item(self, raw: Any, p: str, notices: list[Notice]) -> SourceItem | None:
        exam_id = require(raw, "id", int, p)
        name = clean_text(optional(raw, "name", str, p, ""))
        subject = clean_text(optional(raw, "subject", str, p, ""))
        notes: list[str] = []
        title = name or subject
        iso_start = optional(raw, "startDateTime", str, p)
        if iso_start is None and optional(raw, "examDate", int, p, 0) == 0:  # [SCH] 0/fehlt = ohne Datum (DESIGN §8)
            notices.append(
                Notice(
                    Severity.WARNING,
                    "no_date",
                    f"Prüfung ohne Datum übersprungen: «{title or 'Prüfung'}».",
                    Source.WEBUNTIS,
                    make_uid(Source.WEBUNTIS, Kind.EXAM, str(exam_id)),
                    title or "Prüfung",
                )
            )
            return None
        if not title:
            title = "Prüfung"
            notes.append("In WebUntis ist weder Name noch Fach der Prüfung angegeben.")
            notices.append(_incomplete(title, "Prüfung ohne Name und Fach in WebUntis.", Kind.EXAM, str(exam_id), Severity.WARNING))
        when: ItemTime
        if iso_start is not None:  # [STG] zweite beobachtete Form: ISO-Zeitangaben
            start = _local_iso(iso_start, f"{p}.startDateTime")
            iso_end = optional(raw, "endDateTime", str, p)
            end = _local_iso(iso_end, f"{p}.endDateTime") if iso_end else None
            when = self._exam_when(start, end, notes)
        else:  # [SCH] types.ts Exam: examDate + startTime/endTime als Ganzzahlen
            day = parse_ymd(require(raw, "examDate", int, p), f"{p}.examDate")
            start_raw, end_raw = optional(raw, "startTime", int, p, 0), optional(raw, "endTime", int, p, 0)
            if start_raw == 0 and end_raw == 0:
                when = ItemTime(day)  # keine Uhrzeit hinterlegt -> ganztägig
                notes.append("In WebUntis ist keine Uhrzeit für die Prüfung hinterlegt.")
            else:
                start = make_local(day, parse_hmm(start_raw, f"{p}.startTime"))
                end = make_local(day, parse_hmm(end_raw, f"{p}.endTime")) if end_raw else None
                when = self._exam_when(start, end, notes)
        return SourceItem(
            source=Source.WEBUNTIS,
            source_id=str(exam_id),
            kind=Kind.EXAM,
            title=title,
            when=when,
            course=subject or None,
            description=clean_text(optional(raw, "text", str, p, "")) or None,
            room=self._string_list(raw, "rooms", p),
            teacher=self._string_list(raw, "teachers", p),
            notes=tuple(notes),
            subtype=clean_text(optional(raw, "examType", str, p, "")) or None,
        )

    # -- Unterrichtsentfall [PYW,SCH] bzw. [STG]

    def _cancellations(self, window: Window, refs: Sequence[TrackedRef]) -> _CatResult:
        res = _CatResult()
        ranges = self._plan(window, refs)
        fallback_note = ""
        why: str | None = None
        route = ROUTE_TT_RPC
        if not self._require_session().rpc_usable:  # Anmeldung nur per Formular möglich: jsonrpc.do spricht kein JSON-RPC
            why = "JSON-RPC wird von diesem WebUntis nicht angeboten"
        else:
            try:
                lessons, covered = self._lessons_rpc(ranges)
            except (_RpcError, NotFoundError, AccessDeniedError) as first:  # Weg 1 dauerhaft nicht nutzbar -> Weg 2
                why = (
                    first.reason
                    if isinstance(first, _RpcError)
                    else "Schnittstelle nicht gefunden" if isinstance(first, NotFoundError) else "Zugriff verweigert (HTTP 403)"
                )
        if why is not None:
            route = ROUTE_TT_REST
            fallback_note = f" (Weg 1 {ROUTE_TT_RPC} nicht nutzbar: {why})"
            try:
                lessons, covered = self._lessons_rest(ranges)
            except SchulSyncError as exc:
                exc.detail = f"{exc.detail or ''} | Weg 1 {ROUTE_TT_RPC}: {why}".strip(" |")
                raise
        refs_by_key = {r.source_id: r for r in refs}
        items: dict[str, SourceItem] = {}
        cancelled_keys = {l.key for l in lessons if l.status == "cancelled"}
        for lesson in lessons:
            if lesson.status == "cancelled":
                notices: list[Notice] = []
                item = self._lesson_item(lesson, notices)
                if self._wanted(item, window, refs):
                    if item.uid not in items:
                        res.notices.extend(notices)
                    items.setdefault(item.uid, item)
            # Dieselbe Kennung in diesem Abruf auch als Entfall: widersprüchlich, also kein Rücknahme-Beleg
            elif lesson.status in ("regular", "irregular") and lesson.key in refs_by_key and lesson.key not in cancelled_keys:
                ref = refs_by_key[lesson.key]
                if not ref.cancelled:
                    what = "regulär" if lesson.status == "regular" else "Vertretung/Änderung"
                    res.withdrawn[ref.uid] = (
                        f"WebUntis zeigt die Stunde {lesson.subject or 'Unterricht'} am {_de_date(lesson.start.date())} "
                        f"um {lesson.start:%H:%M} Uhr nicht mehr als Entfall an (Status: {what})."
                    )
        res.items = self._sorted(items)
        res.detail = f"Weg: {route}{fallback_note}; {len(res.items)} Entfall-Einträge, Zeitraum {covered}."
        if covered == _NO_PERIOD:
            # Honest, but not silent (ANFORDERUNGEN 3): "0 Entfälle" must not read like "all lessons were checked". Holidays are the
            # usual reason; a school year that WebUntis has not entered yet looks the same.
            res.notices.append(
                Notice(
                    Severity.INFO,
                    "no_school_year",
                    "Im Prüfzeitraum liegt laut WebUntis kein Schuljahr (Ferien oder Schuljahr noch nicht eingetragen); "
                    "Unterrichtsentfälle konnten deshalb nicht geprüft werden.",
                    Source.WEBUNTIS,
                )
            )
        return res

    def _lesson_item(self, lesson: _Lesson, notices: list[Notice]) -> SourceItem:
        notes: list[str] = []
        title = lesson.subject
        if not title:
            title = "Unterrichtsstunde"
            notes.append("In WebUntis ist kein Fach für die ausgefallene Stunde angegeben.")
            notices.append(_incomplete(title, f"Entfall ohne Fach am {_de_date(lesson.start.date())}.", Kind.CANCELLATION, lesson.key, Severity.WARNING))
        if lesson.merged > 1:
            notes.append(f"WebUntis fasst {lesson.merged} aufeinanderfolgende Stunden zusammen.")
        end: datetime | None = lesson.end
        if end <= lesson.start:  # unplausible Endzeit: keine erfundene/negative Dauer (wie bei Prüfungen)
            end = None
            notes.append("Die Endzeit der Stunde in WebUntis ist unplausibel und wurde nicht übernommen.")
        return SourceItem(
            source=Source.WEBUNTIS,
            source_id=lesson.key,
            kind=Kind.CANCELLATION,
            title=title,
            when=ItemTime(lesson.start, end),
            course=lesson.subject,
            description="\n".join(lesson.texts) or None,
            room=lesson.room,
            teacher=lesson.teacher,
            notes=tuple(notes),
        )

    def _lessons_rpc(self, ranges: Sequence[tuple[date, date]]) -> tuple[list[_Lesson], str]:
        session = self._require_session()
        if session.person_id is None:
            raise _RpcError(None, "Stundenplan", "keine Personenkennung in der Anmeldeantwort")
        years = self._years()
        todo = _clip(ranges, years) if years else list(ranges)
        out: list[_Lesson] = []
        for first, last in todo:
            for a, b in chunk_range(first, last):
                options = {
                    "element": {"id": session.person_id, "type": session.person_type or STUDENT_ELEMENT_TYPE},
                    "startDate": _ymd(a),
                    "endDate": _ymd(b),
                    "showLsText": True,
                    "showSubstText": True,
                    "showInfo": True,
                    "klasseFields": ["id", "name", "longname"],
                    "roomFields": ["id", "name", "longname"],
                    "subjectFields": ["id", "name", "longname"],
                    "teacherFields": ["id", "name", "longname"],
                }
                out.extend(lessons_from_rpc(self._rpc("getTimetable", {"options": options}, "WebUntis Stundenplan")))
        covered = ", ".join(f"{_de_date(a)} bis {_de_date(b)}" for a, b in todo) or _NO_PERIOD
        return out, covered

    def _years(self) -> list[tuple[date, date]] | None:
        """Schuljahre [PYW,SCH getSchoolyears]; nur zum Zuschneiden (außerhalb gibt es keinen Stundenplan)."""
        if self._school_years is False:
            try:
                raw = self._rpc("getSchoolyears", {}, "WebUntis Schuljahre")
                if not isinstance(raw, list):
                    raise _schema("$.result", "Liste erwartet")
                self._school_years = [
                    (
                        parse_ymd(require(y, "startDate", int, f"$.result[{i}]"), f"$.result[{i}].startDate"),
                        parse_ymd(require(y, "endDate", int, f"$.result[{i}]"), f"$.result[{i}].endDate"),
                    )
                    for i, y in enumerate(raw)
                ] or None
            except (_RpcError, NotFoundError, AccessDeniedError, SchemaError):
                self._school_years = None  # ohne Schuljahre: nicht zuschneiden (nur eine Optimierung)
        return self._school_years or None  # type: ignore[return-value]

    def _lessons_rest(self, ranges: Sequence[tuple[date, date]]) -> tuple[list[_Lesson], str]:
        person = self._rest_person_id()
        out: list[_Lesson] = []
        for first, last in ranges:
            for a, b in chunk_range(first, last):
                payload = self._get_json(
                    "/WebUntis/api/rest/view/v1/timetable/entries",
                    {
                        "start": a.isoformat(),
                        "end": b.isoformat(),
                        "format": "3",
                        "resourceType": "STUDENT",
                        "resources": person,
                        "periodTypes": "",
                        "timetableType": "MY_TIMETABLE",
                    },
                    "WebUntis Stundenplan (REST)",
                    bearer=True,
                )
                out.extend(lessons_from_rest(payload))
        return out, ", ".join(f"{_de_date(a)} bis {_de_date(b)}" for a, b in ranges)

    def _rest_person_id(self) -> int:
        """[STG, SCOUT.md §1] user.person.id aus REST app/data (Bearer nötig); einmal je Sitzung gelesen."""
        session = self._require_session()
        if session.rest_person_id is None:
            data = self._get_json("/WebUntis/api/rest/view/v1/app/data", None, "WebUntis Benutzerdaten", bearer=True)
            person = require(require(data, "user", dict, "$"), "person", dict, "$.user")
            session.rest_person_id = require(person, "id", int, "$.user.person")
            if session.person_id is None:
                session.person_id = session.rest_person_id
        return session.rest_person_id

    # ------------------------------------------------------------------ HTTP-Grundlagen

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _require_session(self) -> _Session:
        if self._session is None:
            raise SessionExpiredError(
                "Die WebUntis-Sitzung ist nicht mehr gültig.", hint="Der nächste Versuch meldet sich neu an."
            )
        return self._session

    @staticmethod
    def _looks_html(resp: HttpResponse) -> bool:
        return "html" in resp.headers.get("content-type", "").lower() or resp.text.lstrip()[:1] == "<"

    def _expired(self, what: str) -> SessionExpiredError:
        return SessionExpiredError(
            f"{what}: WebUntis verlangt eine Anmeldung (Sitzung abgelaufen oder Anmeldung nicht akzeptiert).",
            hint="Beim nächsten Versuch wird neu angemeldet; bleibt der Fehler, Zugangsdaten prüfen.",
        )

    def _get_raw(self, path: str, params: Mapping[str, Any] | None, what: str, *, bearer: bool = False) -> HttpResponse:
        headers = {"Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"}
        if bearer:
            token = self._require_session().bearer
            if not token:
                token = self._fetch_bearer()
            headers["Authorization"] = f"Bearer {token}"
        resp = self._http.request("GET", self._url(path), params=params, headers=headers, allow_redirects=False)
        if 300 <= resp.status < 400 or resp.status == 401:
            raise self._expired(what)
        classify_status(resp, what)
        return resp

    def _get_json(self, path: str, params: Mapping[str, Any] | None, what: str, *, bearer: bool = False) -> Any:
        resp = self._get_raw(path, params, what, bearer=bearer)
        if self._looks_html(resp):
            raise self._expired(what)
        return resp.json()

    def _fetch_bearer(self) -> str:
        """[STG] GET /api/token/new (Cookie) -> Bearer-JWT als Text. Das Token wird nie geloggt."""
        resp = self._get_raw("/WebUntis/api/token/new", None, "WebUntis Zugriffstoken")
        token = resp.text.strip()
        if len(token) < 40 or self._looks_html(resp):
            raise _schema("token/new", "Zugriffstoken erwartet")
        self._require_session().bearer = token
        return token

    def _rpc(self, method: str, params: Any, what: str, *, authenticating: bool = False) -> Any:
        """JSON-RPC-Aufruf [PYW,SCH]. Nur lesende Methoden (authenticate, getTimetable, getSchoolyears) werden genutzt."""
        if method not in READ_ONLY_RPC_METHODS:  # read-only guard, before anything is sent
            raise ForbiddenMethodError("Interner Fehler: nicht erlaubte WebUntis-Methode.", detail=method)
        body = {"id": CLIENT_ID, "method": method, "params": params, "jsonrpc": "2.0"}
        resp = self._http.request(
            "POST",
            self._url("/WebUntis/jsonrpc.do"),
            params={"school": self._school},
            json=body,
            headers={"Accept": "application/json"},
            allow_redirects=False,
        )
        if authenticating and resp.status in (301, 302, 303, 307, 308, 404, 405):
            raise _RpcUnavailable()
        if authenticating and resp.status == 401:
            raise AuthError("WebUntis hat die Anmeldung abgelehnt.", hint="Zugangsdaten prüfen.", detail="HTTP 401")
        if 300 <= resp.status < 400 or resp.status == 401:
            raise self._expired(what)
        classify_status(resp, what)
        try:
            data = resp.json() if not self._looks_html(resp) else None
        except SchemaError:
            data = None
        if not isinstance(data, dict):
            if authenticating:
                raise _RpcUnavailable()
            raise self._expired(what)
        if "error" in data:
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) and _is(err.get("code"), int) else None
            if code == -8520:
                raise self._expired(what)
            if code in (-7004, -8507):
                raise IncompleteDataError(
                    f"{what}: Der angefragte Zeitraum ist in WebUntis nicht erlaubt (außerhalb des Schuljahres).",
                    hint="Prüfzeitraum oder Schuljahreseinstellungen in WebUntis prüfen.",
                    detail=f"JSON-RPC-Fehlercode {code}",
                )
            if authenticating:
                if code == -32601:
                    raise _RpcUnavailable()
                if code == -8504:
                    raise AuthError(
                        "WebUntis hat Benutzername oder Passwort abgelehnt.",
                        hint="Zugangsdaten mit 'schulsync setup' neu eingeben (kein automatischer Wiederholversuch).",
                        detail="JSON-RPC-Fehlercode -8504",
                    )
                raise AuthError(
                    "Die Anmeldung bei WebUntis wurde abgelehnt.",
                    hint="Zugangsdaten prüfen; 'schulsync check' ausführen.",
                    detail=f"JSON-RPC-Fehlercode {code}",
                )
            raise _RpcError(code, what)
        return data.get("result")

    # ------------------------------------------------------------------ Anmeldung

    def _reach(self) -> None:
        """Startseite laden (setzt wie im Browser die Sitzungs-/Schul-Cookies); GET, ohne Anmeldung."""
        if self._reached:
            return
        resp = self._http.request("GET", self._url("/WebUntis/"), params={"school": self._school})
        classify_status(resp, "WebUntis-Startseite")
        self._reached = True

    def _ensure_login(self) -> _Session:
        if self._login_failure is not None:
            raise self._login_failure
        if self._session is not None:
            return self._session
        try:
            self._reach()
            try:
                session = self._login_rpc()
            except _RpcUnavailable:
                session = self._login_form()
        except TransientError:
            raise  # Netzproblem: keine Entscheidung des Servers, spätere Wiederholung erlaubt
        except SchulSyncError as exc:
            self._login_failure = exc  # genau EIN Anmeldeversuch pro Adapter
            raise
        self._session = session
        return session

    def _login_rpc(self) -> _Session:
        result = self._rpc(
            "authenticate",
            {"user": self._cfg.username, "password": self._password, "client": CLIENT_ID},
            "WebUntis-Anmeldung",
            authenticating=True,
        )
        if not isinstance(result, dict):
            raise _schema("$.result", "Objekt erwartet")
        if "sessionId" not in result:
            raise AuthError(
                "Die Anmeldung bei WebUntis wurde abgelehnt.",
                hint="Zugangsdaten prüfen; 'schulsync check' ausführen.",
                detail=f"Antwortcode {result.get('code')}" if _is(result.get("code"), int) else None,
            )
        require(result, "sessionId", str, "$.result")
        person_type = optional(result, "personType", int, "$.result")
        if person_type is not None and person_type != STUDENT_ELEMENT_TYPE:
            raise SchemaError(
                "Dieses WebUntis-Konto ist kein Schülerkonto.",
                hint="Für SchulSync wird der persönliche Schülerzugang benötigt.",
                detail=f"personType {person_type}",
            )
        cookie = self._http.get_cookie("JSESSIONID")
        if cookie is None:
            raise _schema("Set-Cookie JSESSIONID", "Sitzungscookie fehlt nach der Anmeldung")
        # Ob der Server das Cookie bei der Anmeldung austauscht, ist in den Quellen nicht belegt (unverified);
        # der Vergleich geht nur als Diagnose in check() ein, der Wert selbst wird nie ausgegeben.
        return _Session(
            ROUTE_LOGIN_RPC,
            optional(result, "personId", int, "$.result"),
            person_type,
            cookie_matches=(cookie == result["sessionId"]),
        )

    def _login_form(self) -> _Session:
        """[STG] Formularanmeldung; nur erreichbar, wenn jsonrpc.do kein JSON-RPC spricht."""
        resp = self._http.request(
            "POST",
            self._url("/WebUntis/j_spring_security_check"),
            data={"school": self._school, "j_username": self._cfg.username, "j_password": self._password, "token": ""},
            headers={"X-Requested-With": "XMLHttpRequest"},
            allow_redirects=False,
        )
        classify_status(resp, "WebUntis-Anmeldung")
        rejected = AuthError(
            "WebUntis hat Benutzername oder Passwort abgelehnt.",
            hint="Zugangsdaten mit 'schulsync setup' neu eingeben (kein automatischer Wiederholversuch).",
        )
        if 300 <= resp.status < 400:
            if "index.do" not in resp.headers.get("location", ""):
                raise rejected
        else:
            body: Any = {}
            if not self._looks_html(resp):
                try:
                    body = resp.json()
                except SchemaError:
                    body = {}  # kein JSON: kein Zustand erkennbar, weiter mit der Sitzungsprüfung
            if not isinstance(body, dict):
                raise _schema("$", "Objekt erwartet")
            state = body.get("state")
            if state == "TOKEN_REQUIRED":
                raise AuthError(
                    "WebUntis verlangt einen Zwei-Faktor-Code; das wird von SchulSync nicht unterstützt.",
                    hint="Zwei-Faktor-Anmeldung für das Konto in WebUntis ausschalten oder Verbesserung abwarten.",
                )
            if state is not None and state != "SUCCESS":
                raise rejected
        session = _Session(ROUTE_LOGIN_FORM, rpc_usable=False)
        self._session = session  # für _get_raw
        try:
            self._fetch_bearer()
            data = self._get_json("/WebUntis/api/rest/view/v1/app/data", None, "WebUntis Benutzerdaten", bearer=True)
        except SessionExpiredError:
            self._session = None
            raise rejected from None
        except SchulSyncError:
            self._session = None
            raise
        session.person_id = session.rest_person_id = require(
            require(require(data, "user", dict, "$"), "person", dict, "$.user"), "id", int, "$.user.person"
        )
        self._session = None
        return session
