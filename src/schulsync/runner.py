"""Daily run (DESIGN section 7) and explicit restore: lock, day gate, network wait, per-source fetch with bounded
retries, ingest, Google write phase, report, browser, preview.

Public API (docs/DESIGN_PHASE2.md): :class:`Deps`, :class:`RunOutcome`, :func:`run`, :func:`restore`,
:func:`default_network_ready`. Time, sleeping, the monotonic clock, the browser and the network check are injected
through :class:`Deps`, so every behaviour is testable without waiting.

Behaviour in short
------------------
* Process lock first. Locked: AUTO ends silently (exit 0, one log line); MANUAL/PREVIEW answer "läuft bereits" (exit 1).
* Day gate (only AUTO and not ``force``), per time slot (00:00, then each of ``config.RUN_TIMES``): a source is *done*
  when the fetch within the current slot was complete AND its writes were
  recorded AND nothing is pending; *writes only* when today's fetch was complete but writes are missing (no
  refetch); otherwise it is fetched. All sources done -> silent exit, no report, no browser. MANUAL runs and
  ``force`` always fetch. Only today counts: there are no "catch-up" runs for missed days.
* Per source, independently: the fetch is retried only on ``TransientError`` (``retry.max_attempts``, waiting
  ``retry.delays_seconds`` through ``deps.sleep``). ``AuthError`` or a rejected login is never retried and blocks the
  source for AUTO runs (``set_auth_blocked``) until a MANUAL run / ``check`` / ``setup`` succeeds. A ``ConfigError``
  from the adapter factory (e.g. missing password) fails the source with a 'schulsync setup' hint but blocks nothing.
  ``retry.run_timeout_seconds`` (``deps.monotonic``) is checked between steps; a call that is already running is
  limited only by the adapters' own HTTP time limits.
* After the fetch of a source the engine ingests it; after all sources the Google write phase runs. A source is
  recorded as fetched (``record_fetch``) and, if nothing is pending, as written (``record_writes_done``).
* The report file is ALWAYS written (also for failures, preview and crashes) and old reports/logs/rows are pruned.
  The browser opens only for ``needs_attention`` (or warnings with ``open_on = "warnings"``), once per fingerprint
  and day (the tab flood protection also holds if the opening failed: only a successful opening is recorded).
* PREVIEW works on an in-memory copy of the database with a DryRunGateway: the real database gets no ``record_*``,
  no auth block, no ``mark_report_opened``, no pruning, and Google receives no write.
* Unexpected exceptions become a FAILED report (code ``unexpected``, redacted detail) instead of a crash; a SchulSyncError
  that ends the run is logged with its code and message (redacted) as well.
* Changed Google targets (other calendar / task list ids in the configuration than the stored link state belongs to) are
  handled FIRST, before the day gate looks at pending work: all items are reset and written into the new targets (H2).
* If the HTML report cannot be written, the path it was meant for is logged and a plain-text copy goes next to the logs
  (``logs/bericht-<day>.txt``); that file is then opened like the report. If even that fails, the run only logs.
* The "report opened" memory (tab flood protection) is the state database (``report_opened``, schema v2: several
  fingerprints per day). Without a usable database nothing is remembered here (the CLI guards that case, see cli._cmd_run).

Exit codes: 0 success / nothing to do / silent exit, 1 partial failure or failure, 2 configuration or call error.
"""

from __future__ import annotations

import hashlib
import logging
import socket
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar
from urllib.parse import urlsplit

from . import logging_setup, notify as notify_mod, report as report_mod, rules
from .config import RUN_TIMES, Config, Paths
from .dryrun import DryRunGateway
from .errors import AuthError, ConfigError, ErrorInfo, SchulSyncError, TransientError
from .google_types import GoogleGateway
from .lock import ProcessLock
from .models import (
    Category,
    CategoryStatus,
    ChangeStatus,
    Counts,
    EventState,
    FetchResult,
    Notice,
    OverallStatus,
    RunMode,
    RunReport,
    Severity,
    Source,
    SourceAdapter,
    SourceRunResult,
    SourceStatus,
    TargetStatus,
    TaskState,
    TrackedRef,
)
from .redact import redact
from .state import StateStore
from .sync import RestoreResult, SyncEngine
from .timeutil import UTC, fetch_window, now_vienna, slot_start, today_vienna

__all__ = ["Deps", "RunOutcome", "run", "restore", "default_network_ready", "network_hosts"]

log = logging.getLogger("schulsync.runner")
T = TypeVar("T")

