"""Vertrag zwischen Synchronisations-Engine und Google-Zugriff.

Das Gateway ist ein dünner, typisierter Wrapper um die Google-APIs. Es tauscht rohe API-JSON-
Dicts aus (Calendar v3 / Tasks v1). Das Bauen der Bodies (`google_bodies.py`) ist reine Logik
und getrennt testbar. Es gibt eine echte Implementierung (`google_client.py`), einen Trockenlauf-
Wrapper für die Vorschau (liest echt, schreibt nie) und einen Fake in `tests/support/`.

Wichtige Google-Eigenheiten, auf die sich die Engine verlässt:
- Kalender-Event-IDs dürfen selbst vergeben werden (Zeichen 0-9a-v, 5..1024) -> deterministische ID,
  ein wiederholtes `insert` ergibt 409 (`ConflictError`) statt eines Duplikats.
- Gelöschte Kalendertermine liefern `status == "cancelled"` (oder 404/410 -> None).
- Tasks haben keine selbst vergebenen IDs. Neue Tasks werden über die Marke "schulsync-id: <uid>"
  im Notiztext wiedererkannt. `list_tasks` liefert auch erledigte, ausgeblendete und gelöschte.
- Tasks kennen nur ein Fälligkeits*datum* (`due` = RFC3339, Uhrzeit wird ignoriert).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

APP_PROP = "schulsync"  # privateExtendedProperty: schulsync=1 kennzeichnet unsere Termine
PROP_UID = "schulsync_uid"
PROP_SOURCE = "schulsync_source"
PROP_KIND = "schulsync_kind"
TASK_MARKER_PREFIX = "schulsync-id: "


def make_event_id(uid: str, gen: int = 0) -> str:
    """Deterministische Kalender-Event-ID (nur 0-9a-f, passt in den Google-Zeichensatz 0-9a-v)."""
    return "ss" + hashlib.sha1(f"{uid}#{gen}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TargetCheck:
    """Ergebnis der Zielprüfung. Fehler werden als Ausnahme (TargetUnavailableError) gemeldet."""

    calendar_id: str
    calendar_summary: str
    tasklist_id: str
    tasklist_title: str


class GoogleGateway(Protocol):
    """Alle Methoden können SchulSyncError-Unterklassen werfen (TransientError, OutcomeUnknownError,
    GoogleAuthError, AccessDeniedError, TargetUnavailableError, ConflictError, ...)."""

    # Prüfung der Ziele; wirft TargetUnavailableError, wenn Kalender/Liste fehlen oder der Kalender
    # der Hauptkalender ist. Weicht NIE auf andere Ziele aus.
    def check_targets(self) -> TargetCheck: ...

    # ---- Kalender
    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """None bei 404/410. Gelöschte Termine können als Dict mit status 'cancelled' kommen."""

    def insert_event(self, body: dict[str, Any]) -> dict[str, Any]:
        """body enthält 'id'. ConflictError bei 409. OutcomeUnknownError, wenn unklar."""

    def patch_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def find_events_by_uid(self, uid: str) -> list[dict[str, Any]]:
        """Abgleich per privateExtendedProperty (inkl. gelöschter, soweit die API sie liefert)."""

    # ---- Aufgaben
    def list_tasks(self) -> list[dict[str, Any]]:
        """ALLE Tasks der Liste (alle Seiten; erledigte, ausgeblendete, gelöschte eingeschlossen)."""

    def get_task(self, task_id: str) -> dict[str, Any] | None: ...

    def insert_task(self, body: dict[str, Any]) -> dict[str, Any]:
        """Kein Wiederholen auf Verdacht! Bei unklarem Ausgang OutcomeUnknownError."""

    def patch_task(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def delete_task(self, task_id: str) -> None:
        """Idempotent: 404/410 gilt als erledigt."""
