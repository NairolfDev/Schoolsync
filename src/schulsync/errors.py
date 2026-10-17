"""Fehlerklassen mit deutschen, geheimnisfreien Meldungen.

Jede Fehlerklasse legt fest, ob ein Wiederholungsversuch sinnvoll ist (`retryable`).
`message` und `hint` sind für den HTML-Bericht bestimmt: Deutsch, ohne Passwörter, Tokens,
Cookies oder geheime URL-Parameter. Technische Details gehören in `detail` (wird vor
Ausgabe maskiert, siehe redact.py).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorInfo:
    """Serialisierbare Kurzform eines Fehlers (für Berichte und die Datenbank)."""

    code: str
    message: str
    hint: str | None = None
    detail: str | None = None


class SchulSyncError(Exception):
    """Basisklasse. Nie Geheimnisse in message/hint/detail schreiben."""

    code = "error"
    retryable = False

    def __init__(self, message: str, *, hint: str | None = None, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(self.code, self.message, self.hint, self.detail)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class TransientError(SchulSyncError):
    """Vorübergehend (Netz, Zeitüberschreitung, 5xx, 429): begrenzt wiederholen."""

    code = "transient"
    retryable = True


class NetworkError(TransientError):
    code = "network"


class OutcomeUnknownError(TransientError):
    """Ein Schreibvorgang wurde gesendet, das Ergebnis ist unbekannt (Antwort verloren/5xx).

    Der Aufrufer MUSS vor jedem erneuten Anlegen anhand der Projektkennung abgleichen.
    """

    code = "outcome_unknown"


class AuthError(SchulSyncError):
    """Anmeldung abgelehnt/abgelaufen. Nicht endlos wiederholen."""

    code = "auth"


class GoogleAuthError(AuthError):
    """Google-Token ungültig, widerrufen oder abgelaufen (z. B. invalid_grant)."""

    code = "google_auth"


class AccessDeniedError(SchulSyncError):
    """403/fehlende Rechte: dauerhaft, nicht wiederholen."""

    code = "access_denied"


class NotFoundError(SchulSyncError):
    code = "not_found"


class BadRequestError(SchulSyncError):
    """400/422: die Gegenstelle lehnt die Anfrage dauerhaft ab (nicht wiederholen)."""

    code = "request_rejected"


class ConflictError(SchulSyncError):
    """409: Ressource existiert bereits (z. B. Kalendertermin mit derselben ID)."""

    code = "conflict"


class SchemaError(SchulSyncError):
    """Antwort hat nicht die erwartete Struktur (Seite/Schnittstelle geändert)."""

    code = "schema"


class IncompleteDataError(SchulSyncError):
    """Abruf unvollständig (Paginierung abgebrochen, Teilantwort). Löst nie Löschungen aus."""

    code = "incomplete"


class TargetUnavailableError(SchulSyncError):
    """Google-Ziel (Kalender 'Schule' / Aufgabenliste 'Schule') nicht verfügbar.

    Es wird NIE auf Hauptkalender oder eine andere Liste ausgewichen.
    """

    code = "target_unavailable"


class ConfigError(SchulSyncError):
    code = "config"
