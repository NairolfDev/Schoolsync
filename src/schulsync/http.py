"""Schlanke HTTP-Schicht für die Schuladapter (austauschbar und testbar).

- Nur HTTPS. Keine Geheimnisse in Logs: URLs nur über `safe_url` ausgeben.
- Netzwerkfehler werden in `NetworkError` (wiederholbar) übersetzt.
- Statuscodes werden NICHT automatisch geworfen; Adapter entscheiden (`classify_status` hilft).
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from .errors import (
    AccessDeniedError,
    AuthError,
    ConfigError,
    NetworkError,
    NotFoundError,
    SchemaError,
    SchulSyncError,
    TransientError,
)

USER_AGENT = "SchulSync/0.1 (persoenliche, lesende Synchronisation)"


@dataclass
class HttpResponse:
    status: int
    url: str  # endgültige URL nach Weiterleitungen (kann geheime Parameter enthalten -> nie loggen)
    headers: Mapping[str, str] = field(default_factory=dict)  # Schlüssel kleingeschrieben
    text: str = ""
    history: tuple[tuple[int, str], ...] = ()  # (Status, Location) der Weiterleitungsschritte

    def json(self) -> Any:
        try:
            return _json.loads(self.text)
        except ValueError as exc:
            raise SchemaError(
                "Die Antwort der Plattform ist kein gültiges JSON.",
                hint="Möglicherweise ist die Sitzung abgelaufen oder die Seite hat sich geändert.",
                detail=f"HTTP {self.status}, {len(self.text)} Zeichen",
            ) from exc


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | str | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> HttpResponse: ...

    def get_cookie(self, name: str) -> str | None: ...


def safe_url(url: str) -> str:
    """URL ohne Query, Fragment und Zugangsdaten (für Logs und Berichte)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def classify_status(resp: HttpResponse, what: str) -> None:
    """Wirft für Fehlerstatus die passende Fehlerklasse; 2xx/3xx passieren."""
    s = resp.status
    if s < 400:
        return
    where = safe_url(resp.url)
    if s == 401:
        raise AuthError(f"{what}: Anmeldung abgelehnt oder Sitzung abgelaufen.", detail=f"HTTP 401 {where}")
    if s == 403:
        raise AccessDeniedError(f"{what}: Zugriff verweigert (fehlende Rechte).", detail=f"HTTP 403 {where}")
    if s == 404:
        raise NotFoundError(f"{what}: Seite oder Schnittstelle nicht gefunden.", detail=f"HTTP 404 {where}")
    if s == 429 or s >= 500:
        raise TransientError(f"{what}: Server vorübergehend nicht verfügbar (HTTP {s}).", detail=where)
    raise SchulSyncError(f"{what}: Unerwarteter Fehlerstatus HTTP {s}.", detail=where)


class RequestsHttp:
    """HttpClient auf Basis von requests.Session (Cookies werden pro Instanz gehalten)."""

    def __init__(self, connect_timeout: float = 10.0, read_timeout: float = 30.0):
        import requests  # lokal importiert: Tests ohne Netz brauchen es nicht

        self._requests = requests
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._timeout = (connect_timeout, read_timeout)

    def get_cookie(self, name: str) -> str | None:
        return self._session.cookies.get(name)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | str | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> HttpResponse:
        if not url.lower().startswith("https://"):
            raise ConfigError("Es sind nur HTTPS-Adressen erlaubt.", detail=safe_url(url))
        r = self._requests
        try:
            resp = self._session.request(
                method,
                url,
                params=params,
                data=data,
                json=json,
                headers=dict(headers) if headers else None,
                timeout=self._timeout,
                allow_redirects=allow_redirects,
            )
        except (r.exceptions.Timeout, r.exceptions.ConnectionError) as exc:
            raise NetworkError(
                "Keine Verbindung zur Plattform (Internet prüfen).",
                hint="Internetverbindung prüfen; der Versuch wird später automatisch wiederholt.",
                detail=type(exc).__name__,
            ) from exc
        except r.exceptions.RequestException as exc:
            raise NetworkError("Netzwerkfehler bei der Anfrage.", detail=type(exc).__name__) from exc
        history = tuple((h.status_code, h.headers.get("Location", "")) for h in resp.history)
        return HttpResponse(
            status=resp.status_code,
            url=resp.url,
            headers={k.lower(): v for k, v in resp.headers.items()},
            text=resp.text,
            history=history,
        )
