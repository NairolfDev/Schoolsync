"""Black-box harness for the acceptance tests (tests/test_acceptance_*.py).

The acceptance tests know the engine only through its documented public surface
(docs/DESIGN_PHASE2.md): `schulsync.runner.run / restore / Deps / RunOutcome`. Everything the engine
could touch is injected here: clock, sleeping, monotonic time, browser opening, network check,
adapters (FakeAdapter) and Google (FakeGoogle).

`runner` is imported lazily (inside `make_deps` / `World.start`), so that test collection works even
while `runner.py` is not there yet.

Process model: one `World.start()` call = one start of the program (fresh `Deps`, fresh adapter/gateway
factory calls) over the SAME `Paths` (same state DB, reports, lock file). The fakes persist across
starts because they stand for the outside world (school platforms, Google).

Inspection helpers read Google through FakeGoogle's accessors and the local state through a separate
READ-ONLY `StateStore` handle on the same DB file.
"""

from __future__ import annotations

import contextlib
import dataclasses
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import pytest

from schulsync import textblocks
from schulsync.config import Config, EduvidualConfig, GoogleConfig, Paths, RulesConfig, WebUntisConfig
from schulsync.errors import NetworkError
from schulsync.google_types import PROP_UID
from schulsync.models import (
    ItemStatus,
    ItemTime,
    Kind,
    RunReport,
    Source,
    SourceItem,
)
from schulsync.state import StateStore
from schulsync.timeutil import UTC
from tests.support import samples
from tests.support.fake_adapter import FakeAdapter
from tests.support.fake_google import FakeGoogle

at = samples.at

# Wednesday, 14.10.2026 08:00 Europe/Vienna (summer time ends 25.10.2026), like tests/conftest.py `now`.
NOW = at(2026, 10, 14, 8, 0)
TODAY = date(2026, 10, 14)

# --------------------------------------------------------------------------- standard sample items
# Realistic anonymised data from tests/support/samples.py (relative to NOW).
ASSIGNMENT = samples.assignment()  # Eduvidual, due Wed 21.10.2026 23:59 -> task 18.10.
HOMEWORK = samples.homework()  # WebUntis, date-only Fri 16.10.2026 -> task would be 13.10. -> "today" 14.10.
EXAM = samples.exam()  # WebUntis, Wed 28.10.2026 08:00-09:40 -> task 21.10.
LESSON = samples.cancellation()  # WebUntis, Thu 15.10.2026 10:15-11:55 -> no task.


class ProcessKilled(BaseException):
    """Simulates the process being killed (power loss, task kill) at an arbitrary point.

    A BaseException on purpose: ordinary `except Exception` handlers of the code under test must not
    be able to swallow it."""


# --------------------------------------------------------------------------- time, sleep, browser, network


class FakeClock:
    """Wall clock + monotonic clock. `sleep` calls advance only the monotonic clock (the wall clock is
    changed explicitly by the test, so "today" cannot move during a run)."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start
        self._mono = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def set(self, value: datetime) -> None:
        self.now = value

    def monotonic(self) -> float:
        return self._mono

    def advance_monotonic(self, seconds: float) -> None:
        self._mono += float(seconds)


@dataclass
class Recorder:
    """Everything the engine did with its injected side-effect hooks."""

    sleeps: list[float] = field(default_factory=list)
    opened: list[Path] = field(default_factory=list)
    network_checks: int = 0
    open_result: bool = True  # what the fake browser-opener answers
    network_script: list[bool] = field(default_factory=list)  # answers consumed first, in order
    network_default: bool = True  # answer once the script is exhausted

    def clear(self) -> None:
        self.sleeps.clear()
        self.opened.clear()
        self.network_checks = 0


# --------------------------------------------------------------------------- config


def configure(cfg: Config, **sections: Mapping[str, Any]) -> Config:
    """`configure(cfg, retry={"max_attempts": 2}, report={"open_on": "never"})` (frozen dataclasses)."""
    for name, changes in sections.items():
        cfg = dataclasses.replace(cfg, **{name: dataclasses.replace(getattr(cfg, name), **changes)})
    return cfg


def make_config(**sections: Mapping[str, Any]) -> Config:
    """Config as after a finished setup: both sources on, calendar/list ids of FakeGoogle's defaults."""
    cfg = Config(
        eduvidual=EduvidualConfig(username="schueler.beispiel"),
        webuntis=WebUntisConfig(username="schueler.beispiel"),
        google=GoogleConfig(calendar_id="cal-schule", tasklist_id="list-schule"),
        rules=RulesConfig(window_days=21, assignment_lead_days=3, assignment_lead_hours=0, assignment_calendar=True),  # the edge tests are written for a 21-day window
    )
    return configure(cfg, **sections)


