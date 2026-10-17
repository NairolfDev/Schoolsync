"""Gemeinsames Fachmodell (Vertrag zwischen Adaptern, Planer, Zustand, Engine und Bericht).

Diese Datei ist ein *Vertrag*: Änderungen nur additiv (neue Felder mit Standardwert) und
immer mit Blick auf alle Verwender. Nichts hier führt I/O aus.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from .errors import ErrorInfo
from .timeutil import VIENNA, Window, to_vienna

# --------------------------------------------------------------------------- Aufzählungen


class Source(StrEnum):
    EDUVIDUAL = "eduvidual"
    WEBUNTIS = "webuntis"


class Kind(StrEnum):
    ASSIGNMENT = "assignment"  # Eduvidual: Aufgabe/Abgabe
    HOMEWORK = "homework"  # WebUntis: Hausaufgabe
    EXAM = "exam"  # WebUntis: Prüfung/Schularbeit
    CANCELLATION = "cancellation"  # WebUntis: Unterrichtsentfall


class Category(StrEnum):
    """Abrufkategorie (für Vollständigkeit): jede aktivierte Kategorie muss vollständig sein."""

    ASSIGNMENTS = "assignments"
    HOMEWORK = "homework"
    EXAMS = "exams"
    CANCELLATIONS = "cancellations"


KIND_CATEGORY: Mapping[Kind, Category] = {
    Kind.ASSIGNMENT: Category.ASSIGNMENTS,
    Kind.HOMEWORK: Category.HOMEWORK,
    Kind.EXAM: Category.EXAMS,
    Kind.CANCELLATION: Category.CANCELLATIONS,
}


class ItemStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"  # von der Quelle ausdrücklich als abgesagt/zurückgenommen gemeldet


class CategoryStatus(StrEnum):
    OK = "ok"  # vollständig gelesen (auch wenn leer: echte leere Liste)
    FAILED = "failed"  # Fehler (Login, Rechte, Laden, Struktur)
    INCOMPLETE = "incomplete"  # Teilantwort/Paginierung unvollständig
    DISABLED = "disabled"  # per Konfiguration abgeschaltet (zählt nicht als fehlend)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Target(StrEnum):
    CALENDAR = "calendar"
    TASKS = "tasks"


class Action(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    CANCEL = "cancel"  # Kalender: "Abgesagt:" + Erinnerung aus
    REACTIVATE = "reactivate"
    REMOVE_TASK = "remove_task"  # nur offene Task bei Absage
    COMPLETE = "complete"  # Task abgehakt, weil in Eduvidual abgegeben
    ADOPT = "adopt"  # vorhandenen Google-Eintrag per Projektkennung übernommen
    RESTORE = "restore"  # ausdrückliche Wiederherstellung
    SKIP = "skip"


class ChangeStatus(StrEnum):
    APPLIED = "applied"
    PLANNED = "planned"  # nur Vorschau
    FAILED = "failed"
    DEFERRED = "deferred"  # zurückgestellt (z. B. Google-Ziel nicht erreichbar)
    UNCERTAIN = "uncertain"  # Schreibausgang ungeklärt


class EventState(StrEnum):
    NONE = "none"  # nie angelegt
    ACTIVE = "active"
    USER_DELETED = "user_deleted"  # vom Nutzer in Google gelöscht -> nicht neu anlegen


class TaskState(StrEnum):
    NONE = "none"  # nicht (mehr) vorhanden / noch nicht angelegt
    OPEN = "open"
    DONE = "done"  # vom Nutzer abgehakt -> nie wieder anfassen
    REMOVED = "removed"  # von uns wegen bestätigter Absage entfernt
    USER_DELETED = "user_deleted"  # vom Nutzer gelöscht -> respektieren


class RunMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
    PREVIEW = "preview"


class OverallStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    NOTHING_TO_DO = "nothing_to_do"  # Tagesbegrenzung: heute schon erfolgreich


class SourceStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    ALREADY_DONE = "already_done"
    DISABLED = "disabled"


# --------------------------------------------------------------------------- Hilfen


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enc_when(v: datetime | date | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return to_vienna(v).isoformat()
    return v.isoformat()


def _dec_when(s: str | None) -> datetime | date | None:
    if s is None:
        return None
    if "T" in s:
        return to_vienna(datetime.fromisoformat(s))
    return date.fromisoformat(s)


def make_uid(source: Source | str, kind: Kind | str, source_id: str) -> str:
    return f"{Source(source).value}:{Kind(kind).value}:{source_id}"


# --------------------------------------------------------------------------- Quelldaten


@dataclass(frozen=True)
class ItemTime:
    """Zeitangabe eines Eintrags.

    start: aware datetime (Europe/Vienna) = Zeitpunkt/Beginn; reines `date` = ohne Uhrzeit (ganztägig).
    end:   Ende (nur bei datetime-Start); None = Endzeit unbekannt bzw. Fristmarkierung.
    Für Abgaben/Hausaufgaben ist `start` die Abgabefrist.
    """

    start: datetime | date
    end: datetime | None = None

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    @property
    def start_date(self) -> date:
        return to_vienna(self.start).date() if isinstance(self.start, datetime) else self.start

    def to_dict(self) -> dict[str, Any]:
        return {"start": _enc_when(self.start), "end": _enc_when(self.end)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ItemTime":
        start = _dec_when(d["start"])
        end = _dec_when(d.get("end"))
        assert start is not None
        assert end is None or isinstance(end, datetime)
        return cls(start, end)  # type: ignore[arg-type]


@dataclass(frozen=True)
class SourceItem:
    """Normalisierter Eintrag aus einer Schulplattform. Nur flüchtigkeitsfreie Felder!

    Der Adapter darf KEINE schwankenden Werte (Abrufzeit, Zähler, Sitzungs-IDs) aufnehmen,
    sonst entstehen ständig Scheinänderungen. Texte werden unverfälscht übernommen
    (nur Whitespace normalisiert, HTML in reinen Text umgewandelt, ohne Inhalte zu deuten).
    """

    source: Source
    source_id: str  # stabile Kennung innerhalb der Quelle (z. B. Moodle-cmid, Untis-Hausaufgaben-ID)
    kind: Kind
    title: str  # Originaltitel (ohne Präfixe)
    when: ItemTime
    course: str | None = None  # Fach/Kurs
    description: str | None = None
    room: str | None = None
    teacher: str | None = None
    url: str | None = None  # nur http(s)
    status: ItemStatus = ItemStatus.ACTIVE
    id_stable: bool = True  # False: Kennung wurde aus Inhalten abgeleitet -> konservative Zuordnung
    notes: tuple[str, ...] = ()  # deutsche Hinweise für den verwalteten Text ("Endzeit unbekannt", ...)
    subtype: str | None = None  # Art laut Quelle, z. B. "Schularbeit", "Test"; bestimmt das Präfix bei Prüfungen
    submitted: bool | None = None  # Eduvidual: Abgabe eingereicht? None = unbekannt (dann nie automatisch abhaken)
    attempt: int | None = None  # Eduvidual: Versuchsnummer; steigt, wenn die Lehrkraft zur Überarbeitung zurückgibt

    @property
    def uid(self) -> str:
        return make_uid(self.source, self.kind, self.source_id)

    @property
    def category(self) -> Category:
        return KIND_CATEGORY[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "source_id": self.source_id,
            "kind": self.kind.value,
            "title": self.title,
            "when": self.when.to_dict(),
            "course": self.course,
            "description": self.description,
            "room": self.room,
            "teacher": self.teacher,
            "url": self.url,
            "status": self.status.value,
            "id_stable": self.id_stable,
            "notes": list(self.notes),
            "subtype": self.subtype,
            # nur wenn bekannt: ältere gespeicherte Einträge behalten so denselben Inhalts-Hash
            **({"submitted": self.submitted} if self.submitted is not None else {}),
            **({"attempt": self.attempt} if self.attempt is not None else {}),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SourceItem":
        return cls(
            source=Source(d["source"]),
            source_id=d["source_id"],
            kind=Kind(d["kind"]),
            title=d["title"],
            when=ItemTime.from_dict(d["when"]),
            course=d.get("course"),
            description=d.get("description"),
            room=d.get("room"),
            teacher=d.get("teacher"),
            url=d.get("url"),
            status=ItemStatus(d.get("status", "active")),
            id_stable=bool(d.get("id_stable", True)),
            notes=tuple(d.get("notes", ())),
            subtype=d.get("subtype"),
            submitted=d.get("submitted"),
            attempt=d.get("attempt"),
        )

    def content_hash(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    def dedupe_key(self) -> str:
        """Inhaltsschlüssel (gleiche Inhalte innerhalb einer Quelle sind keine zwei Einträge)."""
        parts = [
            self.source.value,
            self.kind.value,
            " ".join(self.title.split()).casefold(),
            " ".join((self.course or "").split()).casefold(),
            _enc_when(self.when.start) or "",
        ]
        return sha256_hex("\x1f".join(parts))


@dataclass(frozen=True)
class TrackedRef:
    """Bekannter, noch nicht vollständig vergangener Eintrag, den der Adapter weiter prüfen soll,
    auch wenn er außerhalb des Prüffensters liegt (verschobene Fristen, Absagen)."""

    source: Source
    kind: Kind
    source_id: str
    start: datetime | date
    end: datetime | None
    cancelled: bool = False

    @property
    def uid(self) -> str:
        return make_uid(self.source, self.kind, self.source_id)


@dataclass(frozen=True)
class Notice:
    """Hinweis/Warnung/Fehler für den Bericht (deutsch, ohne Geheimnisse)."""

    severity: Severity
    code: str
    message: str
    source: Source | None = None
    uid: str | None = None
    title: str | None = None


@dataclass
class CategoryOutcome:
    category: Category
    status: CategoryStatus
    error: ErrorInfo | None = None
    detail: str | None = None  # z. B. "3 Seiten gelesen"
    item_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (CategoryStatus.OK, CategoryStatus.DISABLED)


@dataclass
class FetchResult:
    """Ergebnis eines Quellabrufs. `complete` ist nur wahr, wenn JEDE aktivierte Kategorie OK ist.

    Ein unvollständiger Abruf darf nie zu Löschungen/Absagen führen: Fehlende Einträge gelten
    nicht als abgesagt. Absagen/Rücknahmen gibt es nur über `item.status == CANCELLED` oder
    `withdrawn` (ausdrücklich bestätigt, mit Beleg).
    """

    source: Source
    fetched_at: datetime
    items: list[SourceItem] = field(default_factory=list)
    categories: dict[Category, CategoryOutcome] = field(default_factory=dict)
    notices: list[Notice] = field(default_factory=list)
    withdrawn: dict[str, str] = field(default_factory=dict)  # uid -> deutscher Beleg
    login_error: ErrorInfo | None = None  # Anmeldung gescheitert (dann sind categories FAILED)

    @property
    def complete(self) -> bool:
        return self.login_error is None and bool(self.categories) and all(
            c.ok for c in self.categories.values()
        )


@dataclass
class ProbeStep:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ProbeReport:
    """Ergebnis von `adapter.check()`: Welche Schnittstellen/Kategorien sind real zugänglich?"""

    source: Source
    steps: list[ProbeStep] = field(default_factory=list)
    categories: dict[Category, CategoryOutcome] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps) and all(c.ok for c in self.categories.values())


class SourceAdapter(Protocol):
    """Austauschbarer Quelladapter. Liest AUSSCHLIESSLICH (keine Änderungen in der Schulplattform).

    `fetch` löst keine Ausnahmen für fachliche Teilfehler aus, sondern liefert sie je Kategorie in
    `FetchResult.categories`/`login_error`. Transiente Fehler (TransientError) DÜRFEN als Ausnahme
    nach oben gehen; der Runner wiederholt begrenzt. AuthError wird nie wiederholt.
    """

    source: Source

    def check(self) -> ProbeReport: ...

    def fetch(self, window: Window, tracked: Sequence[TrackedRef], now: datetime) -> FetchResult: ...


# --------------------------------------------------------------------------- Google-Soll-Zustand


@dataclass(frozen=True)
class DesiredEvent:
    summary: str
    managed_text: str  # Inhalt des verwalteten Beschreibungsblocks
    start: datetime | date  # date => ganztägig
    end: datetime | date  # ganztägig: exklusives Enddatum (Folgetag)
    location: str | None
    popup_minutes: int | None  # None => keine Erinnerung (abgesagt)
    private_props: Mapping[str, str]
    cancelled: bool = False

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    def content_hash(self) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "summary": self.summary,
                    "managed_text": self.managed_text,
                    "start": _enc_when(self.start),
                    "end": _enc_when(self.end),
                    "location": self.location,
                    "popup_minutes": self.popup_minutes,
                    "private_props": dict(self.private_props),
                    "cancelled": self.cancelled,
                }
            )
        )


@dataclass(frozen=True)
class DesiredTask:
    title: str
    managed_text: str  # enthält die Projektkennung "schulsync-id: <uid>"
    due: date  # Arbeitsbeginn (Google Tasks kennt nur ein Datum, keine Uhrzeit)

    def content_hash(self) -> str:
        return sha256_hex(
            canonical_json({"title": self.title, "managed_text": self.managed_text, "due": self.due.isoformat()})
        )


# --------------------------------------------------------------------------- Zustand (Vertrag mit state.py)


@dataclass
class ItemState:
    """Lokaler Zustand je Eintrag (eine Zeile in `items`)."""

    item: SourceItem
    item_hash: str
    cancelled: bool  # maßgeblich: ausdrücklich abgesagt (item.status oder `withdrawn`)
    first_seen_at: datetime
    last_seen_at: datetime
    event_id: str | None = None
    event_gen: int = 0  # Zähler für die deterministische Event-ID (Wiederherstellung = gen + 1)
    event_state: EventState = EventState.NONE
    event_applied_hash: str | None = None  # DesiredEvent.content_hash() des zuletzt geschriebenen Stands
    applied_cancelled: bool = False  # zuletzt als "Abgesagt:" nach Google geschrieben (unterscheidet CANCEL/REACTIVATE/UPDATE)
    dirty: bool = True  # Google-Schreibvorgänge für diesen Eintrag stehen noch aus (neu/geändert/fehlgeschlagen)
    task_id: str | None = None
    task_state: TaskState = TaskState.NONE
    task_applied_hash: str | None = None
    task_base_deadline: str | None = None  # Frist (ISO), aus der task_due berechnet wurde
    task_due: date | None = None
    task_removed_reason: str | None = None  # z. B. "cancelled"
    task_attempt: int | None = None  # Eduvidual-Versuch, zu dem die aktuelle Task gehört
    id_uncertain: bool = False  # Zuordnung per Inhalt (keine stabile Kennung)

    @property
    def uid(self) -> str:
        return self.item.uid


# --------------------------------------------------------------------------- Laufergebnis / Bericht


@dataclass
class Counts:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    cancelled: int = 0
    reactivated: int = 0
    skipped: int = 0
    failed: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1


@dataclass
class Change:
    """Ein einzelner (geplanter/angewendeter/fehlgeschlagener) Google-Schreibvorgang."""

    uid: str
    source: Source
    kind: Kind
    title: str
    target: Target
    action: Action
    status: ChangeStatus
    message: str | None = None
    error: ErrorInfo | None = None


@dataclass
class TargetStatus:
    """Zustand eines Google-Ziels (Kalender 'Schule' / Aufgabenliste 'Schule')."""

    name: str
    ok: bool | None = None  # None = nicht geprüft
    error: ErrorInfo | None = None


@dataclass
class SourceRunResult:
    source: Source
    status: SourceStatus
    categories: dict[Category, CategoryOutcome] = field(default_factory=dict)
    calendar: Counts = field(default_factory=Counts)
    tasks: Counts = field(default_factory=Counts)
    errors: list[ErrorInfo] = field(default_factory=list)
    last_success_at: datetime | None = None  # letzter vollständig erfolgreicher Abruf (beliebiger Tag)
    stale: bool = False  # Daten veraltet (heute nicht erfolgreich aktualisiert)


@dataclass
class RunReport:
    started_at: datetime
    finished_at: datetime
    mode: RunMode
    day: date  # Kalendertag Europe/Vienna
    status: OverallStatus
    sources: dict[Source, SourceRunResult] = field(default_factory=dict)
    calendar_target: TargetStatus = field(default_factory=lambda: TargetStatus("Kalender"))
    tasks_target: TargetStatus = field(default_factory=lambda: TargetStatus("Aufgabenliste"))
    changes: list[Change] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    pending_writes: int = 0  # noch ausstehende Google-Schreibvorgänge am Laufende
    next_steps: list[str] = field(default_factory=list)  # deutsch, dedupliziert
    report_path: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.mode != RunMode.PREVIEW and self.status in (OverallStatus.PARTIAL, OverallStatus.FAILED)

    def problem_keys(self) -> list[str]:
        keys: list[str] = []
        for src, res in self.sources.items():
            if res.status in (SourceStatus.PARTIAL, SourceStatus.FAILED):
                for e in res.errors:
                    keys.append(f"source:{src.value}:{e.code}")
                for cat, oc in res.categories.items():
                    if not oc.ok:
                        keys.append(f"category:{src.value}:{cat.value}:{oc.error.code if oc.error else oc.status.value}")
        for tgt in (self.calendar_target, self.tasks_target):
            if tgt.ok is False:
                keys.append(f"target:{tgt.name}:{tgt.error.code if tgt.error else 'error'}")
        for ch in self.changes:
            if ch.status in (ChangeStatus.FAILED, ChangeStatus.UNCERTAIN, ChangeStatus.DEFERRED):
                keys.append(f"change:{ch.uid}:{ch.target.value}:{ch.error.code if ch.error else ch.status.value}")
        return sorted(set(keys))

    def fingerprint(self) -> str:
        """Stabil für "identische Fehler"; bestimmt, ob ein Browser-Tab erneut geöffnet wird."""
        return sha256_hex("\n".join(self.problem_keys()))[:16]
