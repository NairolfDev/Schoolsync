"""Shared rigs for the engine tests (`Rig`, engine level) and the runner tests (`Env`, runner level).

Everything is deterministic: time, sleeping, monotonic clock, browser and network check are injected; Google is
`FakeGoogle`, the school platforms are `FakeAdapter`s. Nothing here reads the real clock or touches the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from schulsync.config import Config, Paths, RulesConfig
from schulsync.models import FetchResult, Source, SourceItem
from schulsync.runner import Deps, RunOutcome
from schulsync.state import StateStore
from schulsync.sync import SyncEngine
from schulsync.timeutil import VIENNA, fetch_window
from tests.support.fake_adapter import FakeAdapter
from tests.support.fake_google import FakeGoogle

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA)  # Wednesday; summer time ends on 25.10.2026
EDU, WU = Source.EDUVIDUAL, Source.WEBUNTIS


class Rig:
    """Engine-level rig: one in-memory state, one FakeGoogle, one FakeAdapter per source."""

    def __init__(self, now: datetime = NOW, cfg: Config | None = None, **google_kw: Any) -> None:
        self.now = now
        self.cfg = cfg or Config(rules=RulesConfig(window_days=21, assignment_lead_days=3, assignment_lead_hours=0, assignment_calendar=True))
        self.store = StateStore(":memory:")
        self.google = FakeGoogle(**google_kw)
        self.adapters = {s: FakeAdapter(s) for s in Source}

    def engine(self, *, preview: bool = False, gateway: Any = None, **kw: Any) -> SyncEngine:
        gw = gateway if gateway is not None else self.google
        return SyncEngine(self.store, self.cfg, lambda: gw, preview=preview, **kw)

    def fetch(self, source: Source, items: Sequence[SourceItem], now: datetime | None = None) -> FetchResult:
        adapter = self.adapters[source]
        adapter.set_items(items)
        n = now or self.now
        return adapter.fetch(fetch_window(n, self.cfg.rules.window_days), [], n)

    def sync(
        self,
        *items: SourceItem,
        now: datetime | None = None,
        sources: Sequence[Source] = tuple(Source),
        engine: SyncEngine | None = None,
        preview: bool = False,
        write: bool = True,
    ) -> SyncEngine:
        """Ingest `items` (grouped by source; a source without items is fetched EMPTY and complete) and write."""
        n = now or self.now
        eng = engine or self.engine(preview=preview)
        for src in sources:
            eng.ingest(self.fetch(src, [i for i in items if i.source == src], n), n)
        if write:
            eng.write_phase(sources, n)
        return eng

    def write_again(self, *, now: datetime | None = None, sources: Sequence[Source] = tuple(Source), **kw: Any) -> SyncEngine:
        """A fresh engine that only runs the write phase (retry of whatever is still pending)."""
        eng = self.engine(**kw)
        eng.write_phase(sources, now or self.now)
        return eng

    def st(self, item_or_uid: SourceItem | str):
        return self.store.get(item_or_uid if isinstance(item_or_uid, str) else item_or_uid.uid)


class Env:
    """Runner-level rig. `run()` builds `Deps` from the injected fakes; the clock, sleep and browser are recorded."""

    def __init__(self, tmp_path: Path, now: datetime = NOW, cfg: Config | None = None, **google_kw: Any) -> None:
        self.paths = Paths(tmp_path / "SchulSync")
        self.paths.ensure()
        self.cfg = cfg or Config(rules=RulesConfig(window_days=21, assignment_lead_days=3, assignment_lead_hours=0, assignment_calendar=True))
        self.now = now
        self.mono = 0.0
        self.sleeps: list[float] = []
        self.opened: list[Path] = []
        self.open_result = True
        self.network_answers: list[bool] = []  # popped front first; empty -> ready
        self.network_checks = 0
        self.google = FakeGoogle(**google_kw)
        self.adapters = {s: FakeAdapter(s) for s in Source}
        self.factory_errors: dict[Source, Exception] = {}
        self.gateway_error: Exception | None = None
        self.state = StateStore(self.paths.db)

    # ---- injected callables
    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += seconds

    def monotonic(self) -> float:
        return self.mono

    def open_report(self, path: Path) -> bool:
        self.opened.append(path)
        return self.open_result

    def network_ready(self) -> bool:
        self.network_checks += 1
        return self.network_answers.pop(0) if self.network_answers else True

    def _adapter_factory(self, source: Source) -> Callable[[], Any]:
        def make() -> Any:
            if source in self.factory_errors:
                raise self.factory_errors[source]
            return self.adapters[source]

        return make

    def _gateway_factory(self) -> Any:
        if self.gateway_error is not None:
            raise self.gateway_error
        return self.google

    def deps(self, **overrides: Any) -> Deps:
        kw: dict[str, Any] = dict(
            paths=self.paths,
            config=self.cfg,
            adapter_factories={s: self._adapter_factory(s) for s in Source},
            gateway_factory=self._gateway_factory,
            clock=self.clock,
            sleep=self.sleep,
            monotonic=self.monotonic,
            open_report=self.open_report,
            network_ready=self.network_ready,
            state=self.state,
        )
        kw.update(overrides)
        return Deps(**kw)

    # ---- actions
    def set_items(self, *items: SourceItem) -> None:
        for src in Source:
            self.adapters[src].set_items([i for i in items if i.source == src])

    def run(self, mode: Any = None, *, force: bool = False, **deps_overrides: Any) -> RunOutcome:
        from schulsync import runner
        from schulsync.models import RunMode

        return runner.run(self.deps(**deps_overrides), mode or RunMode.AUTO, force=force)

    def restore(self, uids: Sequence[str] | None = None) -> RunOutcome:
        from schulsync import runner

        return runner.restore(self.deps(), uids)

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)

    def fetch_calls(self, source: Source) -> int:
        return len(self.adapters[source].calls)
