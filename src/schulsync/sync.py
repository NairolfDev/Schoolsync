"""Sync engine: ingest of source data (DESIGN 4.3) and the Google write phase (DESIGN 6).

The engine works on a :class:`StateStore`, a :class:`GoogleGateway` and the pure planner (`rules`). It knows
neither the console nor the browser nor the clock (``now`` is always passed in). One engine instance serves one
run; its public attributes collect everything the runner needs for the report:

* ``changes``   one :class:`Change` per Google write (APPLIED, or PLANNED in preview mode) and per write that did
  not happen (FAILED / DEFERRED / UNCERTAIN). Skips are NOT changes (they are counted).
* ``counts``    per source and target (:class:`SourceCounts`): new / updated / unchanged / cancelled / reactivated /
  skipped / failed. Adoptions (an orphan found by its project marker) count as ``new``; every write that did not
  complete (FAILED, DEFERRED, UNCERTAIN) counts as ``failed``.
* ``notices``   German hints for the report (codes as rendered by report.py).
* ``calendar_target`` / ``tasks_target``  :class:`TargetStatus`, set once the targets were checked.

Rules the code follows (all from the requirements, see docs/DESIGN.md sections 4.3 and 6):

* Absence, an empty list, an incomplete fetch or a connection error NEVER cancel or delete anything. Only
  ``status == CANCELLED`` or ``fetch.withdrawn`` (explicit proof) cancel; a reactivation needs a complete category.
* No fallback target. If ``check_targets`` fails, the writes of the AFFECTED target are DEFERRED and the items stay ``dirty``:
  a missing task list defers only the task writes, a missing (or unverified) calendar only the calendar writes; the other
  target keeps working. A plain ``TargetUnavailableError`` (no per-target information) defers everything.
* Before every write the current Google state is read: a deleted event / task is recorded as USER_DELETED (and
  never re-created except through :meth:`SyncEngine.restore`), a completed or hidden task is DONE (never touched),
  text outside the managed block is preserved (``textblocks.merge_description``).
* Non-idempotent writes (event create, task create, task delete) are announced with an *intent* that is committed
  BEFORE the API call and resolved in the same transaction as the result. An intent that is still open at the start
  of a run (crash, lost response) is reconciled first: events by their deterministic id, tasks by the
  ``schulsync-id: <uid>`` marker in a fresh task list. Nothing is created twice "on suspicion".
* One failing item never stops the others: SchulSyncErrors are caught per write and recorded. Unexpected exceptions
  (bugs) propagate to the caller, unless the engine was built with ``contain_errors=True`` (the runner does): then an
  item that raises one is recorded as FAILED (code ``unexpected``) and the other items are still processed.

Target identity (H2): the configured ids of the calendar and the task list are remembered in the state meta at the first
successful target check. If a later run is configured with other ids (the user re-created the calendar or the list and ran
``schulsync setup``), every event id would read 404 and be taken for "deleted by the user", so nothing would be created in
the new targets. :meth:`SyncEngine.reset_targets_if_changed` therefore resets the Google link state of all items (see there)
before anything else happens in the run.

Intent payloads: calendar create -> ``DesiredEvent.content_hash()``; task create -> ``"<content hash>;<base
deadline>;<due date>"`` so that an adopted task gets exactly the bookkeeping the planner needs (``_decode_payload``).

Honesty: verified against FakeGoogle / DryRunGateway only (see tests); the behaviour of the real Google APIs in the
corner cases (tombstones, deleted tasks) follows the documented assumptions in google_bodies / fake_google.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, NamedTuple, Sequence, TypeVar

from . import rules
from .config import Config
from .errors import (
    AccessDeniedError,
    AuthError,
    BadRequestError,
    ConfigError,
    ConflictError,
    ErrorInfo,
    GoogleAuthError,
    NotFoundError,
    OutcomeUnknownError,
    SchulSyncError,
    TargetUnavailableError,
)
from .google_bodies import (
    event_description,
    event_insert_body,
    event_is_deleted,
    event_patch_body,
    event_private_props,
    task_due_date,
    task_insert_body,
    task_is_completed,
    task_is_deleted,
    task_is_hidden,
    task_marker_uid,
    task_notes,
    task_patch_body,
    task_removed_by_us,
)
from .google_targets import TargetsUnavailableError
from .google_types import PROP_UID, GoogleGateway, make_event_id
from .models import (
    Action,
    Change,
    ChangeStatus,
    Counts,
    EventState,
    FetchResult,
    ItemState,
    ItemStatus,
    Notice,
    Severity,
    Source,
    SourceItem,
    Target,
    TargetStatus,
    TaskState,
    CategoryStatus,
)
from .redact import redact
from .rules import EventAction, EventPlan, TaskAction, TaskPlan
from .state import Intent, StateStore
from .textblocks import TASK_NOTES_LIMIT, managed_lines, merge_description, task_marker_line, task_removed_line
from .timeutil import fetch_window

log = logging.getLogger("schulsync.sync")

T = TypeVar("T")
Retry = Callable[[Callable[[], T]], T]

# Errors that prove the request was NOT processed (rejected / not authorised / target missing): the intent is
# aborted. Everything else (timeouts, 5xx, connection loss, unknown classes) may have been applied.
_DEFINITIVE = (BadRequestError, AccessDeniedError, AuthError, TargetUnavailableError, NotFoundError, ConfigError)
_DUP_TEXT = "Derselbe Eintrag kam in der Quelle mehrfach vor; nur ein Eintrag wurde übernommen."
_UNKNOWN_BASE = "unknown"  # task_base_deadline sentinel: the due date in Google is unknown (see _forget_applied)
_MAX_EVENT_GEN = 32  # generations tried when an event id is matched back to its (uid, gen)
_RESTORED = "restored"  # task_removed_reason of a task released by `restore`: the old tombstone proves nothing any more
_META_CALENDAR = "target.calendar_id"  # state meta: the ids the stored Google link state (event ids, task ids) belongs to
_META_TASKLIST = "target.tasklist_id"
_TARGETS_CHANGED_TEXT = (
    "Kalender oder Aufgabenliste wurden gewechselt (andere Kennung als beim letzten Lauf). Alle noch aktuellen Einträge "
    "werden in den neuen Zielen neu angelegt; in den früheren Zielen wird nichts verändert oder gelöscht."
)
_RESTORE_TEXT = {
    "restored": "Wiederhergestellt: Der Eintrag wird in Google neu angelegt.",
    "unknown": "Nichts wiederherzustellen: Diese Kennung ist unbekannt.",
    "not_deleted": "Nichts wiederherzustellen: Der Eintrag wurde nicht von dir in Google gelöscht.",
    "cancelled": "Nicht wiederhergestellt: Der Eintrag ist abgesagt (von der Schule zurückgenommen); abgesagte Einträge werden nicht neu angelegt.",
    "expired": "Nicht wiederhergestellt: Die Frist bzw. der Termin ist bereits vorbei; Vergangenes wird nicht neu angelegt.",
}
_TASK_SKIP_REASONS = frozenset({"submitted", "done_untouched", "user_deleted", "removed_cancelled", "expired", "already_cancelled"})
_COUNTER_OF = {
    Action.CREATE: "new",
    Action.ADOPT: "new",
    Action.RESTORE: "new",
    Action.UPDATE: "updated",
    Action.CANCEL: "cancelled",
    Action.REMOVE_TASK: "cancelled",
    Action.REACTIVATE: "reactivated",
    Action.COMPLETE: "updated",
    Action.REMOVE_EVENT: "cancelled",
}


@dataclass
class SourceCounts:
    """Counts of one source, per Google target."""

    calendar: Counts = field(default_factory=Counts)
    tasks: Counts = field(default_factory=Counts)

    def of(self, target: Target) -> Counts:
        return self.calendar if target == Target.CALENDAR else self.tasks


@dataclass(frozen=True)
class RestoreEntry:
    """Result of an explicit restore for ONE uid. ``outcome``: "restored", "nothing" (not deleted / unknown uid) or
    "blocked" (deleted, but the planner would skip a re-creation: the item is cancelled or fully past). ``reason`` is
    None for restored, else "unknown" | "not_deleted" | "cancelled" | "expired". ``message`` is the German sentence."""

    uid: str
    outcome: str
    reason: str | None
    title: str | None
    message: str


@dataclass
class RestoreResult:
    restored: list[str] = field(default_factory=list)  # uids whose deleted event and/or task were released
    not_found: list[str] = field(default_factory=list)  # requested uids without anything to restore (unknown or not deleted)
    sources: set[Source] = field(default_factory=set)
    blocked: dict[str, str] = field(default_factory=dict)  # uid -> "cancelled" | "expired": deleted, but NOT restored
    entries: list[RestoreEntry] = field(default_factory=list)  # one per requested (or deleted) uid


class _Applied(NamedTuple):
    hash: str | None
    base: str | None
    due: date | None


def _encode_payload(content_hash: str, base: str | None, due: date | None) -> str:
    return f"{content_hash};{base or ''};{due.isoformat() if due else ''}"


def _decode_payload(payload: str | None) -> _Applied | None:
    if not payload or payload.count(";") != 2:
        return None
    h, base, due = payload.split(";")
    try:
        return _Applied(h or None, base or None, date.fromisoformat(due) if due else None)
    except ValueError:
        return None


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _scrub_item(item: SourceItem) -> SourceItem:
    """A lone surrogate (e.g. an emoji cut in the middle by the platform) cannot be hashed, stored or sent: it would
    fail the whole ingest of the source on every start. Replace it by U+FFFD at the boundary; everything else is kept."""
    changes: dict[str, Any] = {}
    for name in ("source_id", "title", "course", "description", "room", "teacher", "url", "subtype"):
        value = getattr(item, name)
        if isinstance(value, str) and _LONE_SURROGATE.search(value):
            changes[name] = _LONE_SURROGATE.sub("�", value)
    if any(_LONE_SURROGATE.search(n) for n in item.notes):
        changes["notes"] = tuple(_LONE_SURROGATE.sub("�", n) for n in item.notes)
    return dataclasses.replace(item, **changes) if changes else item


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _forget_applied(store: StateStore, st: ItemState, target: Target, *, due: bool = False) -> None:
    """Persist "what Google holds is unknown" BEFORE a patch (DESIGN 6: a patch has no intent, so the stored applied
    state must not claim anything the patch may change). A lost response or a crash between patch and commit would
    otherwise leave Google on the new content while the state says "old content": a source that flips back would
    then compare equal to the stale applied hash and never be corrected.

    Calendar: applied hash -> None (the planner updates again). Task: applied hash -> None; with `due` the base
    deadline becomes the sentinel `_UNKNOWN_BASE`, which never equals a real deadline, so the planner treats the next
    plan as a deadline change and writes the due date again. Written through `StateStore.forget_applied`, not
    `upsert`: the in-memory state object is updated as well, so the later commit of the result overwrites it consistently."""
    if target == Target.CALENDAR:
        st.event_applied_hash = None
        store.forget_applied(st.uid, target)
        return
    st.task_applied_hash = None
    if due:
        st.task_base_deadline = _UNKNOWN_BASE
    store.forget_applied(st.uid, target, base_deadline=_UNKNOWN_BASE if due else None)


def _wrote_location(rec: dict[str, Any]) -> bool:
    """True if there is no location, or the location of the event is the room SchulSync copied into it: its managed block
    names exactly that room ("Raum: ..."). Then clearing it when the source no longer has a room is our own change. Any other
    location was typed by the user (ANFORDERUNGEN 6: what the user wrote outside the automatic part must survive)."""
    location = " ".join(str(rec.get("location") or "").split())
    return not location or f"Raum: {location}" in managed_lines(event_description(rec))


def _no_retry(fn: Callable[[], T]) -> T:
    return fn()


class SyncEngine:
    """Ingest + write phase for one run. See the module docstring."""

    def __init__(
        self,
        store: StateStore,
        cfg: Config,
        gateway_factory: Callable[[], GoogleGateway],
        *,
        preview: bool = False,
        retry: Retry | None = None,
        should_stop: Callable[[], bool] | None = None,
        contain_errors: bool = False,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.preview = preview
        self._contain = contain_errors
        self._stage = Target.CALENDAR  # target the current item is being processed for (names the failed write)
        self._gateway_factory = gateway_factory
        self._retry: Retry = retry or _no_retry
        self._should_stop = should_stop or (lambda: False)
        self.changes: list[Change] = []
        self.notices: list[Notice] = []
        self.counts: dict[Source, SourceCounts] = {}
        self.calendar_target = TargetStatus(cfg.google.calendar_name)
        self.tasks_target = TargetStatus(cfg.google.tasklist_title)
        self._now: datetime | None = None
        self._gateway: GoogleGateway | None = None
        self._connected: bool | None = None
        self._halt: ErrorInfo | None = None  # no Google call any more this run (targets down, auth, time limit)
        self._tasks_cache: list[dict[str, Any]] | None = None
        self._tasks_halt: ErrorInfo | None = None  # task list unreadable: task writes deferred, calendar unaffected
        self._calendar_halt: ErrorInfo | None = None  # calendar vanished mid-run: event writes deferred, tasks unaffected
        self._restored_events: set[str] = set()
        self._restored_tasks: set[str] = set()
        self._touched: set[tuple[str, Target]] = set()
        self._notice_keys: set[tuple[str, str | None, str]] = set()

    # ------------------------------------------------------------------ bookkeeping helpers

    def counts_for(self, source: Source) -> SourceCounts:
        return self.counts.setdefault(source, SourceCounts())

    def _add_notice(self, notice: Notice) -> None:
        key = (notice.code, notice.uid, notice.message)
        if key not in self._notice_keys:
            self._notice_keys.add(key)
            self.notices.append(notice)

    def _notice(self, severity: Severity, code: str, message: str, item: SourceItem) -> None:
        self._add_notice(Notice(severity, code, message, item.source, item.uid, item.title))

    def _count_skip(self, item: SourceItem, reason: str) -> None:
        c = self.counts_for(item.source)
        c.calendar.skip(reason)
        if rules.lead_days(item.kind, self.cfg.rules) is not None:
            c.tasks.skip(reason)

    def _skipped(self, st: ItemState, target: Target, reason: str) -> None:
        self.counts_for(st.item.source).of(target).skip(reason)

    def _unchanged(self, st: ItemState, target: Target) -> None:
        if (st.uid, target) not in self._touched:
            self.counts_for(st.item.source).of(target).unchanged += 1

    def _change(
        self,
        st: ItemState,
        target: Target,
        action: Action,
        status: ChangeStatus,
        *,
        message: str | None = None,
        error: ErrorInfo | None = None,
    ) -> None:
        item = st.item
        self.changes.append(Change(st.uid, item.source, item.kind, item.title, target, action, status, message, error))
        counts = self.counts_for(item.source).of(target)
        if status in (ChangeStatus.APPLIED, ChangeStatus.PLANNED):
            self._touched.add((st.uid, target))
            name = _COUNTER_OF.get(action)
            if name:
                setattr(counts, name, getattr(counts, name) + 1)
        else:
            counts.failed += 1

    def _applied(self, st: ItemState, target: Target, action: Action, message: str | None = None) -> None:
        self._change(st, target, action, ChangeStatus.PLANNED if self.preview else ChangeStatus.APPLIED, message=message)

    def _fail(
        self, st: ItemState, target: Target, action: Action | None, exc: SchulSyncError, *, uncertain: bool = False
    ) -> bool:
        """Record a failed write; the item stays dirty. Returns False (= "not done")."""
        status = ChangeStatus.UNCERTAIN if uncertain or isinstance(exc, OutcomeUnknownError) else ChangeStatus.FAILED
        info = exc.to_info()
        if isinstance(exc, GoogleAuthError):
            self._set_halt(info, targets=True)  # the login is shared: every further call would fail the same way
        elif isinstance(exc, TargetUnavailableError):
            self._set_target_down(target, info)  # only the target that vanished; the other one is independent
        log.warning("%s %s: %s (%s)", target.value, st.uid, info.message, info.code)
        self._change(st, target, action or Action.UPDATE, status, error=info)
        return False

    def _defer(self, st: ItemState, target: Target, action: Action | None) -> bool:
        own = self._tasks_halt if target == Target.TASKS else self._calendar_halt
        self._change(st, target, action or Action.UPDATE, ChangeStatus.DEFERRED, error=self._halt or own)
        return False

    def _set_target_down(self, target: Target, info: ErrorInfo) -> None:
        """One target became unavailable after the check: its writes are deferred, the other target keeps working."""
        if target == Target.TASKS:
            self._tasks_halt, status = self._tasks_halt or info, self.tasks_target
        else:
            self._calendar_halt, status = self._calendar_halt or info, self.calendar_target
        status.ok, status.error = False, info

    def _set_halt(self, info: ErrorInfo, *, targets: bool) -> None:
        if self._halt is None:
            self._halt = info
        if targets:
            for t in (self.calendar_target, self.tasks_target):
                t.ok, t.error = False, info

    def _save(self, st: ItemState, intent: int | None = None, note: str | None = None, *, abort: bool = False) -> None:
        """Commit the item state; an intent is resolved (or aborted) in the SAME transaction."""
        with self.store.transaction():
            self.store.upsert(st, at=self._now)
            if intent is not None:
                (self.store.abort_intent if abort else self.store.resolve_intent)(intent, note)

    def _open_intent(self, uid: str, target: Target, action: str, key: str, payload: str | None) -> int:
        for it in self.store.open_intents():
            if (it.uid, it.target, it.action, it.key) == (uid, target, action, key):
                return it.id  # unresolved from an earlier attempt: reuse instead of piling up
        return self.store.open_intent(uid, target, action, key, payload, at=self._now)

    def _open_intent_for(self, uid: str, target: Target, action: str) -> Intent | None:
        for it in self.store.open_intents():
            if it.uid == uid and it.target == target and it.action == action:
                return it
        return None

    # ------------------------------------------------------------------ ingest (DESIGN 4.3)

    def ingest(self, fetch: FetchResult, now: datetime) -> None:
        """Merge one source fetch into the state. Never cancels or deletes anything from absence."""
        self._now = now
        window = fetch_window(now, self.cfg.rules.window_days)
        fetch = dataclasses.replace(fetch, items=[_scrub_item(i) for i in fetch.items])
        for n in fetch.notices:
            self._add_notice(n)
        known = {s.uid: s for s in self.store.items_for_source(fetch.source)}
        all_uids = {i.uid for i in fetch.items if i.source == fetch.source}
        items = self._dedupe(fetch, known)
        adopt, defer = self._match_identities(items, known, all_uids, fetch, now)
        present = {i.uid for i in items}
        with self.store.transaction():
            for item in items:
                uid = item.uid
                if uid in known:
                    self._ingest_known(known[uid], item, fetch, now)
                elif uid in adopt:
                    self._ingest_adopted(adopt[uid], item, fetch, now)
                elif uid not in defer:
                    self._ingest_new(item, fetch, window, now)
            for uid, proof in fetch.withdrawn.items():  # withdrawn items need not be listed any more
                st = known.get(uid)
                if st is not None and uid not in present and not st.cancelled:
                    st.cancelled, st.dirty = True, True
                    st.last_seen_at = now
                    self._notice(Severity.INFO, "withdrawn_confirmed", proof, st.item)
                    self.store.upsert(st, at=now)

    def _dedupe(self, fetch: FetchResult, known: dict[str, ItemState]) -> list[SourceItem]:
        """Same content (dedupe_key) under another uid inside one fetch = one NEW item. Known uids win and are NEVER dropped:
        the requirement is not to CREATE equal content twice, but an entry that already has its own event / task must
        keep following its source (a moved or cancelled twin would otherwise stay stale in Google without any notice)."""
        ordered = sorted((i for i in fetch.items if i.source == fetch.source), key=lambda i: i.uid not in known)
        kept: dict[str, SourceItem] = {}
        seen_keys: set[str] = set()
        for item in ordered:
            if item.uid in kept:  # the same entry listed twice (paging overlap) is harmless unless the content differs
                if item != kept[item.uid]:
                    self._notice(Severity.WARNING, "duplicate_in_source", _DUP_TEXT, item)
                continue
            key = item.dedupe_key()
            if key in seen_keys and item.uid not in known:
                self._notice(Severity.WARNING, "duplicate_in_source", _DUP_TEXT, item)
                continue
            seen_keys.add(key)
            kept[item.uid] = item
        return list(kept.values())

    def _match_identities(
        self,
        items: list[SourceItem],
        known: dict[str, ItemState],
        all_uids: set[str],
        fetch: FetchResult,
        now: datetime,
    ) -> tuple[dict[str, ItemState], set[str]]:
        """Conservative content matching, ONLY for items without a stable id (id_stable=False).

        A candidate is a known, unstable, not fully past item of the same source/kind/course/title whose uid is
        missing from this fetch. Exactly one candidate that nobody else claims -> adopt (flagged uncertain);
        several -> treated as new + warning. If the category was not read completely a candidate may just be
        missing from the list, so the new item waits for the next complete fetch."""
        adopt: dict[str, ItemState] = {}
        defer: set[str] = set()
        unknown = [i for i in items if i.uid not in known and not i.id_stable]
        if not unknown:
            return adopt, defer
        busy = {it.uid for it in self.store.open_intents(fetch.source)}
        pool = [
            s
            for s in known.values()
            if not s.item.id_stable and s.uid not in all_uids and s.uid not in busy and not rules.is_fully_past(s.item, now)
        ]
        cands = {
            i.uid: [
                s
                for s in pool
                if s.item.kind == i.kind and _norm(s.item.title) == _norm(i.title) and _norm(s.item.course) == _norm(i.course)
            ]
            for i in unknown
        }
        claims = Counter(s.uid for lst in cands.values() for s in lst)
        for i in unknown:
            lst = cands[i.uid]
            if not lst:
                continue
            outcome = fetch.categories.get(i.category)
            if outcome is None or outcome.status != CategoryStatus.OK:
                defer.add(i.uid)
                self._notice(
                    Severity.WARNING,
                    "identity_uncertain",
                    "Die Zuordnung dieses Eintrags ohne feste Kennung wird auf den nächsten vollständigen Abruf verschoben.",
                    i,
                )
            elif len(lst) == 1 and claims[lst[0].uid] == 1:
                adopt[i.uid] = lst[0]
                self._notice(
                    Severity.WARNING,
                    "identity_uncertain",
                    f"Der Eintrag hat keine feste Kennung und wurde über Titel und Kurs dem bekannten Eintrag „{lst[0].item.title}“ zugeordnet.",
                    i,
                )
            else:
                self._notice(
                    Severity.WARNING,
                    "identity_uncertain",
                    "Mehrere bekannte Einträge passen zu diesem Eintrag ohne feste Kennung; er wird als neu behandelt.",
                    i,
                )
        return adopt, defer

    def _ingest_known(self, st: ItemState, item: SourceItem, fetch: FetchResult, now: datetime) -> None:
        proof = fetch.withdrawn.get(st.uid)
        if item.status == ItemStatus.CANCELLED or proof is not None:
            cancelled = True
        elif st.cancelled:  # explicit reactivation: only if the whole category was read completely
            outcome = fetch.categories.get(item.category)
            cancelled = not (outcome is not None and outcome.status == CategoryStatus.OK)
        else:
            cancelled = False
        new_hash = item.content_hash()
        changed = new_hash != st.item_hash or cancelled != st.cancelled
        if cancelled and not st.cancelled and proof is not None:
            self._notice(Severity.INFO, "withdrawn_confirmed", proof, item)
        st.item, st.item_hash, st.cancelled, st.last_seen_at = item, new_hash, cancelled, now
        if changed:
            st.dirty = True
        elif not st.dirty:
            self._count_unchanged_known(st)
        self.store.upsert(st, at=now)

    def _count_unchanged_known(self, st: ItemState) -> None:
        c = self.counts_for(st.item.source)
        if st.event_state == EventState.ACTIVE:
            c.calendar.unchanged += 1
        if rules.lead_days(st.item.kind, self.cfg.rules) is not None and st.task_state in (TaskState.OPEN, TaskState.DONE):
            c.tasks.unchanged += 1

    def _ingest_adopted(self, old: ItemState, item: SourceItem, fetch: FetchResult, now: datetime) -> None:
        cancelled = item.status == ItemStatus.CANCELLED or item.uid in fetch.withdrawn
        new = dataclasses.replace(
            old, item=item, item_hash=item.content_hash(), cancelled=cancelled, last_seen_at=now, dirty=True, id_uncertain=True
        )
        self.store.delete_item(old.uid)
        self.store.upsert(new, at=now)

    def _ingest_new(self, item: SourceItem, fetch: FetchResult, window: Any, now: datetime) -> None:
        if item.uid in fetch.withdrawn:
            self._count_skip(item, "already_cancelled")
            return
        ok, reason = rules.new_item_verdict(item, now, window)
        if not ok:
            self._count_skip(item, reason)  # skipped new items are NOT stored
            return
        self.store.upsert(
            ItemState(item=item, item_hash=item.content_hash(), cancelled=False, first_seen_at=now, last_seen_at=now, dirty=True),
            at=now,
        )

    # ------------------------------------------------------------------ restore

    def restore(self, uids: Sequence[str] | None, now: datetime) -> RestoreResult:
        """Release deliberately deleted entries (None = all). The next write phase re-creates them.

        Event: ``event_gen`` + 1 (a deleted id stays reserved in Google), state NONE. Task USER_DELETED -> REMOVED (reason
        "restored"; the planner creates it again).
        Completed tasks are never touched.

        Every uid gets an explicit result (:class:`RestoreEntry`): restored, nothing to restore (not deleted / unknown) or
        NOT restored because the item is cancelled or fully past: the planner skips such an item when it is written
        (``already_cancelled`` / ``expired``), so releasing it would only pretend. Such an item is left exactly as it was.
        Entries are in the order of the request ("all": uid order)."""
        self._now = now
        result = RestoreResult()

        def deleted(st: ItemState) -> bool:
            return st.event_state == EventState.USER_DELETED or st.task_state == TaskState.USER_DELETED

        if uids is None:
            candidates = [(s.uid, s) for s in self.store.all_items() if deleted(s)]
        else:
            candidates = [(uid, self.store.get(uid)) for uid in dict.fromkeys(uids)]
        with self.store.transaction():
            for uid, st in candidates:
                if st is None or not deleted(st):
                    reason = "unknown" if st is None else "not_deleted"
                    result.not_found.append(uid)
                    result.entries.append(RestoreEntry(uid, "nothing", reason, st.item.title if st else None, _RESTORE_TEXT[reason]))
                    self._add_notice(
                        Notice(Severity.INFO, "restore_nothing", _RESTORE_TEXT[reason], st.item.source if st else None, uid, st.item.title if st else None)
                    )
                    continue
                if st.cancelled or rules.is_fully_past(st.item, now):
                    reason = "cancelled" if st.cancelled else "expired"
                    result.blocked[uid] = reason
                    result.entries.append(RestoreEntry(uid, "blocked", reason, st.item.title, _RESTORE_TEXT[reason]))
                    self._notice(Severity.WARNING, "restore_blocked", _RESTORE_TEXT[reason], st.item)
                    continue
                if st.event_state == EventState.USER_DELETED:
                    st.event_gen += 1
                    st.event_id, st.event_state, st.event_applied_hash, st.applied_cancelled = None, EventState.NONE, None, False
                    self._restored_events.add(st.uid)
                if st.task_state == TaskState.USER_DELETED:
                    # REMOVED, not NONE: for NONE a deleted task with our marker reads as "deleted by the user" (a lost
                    # database, an unclear create): the tombstone of the very task the user just released would undo this
                    # restore. For REMOVED an old tombstone proves nothing (DESIGN 6); the planner re-creates the task.
                    st.task_id, st.task_state, st.task_applied_hash = None, TaskState.REMOVED, None
                    st.task_base_deadline, st.task_due, st.task_removed_reason = None, None, _RESTORED
                    self._restored_tasks.add(st.uid)
                st.dirty = True
                self.store.upsert(st, at=now)
                result.restored.append(st.uid)
                result.sources.add(st.item.source)
                result.entries.append(RestoreEntry(st.uid, "restored", None, st.item.title, _RESTORE_TEXT["restored"]))
                self._notice(Severity.INFO, "restore_done", _RESTORE_TEXT["restored"], st.item)
        return result

    # ------------------------------------------------------------------ target identity (H2)

    def _configured_targets(self) -> tuple[str, str] | None:
        g = self.cfg.google
        calendar_id, tasklist_id = g.calendar_id.strip(), g.tasklist_id.strip()
        return (calendar_id, tasklist_id) if calendar_id and tasklist_id else None

    def reset_targets_if_changed(self, now: datetime) -> bool:
        """If the configured calendar / task list ids differ from the ones the stored link state belongs to, start over.

        Why: after the user re-created the calendar or the list (``schulsync setup`` found new ids) every known event reads
        404/410 and every task is "not in the list": the engine would classify all of it as deleted by the user and create
        nothing. So, for ALL items: event id and applied hashes gone, event state NONE with ``event_gen`` + 1 (an id that was
        used before may be reserved in a calendar that is used again), task id / state / applied hash / base deadline / due
        gone, ``dirty``. Fully past, expired or cancelled items are reset like the others and then simply skipped by the
        planner. Two things hold on purpose: a task the user COMPLETED stays completed (ANFORDERUNGEN 6: "keine neue offene
        Kopie"), and nothing is ever written to or deleted in the old targets. Open write intents belonged to the old
        targets and are aborted. The new ids are stored in the same transaction, so this happens once.

        No stored ids (a new database, a database from before this feature, or a first run that never reached Google) or ids
        that are not configured yet: nothing to compare, nothing happens; the ids are stored at the first successful target
        check (:meth:`_connect`). Returns True if a reset happened. Call it BEFORE the day gate looks at pending work."""
        self._now = now
        configured = self._configured_targets()
        stored = (self.store.get_meta(_META_CALENDAR), self.store.get_meta(_META_TASKLIST))
        if configured is None:
            return False
        if stored == (None, None):
            # A database from before this feature has linked items but no stored ids: this run is the first that can know
            # them, and the best guess is the configuration the user has been running with. (A new database has nothing
            # linked: the ids are stored at the first successful target check instead.)
            if any(st.event_state != EventState.NONE or st.task_state != TaskState.NONE for st in self.store.all_items()):
                self._remember_targets()
            return False
        if None in stored or stored == configured:
            return False
        with self.store.transaction():
            items = self.store.all_items()
            for st in items:
                st.event_id, st.event_state, st.event_applied_hash, st.applied_cancelled = None, EventState.NONE, None, False
                st.event_gen += 1
                if st.task_state != TaskState.DONE:
                    st.task_id, st.task_state, st.task_applied_hash = None, TaskState.NONE, None
                    st.task_base_deadline, st.task_due, st.task_removed_reason = None, None, None
                st.dirty = True
                self.store.upsert(st, at=now)
            for it in self.store.open_intents():
                self.store.abort_intent(it.id, "Ziele gewechselt")
            self.store.set_meta(_META_CALENDAR, configured[0])
            self.store.set_meta(_META_TASKLIST, configured[1])
        log.warning(
            "Google-Ziele gewechselt (Kalender-/Listen-Kennung geändert): %d Einträge werden in den neuen Zielen neu angelegt.", len(items)
        )
        self._add_notice(Notice(Severity.WARNING, "targets_changed", _TARGETS_CHANGED_TEXT))
        return True

    def _remember_targets(self) -> None:
        """First successful target check: remember which targets the link state now belongs to."""
        configured = self._configured_targets()
        if configured is not None and self.store.get_meta(_META_CALENDAR) is None and self.store.get_meta(_META_TASKLIST) is None:
            self.store.set_meta(_META_CALENDAR, configured[0])
            self.store.set_meta(_META_TASKLIST, configured[1])

    # ------------------------------------------------------------------ write phase (DESIGN 6)

    def write_phase(self, sources: Sequence[Source], now: datetime) -> None:
        """Process every dirty item (and every open intent) of `sources`. No Google contact if nothing is pending."""
        self._now = now
        uids = sorted({u for s in sources for u in self.store.dirty_uids(s)})
        intents = [it for s in sources for it in self.store.open_intents(s)]
        if not uids and not intents:
            return
        if self._connect():
            self._recover_intents(intents)
        for uid in uids:
            if self._halt is None and self._should_stop():
                self._set_halt(
                    ErrorInfo(
                        "run_timeout",
                        "Das Zeitlimit für den Lauf wurde erreicht.",
                        hint="Die übrigen Schreibvorgänge folgen beim nächsten Start.",
                    ),
                    targets=False,
                )
            try:
                self._process(uid)
            except Exception as exc:  # noqa: BLE001 - only contained when the caller asked for it (the runner does)
                if not self._contain:
                    raise
                self._unexpected(uid, exc)

    def _unexpected(self, uid: str, exc: Exception) -> None:
        """A bug triggered by ONE item must not keep the other items (of either source) out of Google on every start:
        the item stays dirty and is reported as FAILED; whatever it left open (an intent) is reconciled next run."""
        log.exception("Unerwarteter Fehler bei %s", uid)
        st = self.store.get(uid)
        if st is not None:
            self._change(st, self._stage, Action.UPDATE, ChangeStatus.FAILED, error=self._unexpected_info(exc))

    @staticmethod
    def _unexpected_info(exc: Exception) -> ErrorInfo:
        return ErrorInfo(
            "unexpected",
            "Im Programm ist ein unerwarteter Fehler aufgetreten.",
            hint="Der Fehler steht in der Logdatei. Der nächste Start versucht es erneut.",
            detail=redact(f"{type(exc).__name__}: {exc}")[:500],
        )

    def _connect(self) -> bool:
        """Create the gateway and check both targets ONCE. A failure defers the writes of the affected target (no fallback);
        if nothing can be said about the targets (login, network, an unclassified error) every Google write waits."""
        if self._connected is not None:
            return self._connected
        self._connected = False
        gateway: GoogleGateway | None = None
        try:
            gateway = self._gateway_factory()
            check = self._retry(gateway.check_targets)
        except TargetsUnavailableError as exc:
            return self._partial_targets(gateway, exc)  # type: ignore[arg-type]  # raised by check_targets: the gateway exists
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, SchulSyncError):
                info = exc.to_info()
            elif self._contain:  # a bug while building the gateway: the writes wait, the source data is kept
                log.exception("Unerwarteter Fehler beim Verbinden mit Google")
                info = self._unexpected_info(exc)
            else:
                raise
            log.warning("Google-Ziele nicht verfügbar: %s (%s)", info.message, info.code)
            self._set_halt(info, targets=True)
            return False
        self._gateway = gateway
        self._remember_targets()
        self.calendar_target = TargetStatus(check.calendar_summary or self.calendar_target.name, True)
        self.tasks_target = TargetStatus(check.tasklist_title or self.tasks_target.name, True)
        self._connected = True
        return True

    def _partial_targets(self, gateway: GoogleGateway, exc: TargetsUnavailableError) -> bool:
        """The check found a target unusable (ANFORDERUNGEN 4: only the AFFECTED writes are deferred). The failed target is
        halted with its own error. The calendar is written only after it was verified (not the main calendar, write rights),
        so an unverified calendar waits too; the task list needs no check of its own here: its read (`_ensure_list`) is one,
        and a missing list shows up there. The ids are not remembered (that needs a fully successful check)."""
        first = next(iter(exc.down.values()))
        for target, info in exc.down.items():
            log.warning("Google-Ziel %s nicht verfügbar: %s (%s)", target.value, info.message, info.code)
            self._set_target_down(target, info)
        if Target.CALENDAR in exc.verified:
            self.calendar_target = TargetStatus(self.calendar_target.name, True)
        elif Target.CALENDAR not in exc.down:
            self._set_target_down(Target.CALENDAR, first)
        self._gateway = gateway
        self._connected = True
        return True

    def _ready(self, target: Target) -> bool:
        if self._halt is not None or not self._connect():
            return False
        return self._ensure_list() if target == Target.TASKS else self._calendar_halt is None

    def _ensure_list(self) -> bool:
        if self._tasks_cache is not None:
            return True
        if self._tasks_halt is not None:
            return False
        try:
            self._tasks_cache = list(self._retry(self._gateway.list_tasks))  # type: ignore[union-attr]
        except SchulSyncError as exc:
            info = exc.to_info()
            self._set_target_down(Target.TASKS, info)  # a missing list defers task writes only
            log.warning("Aufgabenliste nicht lesbar: %s (%s)", info.message, info.code)
            if isinstance(exc, GoogleAuthError):
                self._set_halt(info, targets=True)
            return False
        if self.tasks_target.ok is None:  # the calendar failed first, so the check never looked at the list: the read did
            self.tasks_target = TargetStatus(self.tasks_target.name, True)
        return True

    def _reload_tasks(self) -> list[dict[str, Any]]:
        """A FRESH list (reconciliation after an unclear insert); raises SchulSyncError if unreadable."""
        self._tasks_cache = list(self._gateway.list_tasks())  # type: ignore[union-attr]
        return self._tasks_cache

    def _task_in_list(self, task_id: str | None) -> dict[str, Any] | None:
        return next((t for t in self._tasks_cache or [] if t.get("id") == task_id), None)

    def _marker_matches(self, uid: str, tasks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Live (not deleted) tasks carrying this uid's project marker, incl. completed/hidden ones."""
        return [t for t in (tasks if tasks is not None else self._tasks_cache or []) if task_marker_uid(t) == uid and not task_is_deleted(t)]

    def _deleted_marker_tasks(self, uid: str) -> list[dict[str, Any]]:
        """Deleted tasks carrying this uid's marker. NEVER a basis for adoption, only evidence of a deletion."""
        return [t for t in self._tasks_cache or [] if task_marker_uid(t) == uid and task_is_deleted(t)]

    def _remember_task(self, rec: dict[str, Any]) -> None:
        if self._tasks_cache is not None:
            self._tasks_cache = [t for t in self._tasks_cache if t.get("id") != rec.get("id")] + [rec]

    # ------------------------------------------------------------------ intent recovery

    def _recover_intents(self, intents: Sequence[Intent]) -> None:
        for it in intents:
            st = self.store.get(it.uid)
            if st is not None and it.target == Target.CALENDAR and self._calendar_halt is not None:
                continue  # the calendar is down or unverified: the intent stays open, nothing is read from or written to it
            try:
                if st is None:
                    self.store.abort_intent(it.id, "Eintrag nicht mehr vorhanden")
                elif it.target == Target.CALENDAR and it.action == "create":
                    self._recover_event_create(it, st)
                elif it.target == Target.TASKS and it.action == "create":
                    self._recover_task_create(it, st)
                elif it.target == Target.TASKS and it.action == "delete":
                    self._recover_task_delete(it, st)
                else:
                    self.store.abort_intent(it.id, "unbekannte Absicht")
            except SchulSyncError as exc:  # stays open; the item loop reports it (FAILED / UNCERTAIN)
                log.warning("Offene Schreibabsicht %s konnte nicht geklärt werden: %s", it.id, exc.message)
            except Exception:  # noqa: BLE001
                if not self._contain:
                    raise
                log.exception("Unerwarteter Fehler bei der Klärung der Schreibabsicht %s", it.id)  # stays open

    def _recover_event_create(self, it: Intent, st: ItemState) -> None:
        if st.event_state != EventState.NONE:
            self.store.resolve_intent(it.id, "bereits verbucht")
            return
        rec = self._gateway.get_event(it.key)  # type: ignore[union-attr]
        if rec is None:
            self.store.abort_intent(it.id, "Termin wurde nicht angelegt")
        elif event_is_deleted(rec):  # created, then deleted by the user
            st.event_id, st.event_state = it.key, EventState.USER_DELETED
            self._save(st, it.id, "vom Nutzer gelöscht")
            self._notice(Severity.INFO, "user_deleted_event", f"Der Termin „{st.item.title}“ wurde in Google gelöscht und wird nicht neu angelegt.", st.item)
        elif not self._is_ours(rec, st.uid):
            self.store.abort_intent(it.id, "fremder Termin mit gleicher ID")
        else:
            st.event_id, st.event_state, st.event_applied_hash, st.applied_cancelled = it.key, EventState.ACTIVE, it.payload_hash, False
            self._save(st, it.id, "nach unterbrochenem Lauf übernommen")
            self._applied(st, Target.CALENDAR, Action.ADOPT, "Der Termin war schon angelegt (unterbrochener Lauf) und wurde übernommen.")

    def _recover_task_create(self, it: Intent, st: ItemState) -> None:
        if st.task_state not in (TaskState.NONE, TaskState.REMOVED):
            self.store.resolve_intent(it.id, "bereits verbucht")
            return
        if not self._ensure_list():
            return  # stays open; no second task on suspicion
        found = self._marker_matches(rules.task_key(st.item))
        if not found:
            # Only our own create intent proves that WE made a task of this item (the first one; we never delete without
            # state OPEN). For state NONE the deleted marker tasks therefore tell what happened to it (see
            # `_settle_by_tombstones`); after a removal or a restore (REMOVED) an old tombstone proves nothing.
            if st.task_state == TaskState.NONE and self._settle_by_tombstones(st, it.id):
                return
            self.store.abort_intent(it.id, "Aufgabe wurde nicht angelegt")
            return
        if len(found) > 1:
            self._notice(Severity.WARNING, "duplicate_task_marker", "Mehrere Aufgaben tragen dieselbe Projektkennung.", st.item)
        self._adopt_task(st, found[0], it.id, _decode_payload(it.payload_hash), None, Action.ADOPT)

    def _recover_task_delete(self, it: Intent, st: ItemState) -> None:
        if not self._ensure_list():
            return
        rec = self._task_in_list(it.key)
        if rec is None or task_is_deleted(rec):  # our delete went through
            self._mark_task_removed(st, "cancelled")
            self._save(st, it.id, "Aufgabe entfernt")
            self._applied(st, Target.TASKS, Action.REMOVE_TASK, "Die Aufgabe war schon entfernt (unterbrochener Lauf).")
        elif task_is_completed(rec) or task_is_hidden(rec):  # the user finished it first: kept
            st.task_state = TaskState.DONE
            self._save(st, it.id, "inzwischen erledigt", abort=True)
        else:
            self.store.abort_intent(it.id, "Aufgabe wurde nicht gelöscht")

    # ------------------------------------------------------------------ per item

    def _process(self, uid: str) -> None:
        st = self.store.get(uid)
        if st is None or not st.dirty:
            return
        self._stage = Target.CALENDAR
        cal_ok = self._do_event(st)
        self._stage = Target.TASKS
        task_ok = self._do_task(st)
        # An unresolved create/delete intent means Google may hold something the state does not know yet (recovery
        # could not run in this run). The item stays dirty so that the recovery's result is reconciled with the
        # current source state (e.g. a withdrawn item whose orphan event is still live) in the run that resolves it.
        if cal_ok and task_ok and not self._has_open_intent(st):
            st.dirty = False
            self._save(st)

    def _has_open_intent(self, st: ItemState) -> bool:
        return any(it.uid == st.uid for it in self.store.open_intents(st.item.source))

    @staticmethod
    def _is_ours(rec: dict[str, Any], uid: str) -> bool:
        return event_private_props(rec).get(PROP_UID) == uid

    def _blind(self) -> bool:
        """True in a preview whose dry-run gateway could not read Google (no login, offline ...). It then answers every
        read with "nothing there": that is unknowable, never evidence that the user deleted something."""
        return bool(getattr(self._gateway, "reads_unavailable", False))

    # ---- calendar

    def _do_event(self, st: ItemState) -> bool:
        plan = rules.plan_item(st, self._now, self.cfg.rules).event  # type: ignore[arg-type]
        if plan.action == EventAction.NONE:
            self._unchanged(st, Target.CALENDAR)
            return True
        if plan.action == EventAction.SKIP:
            if plan.reason != "no_event_for_kind":  # a kind without calendar entries is not a "skipped entry"
                self._skipped(st, Target.CALENDAR, plan.reason)
            return True
        if not self._ready(Target.CALENDAR):
            return self._defer(st, Target.CALENDAR, plan.change)
        try:
            if plan.action == EventAction.DELETE:
                return self._delete_event(st)
            return self._create_event(st, plan) if plan.action == EventAction.CREATE else self._update_event(st, plan)
        except SchulSyncError as exc:
            return self._fail(st, Target.CALENDAR, plan.change, exc)

    def _delete_event(self, st: ItemState) -> bool:
        """Eduvidual assignments are tasks only: remove the calendar entry SchulSync created earlier. Only an entry that carries
        our project marker for this very item is deleted; a foreign or already deleted one is left alone."""
        assert st.event_id is not None
        rec = self._gateway.get_event(st.event_id)  # type: ignore[union-attr]
        mine = rec is not None and not event_is_deleted(rec) and event_private_props(rec).get(PROP_UID) == st.uid
        if mine:
            self._gateway.delete_event(st.event_id)  # type: ignore[union-attr]
        st.event_state, st.event_id, st.event_applied_hash, st.applied_cancelled = EventState.NONE, None, None, False
        self._save(st)
        if mine:
            self._applied(st, Target.CALENDAR, Action.REMOVE_EVENT, "Kalendertermin entfernt (Eduvidual-Aufgaben stehen nur in Google Tasks).")
        return True

    def _create_event(self, st: ItemState, plan: EventPlan) -> bool:
        d = plan.desired
        assert d is not None
        if st.event_id is None:
            owned = self._own_live_event(st)  # a read: if it fails the item is retried, nothing is created on suspicion
            if owned is not None:
                return self._adopt_owned_event(st, owned)
        eid = st.event_id or self._new_event_id(st)
        body = event_insert_body(d, event_id=eid, description=merge_description(None, d.managed_text))
        intent = self._open_intent(st.uid, Target.CALENDAR, "create", eid, d.content_hash())
        try:
            self._gateway.insert_event(body)  # type: ignore[union-attr]
        except ConflictError:
            return self._adopt_event(st, eid, intent)
        except _DEFINITIVE as exc:
            self.store.abort_intent(intent, exc.code)
            raise
        except SchulSyncError as exc:
            # timeout, 5xx, lost response ...: the event may exist. The intent stays open, the id is deterministic and
            # the next run reconciles with get_event(id) -> no duplicate.
            return self._fail(st, Target.CALENDAR, plan.change, exc, uncertain=True)
        st.event_id, st.event_state, st.event_applied_hash, st.applied_cancelled = eid, EventState.ACTIVE, d.content_hash(), d.cancelled
        self._save(st, intent)
        self._applied(st, Target.CALENDAR, Action.RESTORE if st.uid in self._restored_events else Action.CREATE)
        return True

    def _own_live_event(self, st: ItemState) -> dict[str, Any] | None:
        """ANFORDERUNGEN 7: "vor einem erneuten Anlegen anhand der Projektkennung abgleichen". The deterministic id only finds
        events whose id was derived from this very (uid, generation). An event adopted by identity keeps the id of its old
        uid, and a restored event has the id of a later generation; with the local state gone the id probe misses both and
        would create a second event with the same ``schulsync_uid``. The private project property is the real identity: a LIVE
        event that carries it is ours. Deleted ones (tombstones) are no evidence here, the insert with its 409 sorts that out."""
        found = [
            r
            for r in self._gateway.find_events_by_uid(st.uid)  # type: ignore[union-attr]
            if not event_is_deleted(r)
            and self._is_ours(r, st.uid)
            and r.get("id")
            and self.store.event_id_owner(str(r["id"]), st.uid) is None  # not the event another item was just handed (identity adoption)
        ]
        if len(found) > 1:
            self._notice(
                Severity.WARNING,
                "duplicate_event_property",
                "Mehrere Termine tragen dieselbe Projektkennung; verwendet wird der erste, die übrigen bitte selbst löschen.",
                st.item,
            )
        return sorted(found, key=lambda r: str(r["id"]))[0] if found else None

    def _adopt_owned_event(self, st: ItemState, rec: dict[str, Any]) -> bool:
        """Take over a live event found by its project property and bring it in line with the source (a normal update)."""
        eid = str(rec["id"])
        gen = next((g for g in range(_MAX_EVENT_GEN) if make_event_id(st.uid, g) == eid), None)
        if gen is not None:
            st.event_gen = gen  # a later restore must continue after THIS generation, not hit its (deleted) id
        st.event_id, st.event_state, st.event_applied_hash, st.applied_cancelled = eid, EventState.ACTIVE, None, False
        self._save(st, None, "über die Projektkennung übernommen")
        plan = rules.plan_item(st, self._now, self.cfg.rules).event  # type: ignore[arg-type]
        return self._update_event(st, plan, rec=rec, as_action=Action.ADOPT)

    def _new_event_id(self, st: ItemState) -> str:
        """Deterministic id of the next insert. Identity adoption (DESIGN 4.3.2) hands the event, and with it the id
        derived from the OLD uid, to the new uid; if the old uid later shows up again as a NEW item its own id is
        taken. Then the next generation is used (still deterministic, so recovery by id keeps working)."""
        while True:
            eid = make_event_id(st.uid, st.event_gen)
            if self.store.event_id_owner(eid, st.uid) is None:
                return eid
            st.event_gen += 1

    def _adopt_event(self, st: ItemState, eid: str, intent: int) -> bool:
        """insert answered 409: the id exists. Take over our own event, never touch a foreign one."""
        rec = self._gateway.get_event(eid)  # type: ignore[union-attr]
        # A 409 proves the id is taken, and only a deleted event is unreadable (404/410 -> None) or a tombstone.
        if rec is None or event_is_deleted(rec):  # a deleted event keeps its id reserved: the user deleted ours earlier
            st.event_id, st.event_state = eid, EventState.USER_DELETED
            self._save(st, intent, "vom Nutzer gelöscht")
            self._notice(Severity.INFO, "user_deleted_event", f"Der Termin „{st.item.title}“ wurde in Google gelöscht und wird nicht neu angelegt.", st.item)
            self._skipped(st, Target.CALENDAR, "user_deleted")
            return True
        if not self._is_ours(rec, st.uid):
            self.store.abort_intent(intent, "fremder Termin mit gleicher ID")
            raise BadRequestError("Ein Termin mit dieser ID gehört nicht zu SchulSync und wird nicht verändert.")
        st.event_id, st.event_state, st.event_applied_hash, st.applied_cancelled = eid, EventState.ACTIVE, None, False
        self._save(st, intent, "übernommen")
        plan = rules.plan_item(st, self._now, self.cfg.rules).event  # type: ignore[arg-type]
        return self._update_event(st, plan, rec=rec, as_action=Action.ADOPT)

    def _update_event(self, st: ItemState, plan: EventPlan, *, rec: dict[str, Any] | None = None, as_action: Action | None = None) -> bool:
        d = plan.desired
        assert d is not None and st.event_id is not None
        if rec is None:
            rec = self._gateway.get_event(st.event_id)  # type: ignore[union-attr]  # fresh read = reality check
            if rec is None and self._blind():
                rec = {}  # unknowable: plan the update on an empty base (a preview only shows what would be written)
        if rec is None or event_is_deleted(rec):
            return self._event_gone(st)
        # keep_personal: the 8000 characters are only our own budget; the user's text outside the block is never cut.
        body = event_patch_body(d, description=merge_description(event_description(rec), d.managed_text, keep_personal=True))
        if d.location is None and not _wrote_location(rec):
            body.pop("location")  # the source names no room now: a location SchulSync did not write is the user's own
        _forget_applied(self.store, st, Target.CALENDAR)  # before the patch: its outcome may stay unknown
        try:
            self._gateway.patch_event(st.event_id, body)  # type: ignore[union-attr]
        except NotFoundError:  # deleted between read and write (a patch never revives)
            return self._event_gone(st)
        st.event_applied_hash, st.applied_cancelled = d.content_hash(), d.cancelled
        self._save(st)
        self._applied(st, Target.CALENDAR, as_action or plan.change or Action.UPDATE)
        return True

    def _event_gone(self, st: ItemState) -> bool:
        st.event_state = EventState.USER_DELETED
        self._save(st)
        self._notice(Severity.INFO, "user_deleted_event", f"Der Termin „{st.item.title}“ wurde in Google gelöscht und wird nicht neu angelegt.", st.item)
        self._skipped(st, Target.CALENDAR, "user_deleted")
        return True

    # ---- tasks

    def _reopened(self, st: ItemState) -> None:
        """A finished task that the user ticked off and later UNticked is an open task again (its date / text follow the school
        again). Only when the list is known; a task that stays completed, hidden or deleted is left alone."""
        if st.task_state != TaskState.DONE or not st.task_id or self._blind() or not self._ready(Target.TASKS):
            return
        rec = self._task_in_list(st.task_id)
        if rec is not None and not task_is_completed(rec) and not task_is_hidden(rec) and not task_is_deleted(rec):
            st.task_state = TaskState.OPEN
            self._save(st)

    def _do_task(self, st: ItemState) -> bool:
        self._reopened(st)
        plan = rules.plan_item(st, self._now, self.cfg.rules).task  # type: ignore[arg-type]
        if plan.action == TaskAction.NONE:
            self._task_none(st, plan.reason)
            return True
        action = self._task_action(st, plan)
        if not self._ready(Target.TASKS):
            return self._defer(st, Target.TASKS, action)
        try:
            if plan.action in (TaskAction.UPDATE, TaskAction.REMOVE, TaskAction.COMPLETE) and self._reality_changed(st):
                self._task_none(st, rules.plan_item(st, self._now, self.cfg.rules).task.reason)  # type: ignore[arg-type]
                return True
            if plan.action == TaskAction.CREATE:
                return self._create_task(st, plan, action)
            if plan.action == TaskAction.UPDATE:
                return self._update_task(st, plan)
            if plan.action == TaskAction.COMPLETE:
                return self._complete_task(st)
            return self._remove_task(st)
        except SchulSyncError as exc:
            return self._fail(st, Target.TASKS, action, exc)

    def _task_action(self, st: ItemState, plan: TaskPlan) -> Action:
        if plan.action == TaskAction.COMPLETE:
            return Action.COMPLETE
        if plan.action == TaskAction.REMOVE:
            return Action.REMOVE_TASK
        if plan.action == TaskAction.UPDATE:
            return Action.UPDATE
        if st.uid in self._restored_tasks or st.task_removed_reason == _RESTORED:
            return Action.RESTORE
        return Action.REACTIVATE if plan.reason == "reactivated" else Action.CREATE

    def _task_none(self, st: ItemState, reason: str) -> None:
        if reason == "unchanged":
            self._unchanged(st, Target.TASKS)
        elif reason in _TASK_SKIP_REASONS:
            self._skipped(st, Target.TASKS, reason)

    @staticmethod
    def _verdict(rec: dict[str, Any] | None) -> TaskState | None:
        """Reality of a task: completed (or hidden) first -> DONE, then deleted / gone -> USER_DELETED."""
        if rec is None:
            return TaskState.USER_DELETED
        if task_is_completed(rec) or task_is_hidden(rec):
            return TaskState.DONE
        if task_is_deleted(rec):
            return TaskState.USER_DELETED
        return None

    def _reality_changed(self, st: ItemState) -> bool:
        """Compare an OPEN task with the loaded list; a finished or deleted task is recorded and left alone."""
        if st.task_state != TaskState.OPEN or self._blind():
            return False
        return self._apply_verdict(st, self._verdict(self._task_in_list(st.task_id)))

    def _apply_verdict(self, st: ItemState, verdict: TaskState | None) -> bool:
        if verdict is None:
            return False
        st.task_state = verdict
        self._save(st)
        if verdict == TaskState.USER_DELETED:
            self._notice(Severity.INFO, "user_deleted_task", f"Die Aufgabe „{st.item.title}“ wurde in Google gelöscht und wird nicht neu angelegt.", st.item)
        return True

    def _create_task(self, st: ItemState, plan: TaskPlan, action: Action) -> bool:
        d = plan.desired
        assert d is not None and plan.due is not None
        if self._open_intent_for(st.uid, Target.TASKS, "create") is not None:
            raise OutcomeUnknownError(
                "Ein früherer Anlegeversuch dieser Aufgabe ist noch ungeklärt.",
                hint="Es wird beim nächsten Lauf anhand der Projektkennung abgeglichen; es entsteht keine zweite Aufgabe.",
            )
        found = self._marker_matches(rules.task_key(st.item))
        if found:  # an orphan (state lost, intent aborted ...): adopt, never duplicate
            if len(found) > 1:
                self._notice(Severity.WARNING, "duplicate_task_marker", "Mehrere Aufgaben tragen dieselbe Projektkennung.", st.item)
            return self._adopt_task(st, found[0], None, None, plan, Action.ADOPT)
        if st.task_state == TaskState.NONE and self._settle_by_tombstones(st, None):
            return True  # the user finished or deleted it earlier (the local state forgot): never a new open copy
        sent = _Applied(d.content_hash(), plan.base_deadline, plan.due)
        body = task_insert_body(d, notes=merge_description(None, d.managed_text, limit=TASK_NOTES_LIMIT, html=False))
        intent = self._open_intent(st.uid, Target.TASKS, "create", task_marker_line(rules.task_key(st.item)), _encode_payload(*sent))
        try:
            rec = self._gateway.insert_task(body)  # type: ignore[union-attr]
        except _DEFINITIVE as exc:
            self.store.abort_intent(intent, exc.code)
            raise
        except SchulSyncError as exc:  # may have been applied: reconcile by marker, NEVER insert again on suspicion
            return self._reconcile_task(st, exc, intent, sent, action)
        self._remember_task(rec)
        self._set_open(st, rec["id"], sent)
        self._save(st, intent)
        self._applied(st, Target.TASKS, action)
        return True

    def _settle_by_tombstones(self, st: ItemState, intent: int | None) -> bool:
        """State NONE (the local state has never seen a task of this item, e.g. after a lost database) and no LIVE task with
        the marker: the deleted tasks that carry it are the only trace of what happened in Google (the API lists them with
        showDeleted). ANFORDERUNGEN 6/7: a completed task stays completed, one the user deleted is respected, "auch erledigte,
        ausgeblendete und gelöschte Task-Zustände berücksichtigen".

        * A deleted task that is completed (or hidden) is DONE. SchulSync only ever deletes OPEN tasks, so it was the user's.
        * Otherwise a deleted task WITHOUT SchulSync's removal trace was deleted by the user: USER_DELETED, never re-created
          (``restore`` releases it). One WITH the trace was removed by SchulSync after a cancellation and says nothing about
          the user (a reactivation may create the task again).
        True if the item was settled (state and notice saved), False if the tombstones prove nothing."""
        gone = self._deleted_marker_tasks(rules.task_key(st.item))
        finished = [t for t in gone if task_is_completed(t) or task_is_hidden(t)]
        if finished:
            self._adopt_task(st, finished[0], intent, None, None, Action.ADOPT)
            return True
        if any(not task_removed_by_us(t, st.uid) for t in gone):
            st.task_state = TaskState.USER_DELETED
            self._save(st, intent, "vom Nutzer gelöscht")
            self._notice(
                Severity.INFO, "user_deleted_task", f"Die Aufgabe „{st.item.title}“ wurde in Google gelöscht und wird nicht neu angelegt.", st.item
            )
            self._skipped(st, Target.TASKS, "user_deleted")
            return True
        return False

    def _reconcile_task(self, st: ItemState, exc: SchulSyncError, intent: int, sent: _Applied, action: Action) -> bool:
        try:
            fresh = self._reload_tasks()
        except SchulSyncError:
            return self._fail(st, Target.TASKS, action, exc, uncertain=True)  # list unreadable: intent stays open
        found = self._marker_matches(rules.task_key(st.item), fresh)
        if not found:  # nothing there (yet): the intent stays open, the next run reconciles again
            return self._fail(st, Target.TASKS, action, exc, uncertain=True)
        return self._adopt_task(st, found[0], intent, sent, None, Action.ADOPT)

    def _set_open(self, st: ItemState, task_id: str, applied: _Applied) -> None:
        st.task_id, st.task_state, st.task_attempt = task_id, TaskState.OPEN, st.item.attempt
        st.task_applied_hash, st.task_base_deadline, st.task_due, st.task_removed_reason = applied.hash, applied.base, applied.due, None

    def _adopt_task(
        self,
        st: ItemState,
        rec: dict[str, Any],
        intent: int | None,
        applied: _Applied | None,
        plan: TaskPlan | None,
        action: Action,
    ) -> bool:
        """Take over an existing task. Completed/hidden -> DONE (untouched). Open: with known `applied` data (from
        an intent) as is, otherwise brought in line through a normal update (managed block only)."""
        if task_is_completed(rec) or task_is_hidden(rec):
            st.task_id, st.task_state, st.task_attempt = rec["id"], TaskState.DONE, st.item.attempt
            self._save(st, intent, "bereits erledigt")
            self._applied(st, Target.TASKS, action, "Die vorhandene Aufgabe ist bereits erledigt und bleibt unverändert.")
            return True
        base = (applied.base if applied else None) or (plan.base_deadline if plan else None) or rules.deadline_iso(st.item)
        due = (applied.due if applied else None) or task_due_date(rec) or (plan.due if plan else None)
        self._set_open(st, rec["id"], _Applied(applied.hash if applied else None, base, due))
        self._save(st, intent, "übernommen")
        if applied is not None and applied.hash is not None:
            self._applied(st, Target.TASKS, action, "Die Aufgabe war schon angelegt und wurde übernommen.")
            return True
        sync = rules.plan_item(st, self._now, self.cfg.rules).task  # type: ignore[arg-type]
        if sync.action == TaskAction.UPDATE:
            return self._update_task(st, sync, as_action=action)
        self._applied(st, Target.TASKS, action)
        return True

    def _update_task(self, st: ItemState, plan: TaskPlan, *, as_action: Action | None = None) -> bool:
        d = plan.desired
        assert d is not None and st.task_id is not None
        rec = self._gateway.get_task(st.task_id)  # type: ignore[union-attr]  # fresh read right before the patch
        if rec is None and self._blind():
            rec = {}  # unknowable in a blind preview: plan the update
        if self._apply_verdict(st, self._verdict(rec)):
            self._task_none(st, rules.plan_item(st, self._now, self.cfg.rules).task.reason)  # type: ignore[arg-type]
            return True
        assert rec is not None
        notes = merge_description(task_notes(rec), d.managed_text, limit=TASK_NOTES_LIMIT, html=False)
        _forget_applied(self.store, st, Target.TASKS, due=plan.update_due)  # before the patch: outcome may stay unknown
        try:
            self._gateway.patch_task(st.task_id, task_patch_body(d, notes=notes, include_due=plan.update_due))  # type: ignore[union-attr]
        except NotFoundError:
            self._apply_verdict(st, TaskState.USER_DELETED)
            self._skipped(st, Target.TASKS, "user_deleted")
            return True
        st.task_applied_hash, st.task_base_deadline, st.task_due = d.content_hash(), plan.base_deadline, plan.due
        self._save(st)
        self._applied(st, Target.TASKS, as_action or Action.UPDATE)
        return True

    def _complete_task(self, st: ItemState) -> bool:
        """Submitted in Eduvidual: tick the open task off (fresh read first; a task the user already ticked or deleted
        is only recorded). Afterwards it is DONE like a task the user ticked off: never touched again."""
        assert st.task_id is not None
        rec = self._gateway.get_task(st.task_id)  # type: ignore[union-attr]
        if rec is None and self._blind():
            rec = {}
        if self._apply_verdict(st, self._verdict(rec)):
            self._task_none(st, rules.plan_item(st, self._now, self.cfg.rules).task.reason)  # type: ignore[arg-type]
            return True
        try:
            self._gateway.patch_task(st.task_id, {"status": "completed"})  # type: ignore[union-attr]
        except NotFoundError:
            self._apply_verdict(st, TaskState.USER_DELETED)
            self._skipped(st, Target.TASKS, "user_deleted")
            return True
        st.task_state, st.task_attempt = TaskState.DONE, st.item.attempt
        self._save(st)
        self._applied(st, Target.TASKS, Action.COMPLETE, "In Eduvidual abgegeben: Aufgabe abgehakt.")
        return True

    def _remove_task(self, st: ItemState) -> bool:
        """Cancellation: delete ONLY our open task (completed ones were filtered by the reality check)."""
        assert st.task_id is not None
        # The list was read earlier in the run: ask again right before deleting, the user may have ticked it off.
        rec = self._gateway.get_task(st.task_id)  # type: ignore[union-attr]
        if rec is None and self._blind():
            rec = {}  # unknowable in a blind preview: plan the removal
        if self._apply_verdict(st, self._verdict(rec)):
            self._task_none(st, rules.plan_item(st, self._now, self.cfg.rules).task.reason)  # type: ignore[arg-type]
            return True
        intent = self._open_intent(st.uid, Target.TASKS, "delete", st.task_id, None)
        self._leave_removal_trace(st, rec)
        try:
            self._gateway.delete_task(st.task_id)  # type: ignore[union-attr]
        except _DEFINITIVE as exc:
            self.store.abort_intent(intent, exc.code)
            raise
        self._mark_task_removed(st, "cancelled")
        self._save(st, intent, "Aufgabe entfernt")
        self._applied(st, Target.TASKS, Action.REMOVE_TASK)
        return True

    def _leave_removal_trace(self, st: ItemState, rec: dict[str, Any]) -> None:
        """Right before SchulSync deletes the open task of a cancelled item, write a line into its notes that says so. A
        deleted task stays in the list (showDeleted); without a trace a later run that has lost its database could not tell
        SchulSync's removal from the user's own deletion (and would either re-create what the user deleted or respect nothing).
        Best effort: a failed patch only costs that distinction (the conservative reading, "the user deleted it"); a task whose
        notes are already at the API limit gets no trace for the same reason.

        ponytail: one extra patch per cancellation; the trace lives in the notes of a task that is deleted a moment later."""
        notes = task_notes(rec)
        line = task_removed_line(st.uid)
        if line in notes:
            return
        new = f"{line}\n{notes}" if notes else line
        if len(new) > TASK_NOTES_LIMIT:
            return
        try:
            self._gateway.patch_task(st.task_id, {"notes": new})  # type: ignore[union-attr]  # never a status: a patch stays harmless
        except SchulSyncError as exc:
            log.warning("Entfernungsvermerk für %s konnte nicht geschrieben werden: %s (%s)", st.uid, exc.message, exc.code)

    @staticmethod
    def _mark_task_removed(st: ItemState, reason: str) -> None:
        st.task_state, st.task_removed_reason, st.task_id = TaskState.REMOVED, reason, None
        st.task_applied_hash = st.task_base_deadline = st.task_due = None