# Login errors with these codes are NOT a verdict on the credentials: transient ones are retried like a raised
# TransientError, every other code fails the source for this start without blocking it. Only a rejected login
# ('auth') or a permanent refusal ('access_denied', e.g. terms not accepted) blocks automatic starts: a maintenance
# page (HTTP 404), an unexpected status or a changed page says nothing about the password.
_TRANSIENT_CODES = frozenset({"network", "transient", "outcome_unknown"})
_BLOCKING_CODES = frozenset({"auth", "access_denied"})
_DEFAULT_HOSTS = ("www.googleapis.com", "www.eduvidual.at", "htbla-grieskirchen.webuntis.com")
_SOURCE_NAME = {Source.EDUVIDUAL: "Eduvidual", Source.WEBUNTIS: "WebUntis"}
_OVERALL_DE = {
    OverallStatus.SUCCESS: "Erfolgreich",
    OverallStatus.PARTIAL: "Teilweise fehlgeschlagen",
    OverallStatus.FAILED: "Fehlgeschlagen",
    OverallStatus.NOTHING_TO_DO: "Nichts zu tun",
}
_SOURCE_DE = {
    SourceStatus.SUCCESS: "Erfolgreich",
    SourceStatus.PARTIAL: "Teilweise fehlgeschlagen",
    SourceStatus.FAILED: "Fehlgeschlagen",
    SourceStatus.ALREADY_DONE: "Heute bereits erledigt",
    SourceStatus.DISABLED: "Abgeschaltet",
}
_MODE_DE = {RunMode.AUTO: "automatisch", RunMode.MANUAL: "manuell", RunMode.PREVIEW: "Vorschau"}
_ACTION_DE = {
    "create": "Anlegen",
    "update": "Aktualisieren",
    "cancel": "Als abgesagt markieren",
    "reactivate": "Reaktivieren",
    "remove_task": "Offene Aufgabe entfernen",
    "adopt": "Vorhandenen Eintrag übernehmen",
    "restore": "Wiederherstellen",
    "skip": "Überspringen",
    "complete": "Abhaken (in Eduvidual abgegeben)",
    "remove_event": "Kalendertermin entfernen (nur noch als Task)",
}
_TARGET_DE = {"calendar": "Kalender", "tasks": "Aufgabenliste"}
_STATUS_DE = {
    ChangeStatus.APPLIED: "erledigt",
    ChangeStatus.PLANNED: "geplant",
    ChangeStatus.FAILED: "fehlgeschlagen",
    ChangeStatus.DEFERRED: "zurückgestellt",
    ChangeStatus.UNCERTAIN: "Ausgang ungeklärt",
}

# per-source modes of one run
DISABLED, DONE, WRITES, FETCH, BLOCKED, RESTORE = "disabled", "done", "writes", "fetch", "blocked", "restore"


# --------------------------------------------------------------------------- network check


def network_hosts(config: Config) -> tuple[str, ...]:
    """Hosts of the enabled sources plus Google (for the network check)."""
    hosts = ["www.googleapis.com"]
    if config.eduvidual.enabled:
        hosts.append(urlsplit(config.eduvidual.base_url).hostname or "")
    if config.webuntis.enabled:
        hosts.append(urlsplit(config.webuntis.base_url).hostname or "")
    return tuple(dict.fromkeys(h for h in hosts if h))


def default_network_ready(hosts: Sequence[str] | None = None, *, timeout: float = 3.0) -> bool:
    """True as soon as ONE host answers (DNS + TCP 443): then the internet is up. Per-host trouble is the job of
    the retries. `hosts=None` uses the default school hosts and Google (see `network_hosts` for the configured ones)."""
    for host in hosts or _DEFAULT_HOSTS:
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------- public types


@dataclass
class Deps:
    paths: Paths
    config: Config
    adapter_factories: Mapping[Source, Callable[[], SourceAdapter]]
    gateway_factory: Callable[[], GoogleGateway]
    clock: Callable[[], datetime] = now_vienna
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    open_report: Callable[[Path], bool] = report_mod.open_report
    network_ready: Callable[[], bool] = default_network_ready
    state: StateStore | None = None
    notify: Callable[[str, str], bool] = lambda title, text: notify_mod.toast(title, text)  # looked up at call time (tests patch it)


@dataclass
class RunOutcome:
    exit_code: int  # 0 success / nothing to do / silent, 1 partial failure or failure, 2 configuration or call error
    report: RunReport | None  # None for a silent exit / lock / nothing to restore
    report_path: Path | None
    console_text: str  # short German summary ("" if silent)
    silent: bool  # True: print nothing (day gate / lock in AUTO mode)


def run(deps: Deps, mode: RunMode, *, force: bool = False) -> RunOutcome:
    """One run (AUTO / MANUAL / PREVIEW). Never raises for failures of the run itself."""
    lock = ProcessLock(deps.paths.lock)
    try:
        acquired = lock.acquire(blocking=False)
    except OSError as exc:  # lock file not creatable: do not run unprotected
        return RunOutcome(2, None, None, f"Die Prozesssperre konnte nicht angelegt werden ({type(exc).__name__}).", False)
    if not acquired:
        return _locked(mode)
    try:
        return _Run(deps, mode, force).execute()
    finally:
        lock.release()


def restore(deps: Deps, uids: Sequence[str] | None) -> RunOutcome:
    """Explicitly restore deliberately deleted Google entries (None = all) and write them again."""
    lock = ProcessLock(deps.paths.lock)
    try:
        acquired = lock.acquire(blocking=False)
    except OSError as exc:
        return RunOutcome(2, None, None, f"Die Prozesssperre konnte nicht angelegt werden ({type(exc).__name__}).", False)
    if not acquired:
        return _locked(RunMode.MANUAL)
    try:
        return _Run(deps, RunMode.MANUAL, True, restore_uids=uids, is_restore=True).execute()
    finally:
        lock.release()


def _locked(mode: RunMode) -> RunOutcome:
    if mode == RunMode.AUTO:
        log.info("Ein anderer SchulSync-Lauf ist aktiv; dieser Start endet still.")
        return RunOutcome(0, None, None, "", True)
    return RunOutcome(
        1, None, None, "SchulSync läuft bereits. Bitte warten, bis der andere Lauf beendet ist, und dann erneut versuchen.", False
    )


# --------------------------------------------------------------------------- the run


