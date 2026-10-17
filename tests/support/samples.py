"""Realistische, anonymisierte Beispieldaten (Fantasienamen) für Engine-/Akzeptanztests."""

from __future__ import annotations

from datetime import date, datetime, time

from schulsync.models import ItemStatus, ItemTime, Kind, Source, SourceItem
from schulsync.timeutil import VIENNA, make_local


def at(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    """Aware datetime in Europe/Vienna."""
    return datetime(y, m, d, hh, mm, tzinfo=VIENNA)


def assignment(
    cmid: str = "40213",
    title: str = "Laborprotokoll 2: Operationsverstärker",
    due: datetime | date | None = None,
    course: str = "ELEKTRONIK 3AHEL",
    description: str | None = "Protokoll als PDF abgeben. Messwerte aus dem Laborblatt verwenden.",
    url: str | None = "https://www.eduvidual.at/mod/assign/view.php?id=40213",
    status: ItemStatus = ItemStatus.ACTIVE,
    notes: tuple[str, ...] = (),
) -> SourceItem:
    return SourceItem(
        source=Source.EDUVIDUAL,
        source_id=f"assign:{cmid}",
        kind=Kind.ASSIGNMENT,
        title=title,
        when=ItemTime(due if due is not None else at(2026, 10, 21, 23, 59)),
        course=course,
        description=description,
        url=url,
        status=status,
        notes=notes,
    )


def homework(
    hid: str = "88231",
    title: str = "Beispiele 12 bis 15 rechnen",
    due: date | datetime | None = None,
    course: str = "Angewandte Mathematik",
    description: str | None = "Seite 87, Beispiele 12 bis 15. Ergebnisse bitte mitbringen.",
    teacher: str | None = "Mag. Musterfrau",
    status: ItemStatus = ItemStatus.ACTIVE,
) -> SourceItem:
    return SourceItem(
        source=Source.WEBUNTIS,
        source_id=f"hw:{hid}",
        kind=Kind.HOMEWORK,
        title=title,
        when=ItemTime(due if due is not None else date(2026, 10, 16)),
        course=course,
        description=description,
        teacher=teacher,
        status=status,
    )


def exam(
    eid: str = "30412",
    title: str = "Schularbeit Angewandte Mathematik",
    start: datetime | date | None = None,
    end: datetime | None = None,
    subtype: str | None = "Schularbeit",
    course: str = "Angewandte Mathematik",
    room: str | None = "R 2.14",
    teacher: str | None = "Mag. Musterfrau",
    status: ItemStatus = ItemStatus.ACTIVE,
    notes: tuple[str, ...] = (),
) -> SourceItem:
    s = start if start is not None else at(2026, 10, 28, 8, 0)
    e = end if end is not None else (at(2026, 10, 28, 9, 40) if isinstance(s, datetime) else None)
    return SourceItem(
        source=Source.WEBUNTIS,
        source_id=f"exam:{eid}",
        kind=Kind.EXAM,
        title=title,
        when=ItemTime(s, e),
        course=course,
        room=room,
        teacher=teacher,
        status=status,
        subtype=subtype,
        notes=notes,
    )


def cancellation(
    lesson: str = "5521",
    day: date = date(2026, 10, 15),
    start: time = time(10, 15),
    end: time = time(11, 55),
    title: str = "Betriebstechnik",
    teacher: str | None = "Dipl.-Ing. Beispiel",
    room: str | None = "W 1.03",
    status: ItemStatus = ItemStatus.ACTIVE,
) -> SourceItem:
    return SourceItem(
        source=Source.WEBUNTIS,
        source_id=f"lesson:{lesson}:{day.isoformat()}:{start.strftime('%H%M')}",
        kind=Kind.CANCELLATION,
        title=title,
        when=ItemTime(make_local(day, start), make_local(day, end)),
        course=title,
        room=room,
        teacher=teacher,
        status=status,
    )
