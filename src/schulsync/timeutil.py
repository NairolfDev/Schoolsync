"""Zeitzonen- und Kalendertag-Hilfen. Alles Fachliche läuft in Europe/Vienna.

Regeln (siehe docs/DESIGN.md, Abschnitt "Zeit"):
- "Heute" ist der Kalendertag in Europe/Vienna, nie der UTC- oder Systemtag.
- Vorlaufzeiten werden in Kalendertagen (date-Arithmetik) gerechnet, nicht in 72/168 Stunden.
- Mitternacht (00:00) existiert in Europe/Vienna immer (Umstellung passiert um 02:00/03:00).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

VIENNA = ZoneInfo("Europe/Vienna")
UTC = timezone.utc


def now_vienna() -> datetime:
    """Aktuelle Zeit als aware datetime in Europe/Vienna."""
    return datetime.now(VIENNA)


def to_vienna(dt: datetime) -> datetime:
    """Konvertiert ein aware datetime nach Europe/Vienna. Naive Werte sind ein Programmierfehler."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime ist nicht erlaubt (Zeitzone fehlt)")
    return dt.astimezone(VIENNA)


def today_vienna(now: datetime | None = None) -> date:
    """Kalendertag in Europe/Vienna für `now` (Standard: jetzt)."""
    return to_vienna(now if now is not None else now_vienna()).date()


def local_midnight(d: date) -> datetime:
    """00:00 Uhr des Kalendertags `d` in Europe/Vienna (aware)."""
    return datetime.combine(d, time(0, 0), tzinfo=VIENNA)


def minus_calendar_days(d: date, days: int) -> date:
    """`d` minus `days` Kalendertage (reine Datumsarithmetik, DST-sicher)."""
    return d - timedelta(days=days)


def plus_calendar_days(d: date, days: int) -> date:
    return d + timedelta(days=days)


def make_local(d: date, t: time) -> datetime:
    """Kombiniert Datum und Uhrzeit zu einem aware datetime in Europe/Vienna.

    Hinweis: Nicht existierende Ortszeiten (Umstellung 02:00->03:00) werden von zoneinfo
    mit dem Offset vor der Umstellung interpretiert; mehrdeutige Zeiten (fold=0) mit dem
    ersten Vorkommen. Das ist dokumentiertes, deterministisches Verhalten.
    """
    return datetime.combine(d, t.replace(tzinfo=None), tzinfo=VIENNA)


def add_real_minutes(dt: datetime, minutes: int) -> datetime:
    """Addiert tatsächlich verstrichene Minuten (über UTC), unabhängig von Wanduhr-Sprüngen."""
    return (dt.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(VIENNA)


@dataclass(frozen=True)
class Window:
    """Halboffenes Zeitfenster [start, end) in Europe/Vienna (aware datetimes)."""

    start: datetime
    end: datetime

    def _bounds_utc(self) -> tuple[datetime, datetime]:
        # Vergleiche laufen über UTC: zwei aware datetimes mit derselben ZoneInfo würden sonst nach
        # Wanduhrzeit verglichen (fold wird ignoriert, z. B. in der Stunde der Rückumstellung).
        return _utc(self.start), _utc(self.end)

    def contains(self, value: datetime | date) -> bool:
        """True, wenn der Zeitpunkt (bzw. bei `date`: Tagesbeginn) im Fenster liegt."""
        ws, we = self._bounds_utc()
        v = _utc(value) if isinstance(value, datetime) else _utc(local_midnight(value))
        return ws <= v < we

    def overlaps(self, start: datetime | date, end: datetime | date | None) -> bool:
        """True, wenn [start, end) das Fenster schneidet. Ohne `end` zählt der Punkt `start`.

        Ein `date` steht für den ganzen Tag [00:00, nächster Tag 00:00); ein `end` wird bei
        `date`-Start ignoriert (Eintrag = ein Kalendertag), ebenso ein `date` als `end` bei datetime-Start.
        """
        ws, we = self._bounds_utc()
        s, e = (_utc(x) for x in _as_interval(start, end))
        return s < we and e > ws if e > s else ws <= s < we


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime ist nicht erlaubt (Zeitzone fehlt)")
    return dt.astimezone(UTC)


def _as_interval(start: datetime | date, end: datetime | date | None) -> tuple[datetime, datetime]:
    if isinstance(start, datetime):
        s = to_vienna(start)
        e = to_vienna(end) if isinstance(end, datetime) else s
        return s, e
    s = local_midnight(start)
    e = local_midnight(plus_calendar_days(start, 1))
    return s, e


def slot_start(now: datetime, times: tuple[str, ...]) -> datetime:
    """Beginn des aktuellen Zeitfensters: der späteste Zeitpunkt aus 00:00 und `times` ("HH:MM") heute, der <= now ist."""
    now = to_vienna(now)
    marks = [local_midnight(now.date())] + [
        make_local(now.date(), time(int(t[:2]), int(t[3:5]))) for t in times
    ]
    return max(m for m in marks if m.astimezone(UTC) <= now.astimezone(UTC))


def fetch_window(now: datetime | None = None, days: int = 21) -> Window:
    """Prüffenster [heute 00:00, heute + `days` Kalendertage 00:00) in Europe/Vienna."""
    today = today_vienna(now)
    return Window(local_midnight(today), local_midnight(plus_calendar_days(today, days)))
