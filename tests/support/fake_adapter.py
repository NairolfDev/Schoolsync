"""Scriptbarer Fake-Quelladapter für Engine-/Runner-/Akzeptanztests (implementiert models.SourceAdapter)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from schulsync.errors import ErrorInfo, SchulSyncError
from schulsync.models import (
    Category,
    CategoryOutcome,
    CategoryStatus,
    FetchResult,
    Notice,
    ProbeReport,
    ProbeStep,
    Source,
    SourceItem,
    TrackedRef,
)
from schulsync.timeutil import Window

_DEFAULT_CATEGORIES = {
    Source.EDUVIDUAL: (Category.ASSIGNMENTS,),
    Source.WEBUNTIS: (Category.HOMEWORK, Category.EXAMS, Category.CANCELLATIONS),
}


@dataclass
class FakeAdapter:
    """Liefert genau die Einträge, die der Test setzt; Fehler lassen sich gezielt einspielen.

    - set_items([...]): aktueller Quellbestand (vollständiger Abruf, alle Kategorien OK).
    - fail_category(cat, ErrorInfo | SchulSyncError, status=FAILED): Kategorie scheitert/unvollständig;
      die Einträge dieser Kategorie werden dann NICHT geliefert (wie bei einem echten Teilfehler).
    - login_fails(ErrorInfo): Anmeldung gescheitert (FetchResult.login_error).
    - raise_next(exc, times=1): nächster fetch wirft die Ausnahme (z. B. TransientError/AuthError).
    - set_withdrawn({uid: Beleg}), set_notices([...]).
    - calls: aufgezeichnete (window, tracked, now)-Aufrufe.
    """

    source: Source
    categories: tuple[Category, ...] = ()
    items: list[SourceItem] = field(default_factory=list)
    withdrawn: dict[str, str] = field(default_factory=dict)
    notices: list[Notice] = field(default_factory=list)
    calls: list[tuple[Window, tuple[TrackedRef, ...], datetime]] = field(default_factory=list)
    _failed: dict[Category, tuple[CategoryStatus, ErrorInfo]] = field(default_factory=dict)
    _login_error: ErrorInfo | None = None
    _raise: list[Exception] = field(default_factory=list)
    check_report: ProbeReport | None = None

    def __post_init__(self) -> None:
        if not self.categories:
            self.categories = _DEFAULT_CATEGORIES[self.source]

    # ---- Konfiguration
    def set_items(self, items: Sequence[SourceItem]) -> "FakeAdapter":
        self.items = list(items)
        return self

    def set_withdrawn(self, withdrawn: dict[str, str]) -> "FakeAdapter":
        self.withdrawn = dict(withdrawn)
        return self

    def set_notices(self, notices: Sequence[Notice]) -> "FakeAdapter":
        self.notices = list(notices)
        return self

    def fail_category(
        self, cat: Category, error: ErrorInfo | SchulSyncError, status: CategoryStatus = CategoryStatus.FAILED
    ) -> "FakeAdapter":
        self._failed[cat] = (status, error.to_info() if isinstance(error, SchulSyncError) else error)
        return self

    def heal_category(self, cat: Category) -> "FakeAdapter":
        self._failed.pop(cat, None)
        return self

    def login_fails(self, error: ErrorInfo | SchulSyncError) -> "FakeAdapter":
        self._login_error = error.to_info() if isinstance(error, SchulSyncError) else error
        return self

    def login_ok(self) -> "FakeAdapter":
        self._login_error = None
        return self

    def raise_next(self, exc: Exception, times: int = 1) -> "FakeAdapter":
        self._raise.extend([exc] * times)
        return self

    # ---- SourceAdapter
    def check(self) -> ProbeReport:
        if self.check_report is not None:
            return self.check_report
        return ProbeReport(self.source, [ProbeStep("Anmeldung", self._login_error is None, "Fake")])

    def fetch(self, window: Window, tracked: Sequence[TrackedRef], now: datetime) -> FetchResult:
        self.calls.append((window, tuple(tracked), now))
        if self._raise:
            raise self._raise.pop(0)
        res = FetchResult(source=self.source, fetched_at=now, notices=list(self.notices))
        if self._login_error is not None:
            res.login_error = self._login_error
            for cat in self.categories:
                res.categories[cat] = CategoryOutcome(cat, CategoryStatus.FAILED, self._login_error)
            return res
        failed_cats = set(self._failed)
        for cat in self.categories:
            if cat in self._failed:
                status, err = self._failed[cat]
                res.categories[cat] = CategoryOutcome(cat, status, err)
            else:
                n = sum(1 for i in self.items if i.category == cat)
                res.categories[cat] = CategoryOutcome(cat, CategoryStatus.OK, item_count=n)
        res.items = [i for i in self.items if i.source == self.source and i.category not in failed_cats]
        res.withdrawn = dict(self.withdrawn)
        return res