# --------------------------------------------------------------------------- the Deps factory


def make_deps(
    paths: Paths,
    config: Config,
    adapters: Mapping[Source, Any],
    google: Any,
    clock: FakeClock,
    *,
    rec: Recorder | None = None,
    state: StateStore | None = None,
) -> Any:
    """Build `runner.Deps` with recording hooks.

    `adapters`: Source -> adapter object OR zero-arg factory. `google`: gateway object OR zero-arg
    factory. Pass `rec` to read what happened (a fresh Recorder is used otherwise)."""
    from schulsync.runner import Deps  # lazy: runner.py is written in parallel

    rec = rec if rec is not None else Recorder()

    def as_factory(obj: Any) -> Callable[[], Any]:
        if callable(obj) and not hasattr(obj, "fetch") and not hasattr(obj, "check_targets"):
            return obj
        return lambda: obj

    def sleep(seconds: float) -> None:
        rec.sleeps.append(float(seconds))
        clock.advance_monotonic(seconds)

    def open_report(path: Path) -> bool:
        rec.opened.append(Path(path))
        return rec.open_result

    def network_ready() -> bool:
        rec.network_checks += 1
        return rec.network_script.pop(0) if rec.network_script else rec.network_default

    return Deps(
        paths=paths,
        config=config,
        adapter_factories={src: as_factory(a) for src, a in adapters.items()},
        gateway_factory=as_factory(google),
        clock=clock,
        sleep=sleep,
        monotonic=clock.monotonic,
        open_report=open_report,
        network_ready=network_ready,
        state=state,
    )


class World:
    """One user's PC: paths, config, the two school platforms, Google and a clock."""

    def __init__(
        self,
        paths: Paths,
        config: Config | None = None,
        *,
        google: FakeGoogle | None = None,
        now: datetime = NOW,
    ) -> None:
        paths.ensure()
        self.paths = paths
        self.config = config if config is not None else make_config()
        self.google = google if google is not None else FakeGoogle()
        self.eduvidual = FakeAdapter(Source.EDUVIDUAL)
        self.webuntis = FakeAdapter(Source.WEBUNTIS)
        self.adapters: dict[Source, FakeAdapter] = {Source.EDUVIDUAL: self.eduvidual, Source.WEBUNTIS: self.webuntis}
        self.clock = FakeClock(now)
        self.rec = Recorder()
        self.factory_errors: dict[Source, Exception] = {}  # adapter factory raises (e.g. password missing)
        self.gateway_error: Exception | None = None  # gateway factory raises (e.g. token expired)
        self.gateway_builds = 0

    # ---- configuration helpers
    def configure(self, **sections: Mapping[str, Any]) -> None:
        self.config = configure(self.config, **sections)

    def load_standard(self) -> "World":
        self.eduvidual.set_items([ASSIGNMENT])
        self.webuntis.set_items([HOMEWORK, EXAM, LESSON])
        return self

    # ---- one program start
    def deps(self, **overrides: Any) -> Any:
        def adapter_factory(src: Source) -> Callable[[], FakeAdapter]:
            def make() -> FakeAdapter:
                if src in self.factory_errors:
                    raise self.factory_errors[src]
                return self.adapters[src]

            return make

        def gateway_factory() -> FakeGoogle:
            self.gateway_builds += 1
            if self.gateway_error is not None:
                raise self.gateway_error
            return self.google

        deps = make_deps(
            self.paths,
            self.config,
            {s: adapter_factory(s) for s in self.adapters},
            gateway_factory,
            self.clock,
            rec=self.rec,
        )
        for name, value in overrides.items():
            setattr(deps, name, value)
        return deps

    def start(self, mode: Any = None, *, force: bool = False, at: datetime | None = None, **deps_overrides: Any) -> Any:
        """One program start (default: the automatic start after Windows logon)."""
        from schulsync import runner  # lazy
        from schulsync.models import RunMode

        if at is not None:
            self.clock.set(at)
        return runner.run(self.deps(**deps_overrides), mode if mode is not None else RunMode.AUTO, force=force)

    def start_killed(self, *args: Any, **kwargs: Any) -> Any:
        """A start during which the injected `ProcessKilled` fires. Returns None if it escaped `run()`
        (the normal case: the process is gone), else the outcome (the engine caught it)."""
        try:
            return self.start(*args, **kwargs)
        except ProcessKilled:
            return None

    def preview(self, **kwargs: Any) -> Any:
        from schulsync.models import RunMode

        return self.start(RunMode.PREVIEW, **kwargs)

    def restore(self, uids: Sequence[str] | None = None) -> Any:
        from schulsync import runner  # lazy

        return runner.restore(self.deps(), uids)

    # ---- local state (read-only inspection through a separate handle on the same file)
    @contextlib.contextmanager
    def store(self) -> Iterator[StateStore]:
        st = StateStore(self.paths.db)
        try:
            yield st
        finally:
            st.close()

    def item_state(self, uid: str):
        with self.store() as st:
            return st.get(uid)

    def wipe_state(self) -> None:
        """The state DB is lost (new PC profile, deleted by mistake): Google keeps what was written."""
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.paths.db) + suffix).unlink(missing_ok=True)