class _Run:
    def __init__(
        self,
        deps: Deps,
        mode: RunMode,
        force: bool,
        *,
        is_restore: bool = False,
        restore_uids: Sequence[str] | None = None,
    ) -> None:
        self.deps, self.mode, self.force = deps, mode, force
        self.cfg = deps.config
        self.is_restore, self.restore_uids = is_restore, restore_uids
        self.preview = mode == RunMode.PREVIEW
        self.started = deps.clock()
        self.day: date = today_vienna(self.started)
        self.slot = slot_start(self.started, RUN_TIMES)
        self.t0 = deps.monotonic()
        enabled = set(self.cfg.enabled_sources)
        self.sources = [s for s in Source if s.value in enabled]
        self.shown: list[Source] = list(Source)  # sources that appear in the report
        self.modes: dict[Source, str] = {}
        self.res: dict[Source, SourceRunResult] = {s: SourceRunResult(s, SourceStatus.FAILED) for s in Source}
        self.fetches: dict[Source, FetchResult] = {}  # usable results (a fetch happened)
        self.failed: set[Source] = set()  # fetch failed / source skipped
        self.notices: list[Notice] = []
        self.next_steps: list[str] = []
        self.crash: ErrorInfo | None = None
        self.restore_result: RestoreResult | None = None
        self.real: StateStore | None = None  # the real database
        self.work: StateStore | None = None  # == real, or an in-memory copy (preview)
        self.owns_real = False
        self.engine: SyncEngine | None = None
        self.dry: DryRunGateway | None = None
        self.preview_google_error: ErrorInfo | None = None

    # ---- frame

    def execute(self) -> RunOutcome:
        try:
            self._open_state()
            return self._restore_body() if self.is_restore else self._body()
        except Exception as exc:  # noqa: BLE001 - a run must end with a report, not with a traceback
            return self._crash(exc)
        finally:
            self._close()

    def _open_state(self) -> None:
        self.deps.paths.ensure()
        if self.deps.state is not None:
            self.real = self.deps.state
        else:
            self.real = StateStore(self.deps.paths.db)
            self.owns_real = True
        self.work = self.real.copy_to_memory() if self.preview else self.real

    def _close(self) -> None:
        for store in {id(s): s for s in (self.work, self.real) if s is not None}.values():
            if store is self.real and not self.owns_real:
                continue
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

    def _crash(self, exc: Exception) -> RunOutcome:
        if isinstance(exc, SchulSyncError):
            self.crash = exc.to_info()
            code = 2 if isinstance(exc, ConfigError) else 1
            log.error(
                "Lauf abgebrochen (%s): %s %s",
                self.crash.code,
                redact(self.crash.message),
                redact(self.crash.detail or ""),
            )
        else:
            log.exception("Unerwarteter Fehler im Lauf")
            detail = redact(f"{type(exc).__name__}: {exc}")[:500]
            self.crash = ErrorInfo(
                "unexpected",
                "Im Programm ist ein unerwarteter Fehler aufgetreten.",
                hint="Der Fehler steht in der Logdatei. Der nächste Start versucht es erneut.",
                detail=detail,
            )
            code = 1
        try:
            return self._publish(self._assemble(), exit_code=code)
        except Exception:  # noqa: BLE001
            log.exception("Der Fehlerbericht konnte nicht erstellt werden")
            return RunOutcome(code, None, None, f"SchulSync: {self.crash.message} ({self.crash.code})", False)

    # ---- helpers

    def _remaining(self) -> float:
        """Seconds left until the hard run limit (`retry.run_timeout_seconds`, measured with `deps.monotonic`)."""
        return self.cfg.retry.run_timeout_seconds - (self.deps.monotonic() - self.t0)

    def _timed_out(self) -> bool:
        return self._remaining() <= 0

    def _delay(self, attempt: int) -> float:
        delays = self.cfg.retry.delays_seconds
        return delays[attempt - 1] if attempt - 1 < len(delays) else (delays[-1] if delays else 0.0)

    def _call_with_retries(self, fn: Callable[[], T]) -> T:
        """Bounded retries with growing waits; ONLY `TransientError` is retried."""
        attempts = max(1, self.cfg.retry.max_attempts)
        n = 1
        while True:
            try:
                return fn()
            except TransientError:
                delay = self._delay(n)
                if n >= attempts or delay >= self._remaining():  # never sleep past the hard limit
                    raise
                self.deps.sleep(delay)
                n += 1

    def _wait_network(self) -> None:
        limit = self.cfg.retry.network_wait_seconds
        start, step = self.deps.monotonic(), 2.0
        while not self._network_ready():
            waited = self.deps.monotonic() - start  # elapsed time incl. the probes themselves (their timeouts count)
            if waited >= limit or self._remaining() <= 0:
                log.warning("Kein Internet nach %.0f Sekunden; der Lauf versucht es trotzdem.", waited)
                return
            self.deps.sleep(min(step, limit - waited, self._remaining()))
            step = min(step * 2, 15.0)

    def _network_ready(self) -> bool:
        try:
            return bool(self.deps.network_ready())
        except Exception as exc:  # noqa: BLE001 - a broken probe must not stop the run; the attempts decide
            log.warning("Die Netzwerkprüfung ist fehlgeschlagen (%s); der Lauf versucht es trotzdem.", type(exc).__name__)
            return True

    def _safe(self, fn: Callable[[], Any], what: str) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - housekeeping must never fail the run
            log.warning("%s fehlgeschlagen: %s", what, type(exc).__name__)
            return None

    def _timeout_info(self) -> ErrorInfo:
        return ErrorInfo(
            "run_timeout", "Das Zeitlimit für den Lauf wurde erreicht.", hint="Der Rest wird beim nächsten Start nachgeholt."
        )

    # ---- normal run

    def _body(self) -> RunOutcome:
        assert self.work is not None
        gate = self.mode == RunMode.AUTO and not self.force
        self.engine = SyncEngine(
            self.work,
            self.cfg,
            self._gateway,
            preview=self.preview,
            retry=self._call_with_retries,
            should_stop=self._timed_out,
            contain_errors=True,  # a bug triggered by one item must not keep the others out of Google
        )
        # New targets (the user re-created the calendar / list) mean pending work for every item, also on a day that was
        # already finished: reset BEFORE the day gate looks at it. A preview resets only its in-memory copy.
        self.engine.reset_targets_if_changed(self.deps.clock())
        self._classify(gate)
        if gate and all(self.modes[s] in (DONE, DISABLED) for s in Source):
            log.info("Heute ist bereits alles erledigt; dieser Start endet still.")
            return RunOutcome(0, None, None, "", True)
        if not self.sources:
            return RunOutcome(0, None, None, "Es ist keine Quelle aktiviert (config.toml).", False)
        needs_net = any(
            self.modes[s] == FETCH or (self.modes[s] == WRITES and self.work.pending_count(s) > 0) for s in self.sources
        )
        if self.mode == RunMode.AUTO and needs_net:
            self._wait_network()
        now = self.deps.clock()
        for s in self.sources:
            if self.modes[s] == FETCH:
                self._fetch_source(s, now)
            elif self.modes[s] == BLOCKED:
                self._blocked(s)
        self.engine.write_phase([s for s in self.sources if self.modes[s] != DONE], now)
        if self.dry is not None and self.dry.reads_unavailable:
            # the real reason first (expired login, set-up missing, target gone ...), never a guess
            reasons = [e for e in (self.preview_google_error, *self.dry.read_errors) if e is not None]
            extra = " ".join(dict.fromkeys(t for e in reasons for t in (e.message, e.hint) if t))
            extra = extra or "Es ist keine Google-Anmeldung vorhanden."
            self.notices.append(
                Notice(
                    Severity.WARNING,
                    "preview_incomplete",
                    f"Die Vorschau konnte Google nicht vollständig lesen und beruht auf unvollständigem Wissen. {extra}".strip(),
                )
            )
        if not self.preview:
            self._record(now)
        return self._publish(self._assemble())

    def _classify(self, gate: bool) -> None:
        assert self.work is not None
        for s in Source:
            if s not in self.sources:
                self.modes[s] = DISABLED
                continue
            sd = self.work.get_source_day(self.day, s)
            if sd is not None and (sd.fetched_at is None or sd.fetched_at.astimezone(UTC) < self.slot.astimezone(UTC)):
                sd = None  # fetched before the current time slot (06:00 / 18:00): fetch again
            if gate and sd is not None and sd.done and self.work.pending_count(s) == 0:
                self.modes[s] = DONE
            elif gate and sd is not None and sd.fetch_complete:
                self.modes[s] = WRITES  # fetched today; only the Google writes are missing -> no refetch
            elif self.mode == RunMode.AUTO and self.work.is_auth_blocked(s):
                self.modes[s] = BLOCKED
            else:
                self.modes[s] = FETCH

    def _gateway(self) -> GoogleGateway:
        if not self.preview:
            return self.deps.gateway_factory()
        try:
            inner: GoogleGateway | None = self.deps.gateway_factory()
        except SchulSyncError as exc:
            self.preview_google_error = exc.to_info()
            inner = None
        self.dry = DryRunGateway(inner)
        return self.dry

    # ---- fetching

    def _tracked(self, source: Source, now: datetime) -> list[TrackedRef]:
        assert self.work is not None
        return [
            TrackedRef(source, st.item.kind, st.item.source_id, st.item.when.start, st.item.when.end, st.cancelled)
            for st in self.work.items_for_source(source)
            if not rules.is_fully_past(st.item, now)
        ]

    def _source_failed(self, source: Source, info: ErrorInfo, now: datetime) -> None:
        self.res[source].errors.append(info)
        self.failed.add(source)
        if not self.preview:
            assert self.work is not None
            self._safe(lambda: self.work.record_attempt(source, now, info.code), "Versuch speichern")  # type: ignore[union-attr]
        log.warning("%s: %s (%s)", _SOURCE_NAME[source], info.message, info.code)

    def _fetch_source(self, source: Source, now: datetime) -> None:
        """One source, isolated: whatever goes wrong inside (a parser bug, an unreadable secret store, a hostile text that
        the ingest cannot store) fails THIS source only; the other source is still fetched and written (DESIGN 7.4)."""
        name = _SOURCE_NAME[source]
        try:
            self._fetch_source_body(source, now)
        except Exception as exc:  # noqa: BLE001
            log.exception("Unerwarteter Fehler bei %s", name)
            self.fetches.pop(source, None)  # nothing was ingested (the ingest transaction is rolled back): no day success
            info = ErrorInfo(
                "unexpected",
                f"{name}: Im Programm ist ein unerwarteter Fehler aufgetreten.",
                hint="Der Fehler steht in der Logdatei. Der nächste Start versucht es erneut.",
                detail=redact(f"{type(exc).__name__}: {exc}")[:500],
            )
            self._source_failed(source, info, now)
            self.next_steps.append(f"{name}: Der Fehler wurde in der Logdatei (Ordner 'logs') festgehalten; der nächste Start versucht es erneut.")

    def _fetch_source_body(self, source: Source, now: datetime) -> None:
        assert self.work is not None and self.engine is not None
        name = _SOURCE_NAME[source]
        if self._timed_out():
            self._source_failed(source, self._timeout_info(), now)
            self.next_steps.append("Der Lauf hat das Zeitlimit erreicht; die übrigen Quellen folgen beim nächsten Start.")
            return
        factory = self.deps.adapter_factories.get(source)
        try:
            if factory is None:
                raise ConfigError(f"Für {name} ist kein Zugriff eingerichtet.")
            adapter = factory()
        except SchulSyncError as exc:
            info = exc.to_info()
            if "schulsync setup" not in (info.hint or ""):
                hint = f"Bitte 'schulsync setup --{source.value}' ausführen."
                info = ErrorInfo(info.code, info.message, f"{info.hint} {hint}" if info.hint else hint, info.detail)
            self._source_failed(source, info, now)  # no auth block: the credentials were never tried
            self.next_steps.append(f"{name}: 'schulsync setup --{source.value}' ausführen (Zugangsdaten fehlen oder sind unvollständig).")
            return
        tracked = self._tracked(source, now)
        window = fetch_window(now, self.cfg.rules.window_days)

        def attempt() -> FetchResult:
            result = adapter.fetch(window, tracked, now)
            le = result.login_error
            if le is not None and le.code in _TRANSIENT_CODES:
                raise TransientError(le.message, hint=le.hint, detail=le.detail)
            return result

        try:
            result = self._call_with_retries(attempt)
        except AuthError as exc:
            self._auth_failed(source, exc.to_info(), now)
            return
        except SchulSyncError as exc:
            self._source_failed(source, exc.to_info(), now)
            return
        if result.login_error is not None:
            if result.login_error.code in _BLOCKING_CODES:
                # A rejected login is ONE problem: the categories it made fail are not listed (they would only repeat it),
                # exactly like a later, blocked start of the day that never fetches. Both share one report fingerprint,
                # so the second logon of the day opens no second tab.
                self._auth_failed(source, result.login_error, now)
            else:  # structural or unexpected (changed page, HTTP 404, ...): this start fails, the next one tries again
                self.res[source].categories = dict(result.categories)
                self._source_failed(source, result.login_error, now)
            return
        if not self.preview:
            assert self.real is not None
            self._safe(lambda: self.real.record_attempt(source, now, None), "Versuch speichern")  # type: ignore[union-attr]
            if self.real.is_auth_blocked(source):
                self._safe(lambda: self.real.clear_auth_block(source), "Anmeldesperre aufheben")  # type: ignore[union-attr]
        self.res[source].categories = dict(result.categories)
        self.engine.ingest(result, now)
        self.fetches[source] = result  # only after a successful ingest: it decides about the day's success of the source

    def _auth_failed(self, source: Source, info: ErrorInfo, now: datetime) -> None:
        self._source_failed(source, info, now)
        if not self.preview:
            assert self.real is not None
            self._safe(lambda: self.real.set_auth_blocked(source, now, info.code), "Anmeldesperre setzen")  # type: ignore[union-attr]
        name = _SOURCE_NAME[source]
        self.next_steps.append(
            f"{name}: Anmeldedaten prüfen und 'schulsync setup --{source.value}' ausführen. Automatische Läufe überspringen {name}, "
            "bis die Anmeldung wieder funktioniert; ein manueller Lauf ('schulsync run') versucht sie erneut."
        )

    def _blocked(self, source: Source) -> None:
        name = _SOURCE_NAME[source]
        self.failed.add(source)
        self.res[source].errors.append(
            ErrorInfo(
                "auth",
                f"Die Anmeldung bei {name} wurde abgelehnt; automatische Läufe überspringen diese Quelle.",
                hint=f"Zugangsdaten prüfen und 'schulsync setup --{source.value}' ausführen; 'schulsync check' oder ein manueller Lauf versucht die Anmeldung erneut.",
            )
        )
        self.next_steps.append(
            f"{name}: Anmeldedaten prüfen und 'schulsync setup --{source.value}' ausführen, damit die Quelle wieder automatisch läuft."
        )

    def _record(self, now: datetime) -> None:
        """record_fetch per fetched source; record_writes_done when nothing is pending (DESIGN 7.6)."""
        assert self.real is not None
        for s in self.sources:
            mode = self.modes[s]
            if mode in (DONE, BLOCKED):
                continue
            fetch = self.fetches.get(s)
            if fetch is not None:
                self.real.record_fetch(self.day, s, fetch.fetched_at, fetch.complete)
            if self.real.pending_count(s) == 0 and (fetch is not None or mode == WRITES):
                self.real.record_writes_done(self.day, s, now)

    # ---- restore

    def _restore_body(self) -> RunOutcome:
        assert self.real is not None
        self.engine = SyncEngine(
            self.real, self.cfg, self._gateway, retry=self._call_with_retries, should_stop=self._timed_out, contain_errors=True
        )
        now = self.deps.clock()
        self.engine.reset_targets_if_changed(now)  # entries deleted in the OLD targets are no longer "deleted by the user"
        result = self.engine.restore(self.restore_uids, now)
        self.restore_result = result
        if not result.restored:
            # Nothing was released: say, per uid, why (never silently). Exit code: an explicit request that hit only
            # unknown / not deleted uids is a call error (2); blocked ones (cancelled / expired) are a refusal (1);
            # "restore all" with nothing restorable is just information (0).
            if not result.entries:
                return RunOutcome(0, None, None, "Es gibt keine bewusst gelöschten Einträge, die wiederhergestellt werden müssten.", False)
            head = "Es gibt keine wiederherstellbaren Einträge." if not self.restore_uids else "Es wurde nichts wiederhergestellt."
            code = 0 if not self.restore_uids else (1 if result.blocked else 2)
            return RunOutcome(code, None, None, "\n".join([head, *self._restore_lines(result)]), False)
        self.shown = self.sources = [s for s in Source if s in result.sources]
        for s in self.shown:
            self.modes[s] = RESTORE
        self.engine.write_phase(self.shown, now)
        return self._publish(self._assemble())

    @staticmethod
    def _restore_lines(result: RestoreResult) -> list[str]:
        """One German line per uid: what happened to the explicit restore of exactly this entry."""
        return [
            f"  {e.uid} - {e.title}: {e.message}" if e.title else f"  {e.uid}: {e.message}" for e in result.entries
        ]

    # ---- report

    def _applied(self, source: Source) -> bool:
        return self.engine is not None and any(
            c.source == source and c.status in (ChangeStatus.APPLIED, ChangeStatus.PLANNED) for c in self.engine.changes
        )

    def _source_status(self, source: Source) -> SourceStatus:
        mode = self.modes.get(source, FETCH)
        applied = self._applied(source)
        if self.crash is not None:
            return SourceStatus.PARTIAL if applied else SourceStatus.FAILED
        assert self.work is not None
        if mode == BLOCKED or source in self.failed:
            return SourceStatus.FAILED
        pending = self.work.pending_count(source)
        if mode in (WRITES, RESTORE):
            if pending == 0:
                return SourceStatus.SUCCESS
            return SourceStatus.PARTIAL if applied else SourceStatus.FAILED
        fetch = self.fetches.get(source)
        if fetch is None:
            return SourceStatus.FAILED
        if fetch.complete:
            if pending == 0:
                return SourceStatus.SUCCESS
            return SourceStatus.PARTIAL if applied else SourceStatus.FAILED
        any_ok = any(oc.status == CategoryStatus.OK for oc in fetch.categories.values())
        return SourceStatus.PARTIAL if (any_ok or applied) else SourceStatus.FAILED

    def _source_result(self, source: Source) -> SourceRunResult:
        mode = DISABLED if source not in self.sources else self.modes.get(source, FETCH)
        if mode == DISABLED:
            return SourceRunResult(source, SourceStatus.DISABLED)
        res = self.res[source]
        if mode == DONE:
            res.status = SourceStatus.ALREADY_DONE
        else:
            if self.engine is not None:
                counts = self.engine.counts_for(source)
                res.calendar, res.tasks = counts.calendar, counts.tasks
            if self.crash is not None and self.crash not in res.errors:
                res.errors.append(self.crash)
            res.status = self._source_status(source)
        if self.work is not None:
            res.last_success_at = self._safe(lambda: self.work.last_complete_fetch(source), "Letzten Abruf lesen")  # type: ignore[union-attr]
        res.stale = (not self.preview) and res.status in (SourceStatus.PARTIAL, SourceStatus.FAILED)
        return res

    @staticmethod
    def _overall(sources: Mapping[Source, SourceRunResult]) -> OverallStatus:
        sts = [r.status for r in sources.values() if r.status != SourceStatus.DISABLED]
        good = (SourceStatus.SUCCESS, SourceStatus.ALREADY_DONE)
        if all(s in good for s in sts):
            return OverallStatus.SUCCESS
        if not any(s in good for s in sts) and any(s == SourceStatus.FAILED for s in sts):
            return OverallStatus.FAILED
        return OverallStatus.PARTIAL

    def _assemble(self) -> RunReport:
        e = self.engine
        sources = {s: self._source_result(s) for s in self.shown}
        pending = 0
        if self.work is not None:
            pending = sum(self._safe(lambda s=s: self.work.pending_count(s), "Ausstehende zählen") or 0 for s in self.sources)  # type: ignore[union-attr]
        steps = list(self.next_steps)
        if pending:
            steps.append("Ausstehende Google-Schreibvorgänge werden beim nächsten Start automatisch nachgeholt.")
        if any(c.error is not None and c.error.code == "run_timeout" for c in (e.changes if e else [])):
            steps.append("Der Lauf hat das Zeitlimit erreicht; die übrigen Schreibvorgänge folgen beim nächsten Start.")
        if self.crash is not None and self.crash.code == "unexpected":
            steps.append("Der Fehler wurde in der Logdatei (Ordner 'logs') festgehalten.")
        if any(c.error is not None and c.error.code == "unexpected" for c in (e.changes if e else [])):
            steps.append("Ein Programmfehler bei einzelnen Einträgen wurde in der Logdatei (Ordner 'logs') festgehalten; der nächste Start versucht es erneut.")
        notices = [*(e.notices if e else []), *self.notices, *self._taken_over_notices()]
        return RunReport(
            started_at=self.started,
            finished_at=self.deps.clock(),
            mode=self.mode,
            day=self.day,
            status=self._overall(sources),
            sources=sources,
            calendar_target=e.calendar_target if e else TargetStatus(self.cfg.google.calendar_name),
            tasks_target=e.tasks_target if e else TargetStatus(self.cfg.google.tasklist_title),
            changes=list(e.changes) if e else [],
            notices=notices,
            pending_writes=pending,
            next_steps=list(dict.fromkeys(steps)),
        )

    def _taken_over_notices(self) -> list[Notice]:
        """What Google already holds from a source this start did not fetch again (ANFORDERUNGEN 8: "Welche Daten bereits
        erfolgreich übernommen wurden"). The day's report file is overwritten by every later start; without this a retry
        after a partial failure would list nothing for the source that was taken over earlier today. One INFO notice per
        entry (rendered in the section "Bereits erfolgreich übernommen", see report.NOTICE_TAKEN_OVER)."""
        if self.preview or self.is_restore or self.work is None:
            return []
        now = self.deps.clock()
        out: list[Notice] = []
        for source in self.shown:
            if self.modes.get(source) not in (DONE, WRITES):
                continue
            stored = self._safe(lambda s=source: self.work.items_for_source(s), "Übernommene Einträge lesen") or []  # type: ignore[union-attr]
            for st in stored:
                if rules.is_fully_past(st.item, now):
                    continue
                where = []
                if st.event_state == EventState.ACTIVE:
                    where.append("Kalender (abgesagt)" if st.cancelled else "Kalender")
                if st.task_state == TaskState.OPEN:
                    where.append("Aufgabe")
                elif st.task_state == TaskState.DONE:
                    where.append("Aufgabe (von dir erledigt)")
                if where:
                    out.append(Notice(Severity.INFO, report_mod.NOTICE_TAKEN_OVER, " und ".join(where), source, st.uid, st.item.title))
        return out

    @staticmethod
    def _digest(text: str) -> str:
        """Short hash that never fails: a lone surrogate in a (hostile) title must not cost the report."""
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]

    def _fingerprint(self, report: RunReport) -> str:
        try:
            base = report.fingerprint()
        except UnicodeEncodeError:
            base = self._digest("\n".join(report.problem_keys()))
        if self.cfg.report.open_on != "warnings":
            return base
        warns = sorted(f"{n.code}:{n.uid or n.title or ''}" for n in report.notices if n.severity == Severity.WARNING)
        return self._digest(base + "|" + "\n".join(warns)) if warns else base

    def _publish(self, report: RunReport, exit_code: int | None = None) -> RunOutcome:
        path: Path | None = None
        try:
            path = report_mod.write_report(self.deps.paths, report)
            report.report_path = str(path)
        except Exception:  # noqa: BLE001
            log.exception(
                "Der Bericht konnte nicht gespeichert werden (vorgesehen: %s)", report_mod.report_file_for(self.deps.paths, report)
            )
            path = self._plain_text_copy(report)
            if path is not None:
                report.report_path = str(path)
        fp = self._fingerprint(report)
        if not self.preview:
            self._housekeeping()
            self._maybe_open(report, fp, path)
            if self.real is not None:
                self._safe(
                    lambda: self.real.record_run(  # type: ignore[union-attr]
                        self.started, self.deps.clock(), self.day, self.mode, report.status, fp, str(path) if path else None
                    ),
                    "Lauf speichern",
                )
        code = exit_code if exit_code is not None else (0 if report.status in (OverallStatus.SUCCESS, OverallStatus.NOTHING_TO_DO) else 1)
        log.info(
            "Lauf beendet (%s, %s): %s; %s",
            _MODE_DE[report.mode],
            report.day.isoformat(),
            report.status.value,
            ", ".join(f"{s.value}={r.status.value}" for s, r in report.sources.items()),
        )
        return RunOutcome(code, report, path, redact(self._console_text(report, path)), False)

    def _plain_text_copy(self, report: RunReport) -> Path | None:
        """The HTML report is lost: leave the console summary as a text file next to the logs, so that a failed run never
        shows nothing at all (a successful run does not need it). None if there is nothing to keep or it cannot be written
        either (then only the log remains)."""
        if report.status in (OverallStatus.SUCCESS, OverallStatus.NOTHING_TO_DO) and not self.preview:
            return None
        name = "vorschau.txt" if self.preview else f"bericht-{report.day.isoformat()}.txt"
        target = self.deps.paths.logs / name
        try:
            text = redact(self._console_text(report, None))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "SchulSync - Textkopie des Berichts (die HTML-Datei konnte nicht gespeichert werden)\n\n" + text + "\n",
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:  # noqa: BLE001 - last resort: log only
            log.error("Auch die Textkopie des Berichts konnte nicht geschrieben werden (%s): %s", type(exc).__name__, target)
            return None
        log.warning("Statt der HTML-Datei liegt eine Textkopie des Berichts unter %s", target)
        return target

    def _housekeeping(self) -> None:
        now = self.deps.clock()
        keep = self.cfg.report.keep_days
        self._safe(lambda: report_mod.prune_reports(self.deps.paths, keep, now), "Berichte aufräumen")
        self._safe(lambda: logging_setup.prune_logs(self.deps.paths, self.cfg.report.log_keep_days, now), "Logs aufräumen")
        if self.real is not None:
            self._safe(lambda: self.real.prune_old(self.day - timedelta(days=keep)), "Alte Läufe aufräumen")  # type: ignore[union-attr]

    def _maybe_open(self, report: RunReport, fp: str, path: Path | None) -> None:
        open_on = self.cfg.report.open_on
        warn = any(n.severity == Severity.WARNING for n in report.notices)
        if open_on == "never" or path is None or not (report.needs_attention or (open_on == "warnings" and warn)):
            return
        shown = self._opened_today()
        if fp in shown:
            log.info("Der Bericht wurde heute schon geöffnet (gleicher Fehler); die Datei wurde nur aktualisiert: %s", path)
            return
        try:
            ok = bool(self.deps.open_report(path))
        except Exception:  # noqa: BLE001
            ok = False
        if report.needs_attention:  # also when the browser could not be opened: the toast is the second way to notice
            try:
                self.deps.notify("SchulSync: Problem beim Abgleich", "Nicht alles konnte übernommen werden. Der Bericht liegt im Browser; 'schulsync report' öffnet ihn erneut.")
            except Exception:  # noqa: BLE001 - a notification must never break the run
                log.warning("Benachrichtigung fehlgeschlagen.")
        if ok:
            if self.real is not None:
                self._safe(lambda: self.real.mark_report_opened(self.day, fp, self.deps.clock()), "Berichtsstatus speichern")  # type: ignore[union-attr]
        else:
            log.warning("Der Bericht konnte nicht im Browser geöffnet werden. Gespeichert unter: %s", path)

    def _opened_today(self) -> set[str]:
        """Fingerprints that already got a tab today: ALL of them (problem A, B, A must not open a third tab), kept in the
        state database (`report_opened`, schema v2). While the database is unusable nothing is remembered here: the CLI
        probes the database first and then uses its own marker (`auto_failure_report.json`), see cli._cmd_run."""
        if self.real is None:
            return set()
        return self._safe(lambda: self.real.report_opened_fingerprints(self.day), "Berichtsstatus lesen") or set()  # type: ignore[union-attr]

    # ---- console text

    @staticmethod
    def _fmt_counts(c: Counts) -> str:
        parts = [
            (c.new, "neu"),
            (c.updated, "aktualisiert"),
            (c.unchanged, "unverändert"),
            (c.cancelled, "abgesagt/entfernt"),
            (c.reactivated, "reaktiviert"),
            (c.skipped, "übersprungen"),
            (c.failed, "fehlgeschlagen"),
        ]
        shown = [f"{n} {label}" for n, label in parts if n]
        return ", ".join(shown) if shown else "keine Änderungen"

    def _console_text(self, report: RunReport, path: Path | None) -> str:
        lines: list[str] = []
        preview = report.mode == RunMode.PREVIEW
        if preview:
            lines.append("Vorschau: Es wurde nichts bei Google geschrieben und kein Erfolg verbucht.")
        title = "Wiederherstellung" if self.is_restore else "SchulSync"
        lines.append(
            f"{title} ({_MODE_DE[report.mode]}) am {report.day:%d.%m.%Y}: {_OVERALL_DE[report.status]}"
        )
        if self.restore_result is not None:
            lines.append(f"  Freigegeben: {len(self.restore_result.restored)} Eintrag/Einträge.")
            lines.extend(self._restore_lines(self.restore_result))
        for src, r in report.sources.items():
            if r.status == SourceStatus.DISABLED:
                continue
            lines.append(f"  {_SOURCE_NAME[src]}: {_SOURCE_DE[r.status]}")
            lines.append(f"    Kalender: {self._fmt_counts(r.calendar)}")
            lines.append(f"    Aufgaben: {self._fmt_counts(r.tasks)}")
            lines.extend(f"    Problem: {e.message}" for e in r.errors)
            lines.extend(
                f"    Problem ({_CAT_DE.get(cat, cat.value)}): {oc.error.message if oc.error else oc.status.value}"
                for cat, oc in r.categories.items()
                if not oc.ok
            )
        for tgt in (report.calendar_target, report.tasks_target):
            if tgt.ok is False and tgt.error:
                lines.append(f"  Google-Ziel „{tgt.name}“: {tgt.error.message}")
        if preview:
            planned = [c for c in report.changes if c.status == ChangeStatus.PLANNED]
            lines.append(f"Geplante Änderungen: {len(planned)}")
            lines.extend(
                f"  - {_ACTION_DE.get(c.action.value, c.action.value)} ({_TARGET_DE[c.target.value]}): {c.title}" for c in planned
            )
        bad = [c for c in report.changes if c.status in (ChangeStatus.FAILED, ChangeStatus.DEFERRED, ChangeStatus.UNCERTAIN)]
        if bad:
            lines.append(f"Nicht erledigt: {len(bad)}")
            lines.extend(
                f"  - {c.title} ({_TARGET_DE[c.target.value]}): {_STATUS_DE[c.status]}" + (f" – {c.error.message}" if c.error else "")
                for c in bad[:10]
            )
            if len(bad) > 10:
                lines.append(f"  ... und {len(bad) - 10} weitere (siehe Bericht)")
        if report.pending_writes:
            lines.append(f"Ausstehende Google-Schreibvorgänge: {report.pending_writes} (werden beim nächsten Start nachgeholt)")
        lines.extend(f"Nächster Schritt: {s}" for s in report.next_steps)
        if path is not None:
            lines.append(f"Bericht: {path}")
        return "\n".join(lines)


_CAT_DE = {
    Category.ASSIGNMENTS: "Aufgaben",
    Category.HOMEWORK: "Hausaufgaben",
    Category.EXAMS: "Prüfungen",
    Category.CANCELLATIONS: "Unterrichtsentfälle",
}