@pytest.fixture
def world(paths: Paths) -> World:
    return World(paths)


# --------------------------------------------------------------------------- failure injection helpers


def fail_adapter(adapter: FakeAdapter, exc: Exception | None = None, times: int = 50) -> None:
    """Every fetch of `adapter` raises `exc` (default: a network error), `times` times."""
    adapter.raise_next(exc if exc is not None else NetworkError("Keine Verbindung (Test)."), times=times)


def heal_adapter(adapter: FakeAdapter) -> None:
    """Remove all injected failures. FakeAdapter has no public 'clear raises', so touch its fields."""
    adapter._raise.clear()  # noqa: SLF001
    adapter._failed.clear()  # noqa: SLF001
    adapter._login_error = None  # noqa: SLF001


class ListFailsAfterInsertGoogle(FakeGoogle):
    """FakeGoogle whose `list_tasks` answers a network error after an `insert_task` that raised
    (typically a lost response). Models "outcome unknown AND the reconciliation list is not loadable":
    a correct engine must not create a second task on suspicion."""

    def __init__(self, *args: Any, list_failures: int = 100, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._list_failures = list_failures
        self._armed = 0

    def insert_task(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return super().insert_task(body)
        except BaseException:
            self._armed = self._list_failures
            raise

    def list_tasks(self) -> list[dict[str, Any]]:
        if self._armed > 0:
            self._armed -= 1
            self.calls["list_tasks"] += 1
            raise NetworkError("Aufgabenliste nicht ladbar (Test).")
        return super().list_tasks()

    def heal_list(self) -> None:
        self._armed = 0


# --------------------------------------------------------------------------- Google inspection


def events_for(google: FakeGoogle, uid: str) -> list[dict[str, Any]]:
    """Live (not deleted) calendar events that carry the project uid."""
    return [e for e in google.events.values() if (e.get("extendedProperties") or {}).get("private", {}).get(PROP_UID) == uid]


def event_for(google: FakeGoogle, uid: str) -> dict[str, Any]:
    found = events_for(google, uid)
    assert len(found) == 1, f"expected exactly one live event for {uid}, found {len(found)}: {found}"
    return found[0]


def tasks_for(google: FakeGoogle, uid: str) -> list[dict[str, Any]]:
    """All tasks (open, completed, hidden, deleted) whose notes carry the marker of `uid`."""
    return [t for t in google.tasks if textblocks.find_task_marker(t.get("notes")) == uid]


def open_tasks_for(google: FakeGoogle, uid: str) -> list[dict[str, Any]]:
    return [t for t in tasks_for(google, uid) if t["status"] == "needsAction" and not t.get("deleted")]


def task_for(google: FakeGoogle, uid: str) -> dict[str, Any]:
    found = tasks_for(google, uid)
    assert len(found) == 1, f"expected exactly one task (any state) for {uid}, found {len(found)}: {found}"
    return found[0]


def when_of(rec: dict[str, Any]) -> datetime | date:
    """Google start/end dict -> aware datetime (exact instant, offset taken from the string) or date."""
    if "dateTime" in rec:
        parsed = datetime.fromisoformat(rec["dateTime"])
        assert parsed.tzinfo is not None, f"dateTime without offset: {rec}"
        return parsed
    return date.fromisoformat(rec["date"])


def event_span(ev: dict[str, Any]) -> tuple[datetime | date, datetime | date]:
    return when_of(ev["start"]), when_of(ev["end"])


def same_instant(a: datetime, b: datetime) -> bool:
    """Instants compared via UTC (never wall clock: DST overlap)."""
    return a.astimezone(UTC) == b.astimezone(UTC)


def due_of(task: dict[str, Any]) -> date:
    return date.fromisoformat(task["due"][:10])


def snapshot(google: FakeGoogle) -> tuple[Any, Any, Any]:
    return google.events, google.tasks, google.primary_events


def write_ops(google: FakeGoogle) -> list[str]:
    return [op for op, _id, _body in google.write_log]


def description_of(ev: dict[str, Any]) -> str:
    return ev.get("description") or ""


def is_cancelled_event(ev: dict[str, Any]) -> bool:
    rem = ev.get("reminders") or {}
    return ev["summary"].startswith("Abgesagt:") and not rem.get("overrides") and rem.get("useDefault") is False


# --------------------------------------------------------------------------- report / outcome helpers


def notice_codes(report: RunReport) -> set[str]:
    return {n.code for n in report.notices}


def skipped(report: RunReport, source: Source, reason: str) -> int:
    """Skips with `reason` for `source`, summed over the calendar and the task counters."""
    res = report.sources[source]
    return res.calendar.skipped_reasons.get(reason, 0) + res.tasks.skipped_reasons.get(reason, 0)


def read_report(outcome: Any) -> str:
    assert outcome.report_path is not None, "the run produced no report file"
    return Path(outcome.report_path).read_text(encoding="utf-8")


def all_log_text(paths: Paths) -> str:
    chunks: list[str] = []
    if paths.logs.is_dir():
        for f in sorted(paths.logs.rglob("*")):
            if f.is_file():
                chunks.append(f.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def hrefs(html_text: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', html_text)


# --------------------------------------------------------------------------- item builders


def lesson_between(
    start: datetime,
    end: datetime,
    *,
    lesson: str = "9001",
    title: str = "Betriebstechnik",
    status: ItemStatus = ItemStatus.ACTIVE,
) -> SourceItem:
    """A WebUntis cancelled lesson with arbitrary start/end (samples.cancellation takes day + times)."""
    return SourceItem(
        source=Source.WEBUNTIS,
        source_id=f"lesson:{lesson}:{start:%Y-%m-%d}:{start:%H%M}",
        kind=Kind.CANCELLATION,
        title=title,
        when=ItemTime(start, end),
        course=title,
        room="W 1.03",
        teacher="Dipl.-Ing. Beispiel",
        status=status,
    )


def exam_start_only(start: datetime | date, *, eid: str = "30999", title: str = "Test Elektronik Grundlagen") -> SourceItem:
    """An exam for which only the start is known (no end time)."""
    return SourceItem(
        source=Source.WEBUNTIS,
        source_id=f"exam:{eid}",
        kind=Kind.EXAM,
        title=title,
        when=ItemTime(start, None),
        course="Elektronik",
        room="E 0.07",
        teacher="Dipl.-Ing. Beispiel",
        subtype="Test",
    )


def moved(item: SourceItem, start: datetime | date, end: datetime | None = None) -> SourceItem:
    """The same source item (same stable id) on a new date/time."""
    return dataclasses.replace(item, when=ItemTime(start, end))


def cancelled(item: SourceItem) -> SourceItem:
    """The same item as explicitly cancelled/withdrawn by the source."""
    return dataclasses.replace(item, status=ItemStatus.CANCELLED)


def renamed(item: SourceItem, title: str) -> SourceItem:
    return dataclasses.replace(item, title=title)

