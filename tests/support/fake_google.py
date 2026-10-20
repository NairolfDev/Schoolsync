"""FakeGoogle: in-memory test double for `google_types.GoogleGateway` with faithful Google semantics.

It is the engine's test double, so it deliberately behaves like the real APIs, including their awkward
parts. Sources (official docs, read 2026-10-04):

Calendar v3
- Custom event ids: base32hex (a-v, 0-9), length 5..1024, unique per calendar, a deleted event keeps its
  id reserved; an insert with a used id answers 409 "The requested identifier already exists":
  https://developers.google.com/workspace/calendar/api/v3/reference/events/insert and
  https://developers.google.com/workspace/calendar/api/guides/errors
- Patch: arrays replace, unspecified fields stay, null deletes a field (also nested), nested objects merge:
  https://developers.google.com/workspace/calendar/api/guides/performance
- start/end: all-day = `date` with EXCLUSIVE end; timed = `dateTime` (RFC3339, offset or `timeZone`); start
  and end must use the same representation; reminders: <= 5 overrides, minutes 0..40320:
  https://developers.google.com/workspace/calendar/api/v3/reference/events
- Private extended properties: key > 44 chars silently dropped, value > 1024 chars silently truncated,
  filter `privateExtendedProperty=name=value`:
  https://developers.google.com/workspace/calendar/api/guides/extended-properties
Tasks v1
- Server-assigned ids; title <= 1024, notes <= 8192; status needsAction|completed; `due` keeps only the date;
  `completed` timestamp only while completed; read-only flags `deleted` / `hidden`; tasks.list hides completed
  (when showHidden is false), hidden and deleted by default - the real gateway must request all of them:
  https://developers.google.com/workspace/tasks/reference/rest/v1/tasks and
  https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/list

NOT documented by Google, so these are conservative ASSUMPTIONS of this fake (kept pessimistic on purpose):
- A deleted event is a tombstone `{"id", "status": "cancelled"}` for `get_event` (set
  `deleted_event_get_returns_none=True` to model a 410 Gone answer instead).
- `find_events_by_uid` still finds a deleted event but returns it WITHOUT extendedProperties.
- Patching a tombstone answers 404 (NotFoundError); it never revives the event.
- tasks.delete marks the task `deleted: true` (it stays visible with showDeleted=true) and deleting again
  is a no-op; `user_delete_task(purge=True)` models a task that vanished completely.

Fidelity checks that a real server would reject (bad id characters, both date and dateTime, end before
start, ...) raise ValueError: that is a bug in the code under test, not a Google answer.

Conventions
- `write_log`: (op, id_or_none, body) of every mutating call that the "server" accepted (including
  `applied=True` lost-response faults; excluding rejected calls such as a 409, an unavailable target or an
  `applied=False` fault). `insert_task` logs id None (the caller has none); `delete_task` logs body None.
- `calls`: attempts per op (reads and writes, accepted or not).
- Writes that land in the primary calendar (only possible when `calendar_is_primary=True` and the code under
  test ignored a failing `check_targets`) are collected in `misrouted_writes()`.
"""

from __future__ import annotations

import copy
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from schulsync.errors import ConflictError, NotFoundError, SchulSyncError, TargetUnavailableError
from schulsync.google_targets import TargetsUnavailableError
from schulsync.google_types import PROP_UID, TargetCheck
from schulsync.models import Target

READ_OPS = ("check_targets", "get_event", "find_events_by_uid", "list_tasks", "get_task")
WRITE_OPS = ("insert_event", "patch_event", "insert_task", "patch_task", "delete_task")
ALL_OPS = READ_OPS + WRITE_OPS
_EVENT_WRITE_OPS = ("insert_event", "patch_event")

_EVENT_ID_RE = re.compile(r"[a-v0-9]{5,1024}")
_TASK_STATUSES = ("needsAction", "completed")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")  # Calendar all-day `date`: yyyy-mm-dd only
# Tasks `due` must be a full RFC 3339 timestamp (date, time AND offset/Z); a bare date is answered with
# 400 "Invalid value for: Invalid format" (n8n issue 20274, make.com community thread).
_RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})")
Write = tuple[str, str | None, Any]


@dataclass
class _Store:
    """One calendar: live events by id plus tombstones (id -> uid we remember internally)."""

    live: dict[str, dict[str, Any]] = field(default_factory=dict)
    deleted: dict[str, str | None] = field(default_factory=dict)


@dataclass
class _Fault:
    exc: BaseException
    applied: bool
    remaining: int


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Independent re-implementation of Google's patch semantics (not shared with google_bodies)."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict):
            cur = out.get(k)
            out[k] = _merge(cur if isinstance(cur, dict) else {}, v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _parse_dt(v: Any) -> datetime:
    if not isinstance(v, str):
        raise ValueError(f"dateTime must be a string: {v!r}")
    return datetime.fromisoformat(v)


def _norm_private(props: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in props.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("private extended properties must be string -> string")
        if len(k) > 44:
            continue  # silently dropped by Google
        out[k] = v[:1024]  # silently truncated by Google
    return out


class FakeGoogle:
    def __init__(
        self,
        calendar_summary: str = "Schule",
        calendar_exists: bool = True,
        calendar_is_primary: bool = False,
        tasklist_exists: bool = True,
        calendar_id: str = "cal-schule",
        tasklist_id: str = "list-schule",
        *,
        deleted_event_get_returns_none: bool = False,
    ) -> None:
        self.calendar_summary = calendar_summary
        self.calendar_exists = calendar_exists
        self.calendar_is_primary = calendar_is_primary
        self.tasklist_exists = tasklist_exists
        self.calendar_id = calendar_id
        self.tasklist_id = tasklist_id
        self.tasklist_title = "Schule"
        self.deleted_event_get_returns_none = deleted_event_get_returns_none
        self.write_log: list[Write] = []
        self.calls: Counter[str] = Counter()
        self.clock = datetime(2026, 10, 14, 6, 0, 0, tzinfo=timezone.utc)
        self._cal = _Store()
        self._primary = _Store()  # the separate primary calendar; must stay untouched
        self._misrouted: list[Write] = []
        self._tasks: dict[str, dict[str, Any]] = {}
        self._task_seq = 0
        self._event_seq = 0
        self._faults: dict[str, deque[_Fault]] = {}
        self._always: dict[str, BaseException] = {}

    # ------------------------------------------------------------------ fault injection

    @staticmethod
    def _exc(exc: BaseException | type[BaseException]) -> BaseException:
        return exc("Fehler eingespielt (Fake).") if isinstance(exc, type) else exc

    def fail_next(self, op: str, exc: BaseException | type[BaseException], *, applied: bool = False, times: int = 1) -> None:
        """The next `times` calls of `op` raise `exc`. With applied=True the write IS applied first
        (lost response: the caller sees an error although Google stored the change)."""
        if op not in ALL_OPS:
            raise ValueError(f"unknown op {op!r}")
        if applied and op not in WRITE_OPS:
            raise ValueError("applied=True only makes sense for write ops")
        if times < 1:
            raise ValueError("times must be >= 1")
        self._faults.setdefault(op, deque()).append(_Fault(self._exc(exc), applied, times))

    def fail_always(self, op: str, exc: BaseException | type[BaseException]) -> None:
        if op not in ALL_OPS:
            raise ValueError(f"unknown op {op!r}")
        self._always[op] = self._exc(exc)

    def clear_faults(self) -> None:
        self._faults.clear()
        self._always.clear()

    def _pop_fault(self, op: str) -> _Fault | None:
        q = self._faults.get(op)
        if q:
            f = q[0]
            f.remaining -= 1
            if f.remaining <= 0:
                q.popleft()
            return _Fault(f.exc, f.applied, 0)
        if op in self._always:
            return _Fault(self._always[op], False, 0)
        return None

    def _call(self, op: str, fn):
        self.calls[op] += 1
        fault = self._pop_fault(op)
        if fault is None:
            return fn()
        if fault.applied:
            try:
                fn()
            except SchulSyncError:
                pass  # the server rejected it; the client still only sees the injected failure
        raise fault.exc

    # ------------------------------------------------------------------ helpers

    def _stamp(self) -> str:
        self.clock += timedelta(seconds=1)
        return self.clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _store(self) -> _Store:
        if not self.calendar_exists:
            raise TargetUnavailableError(
                f"Der Kalender „{self.calendar_summary}“ wurde nicht gefunden.",
                hint="Kalender in Google Kalender anlegen und `schulsync setup` ausführen.",
            )
        return self._primary if self.calendar_is_primary else self._cal

    def _require_list(self) -> None:
        if not self.tasklist_exists:
            raise TargetUnavailableError(
                f"Die Aufgabenliste „{self.tasklist_title}“ wurde nicht gefunden.",
                hint="Liste in Google Tasks anlegen und `schulsync setup` ausführen.",
            )

    def _log(self, op: str, id_: str | None, body: Any) -> None:
        entry: Write = (op, id_, copy.deepcopy(body))
        self.write_log.append(entry)
        if op in _EVENT_WRITE_OPS and self.calendar_is_primary:
            self._misrouted.append(entry)

    def reset_write_log(self) -> None:
        self.write_log.clear()
        self._misrouted.clear()

    def reset_counters(self) -> None:
        self.calls.clear()

    def writes(self, op: str | None = None) -> list[Write]:
        return [w for w in self.write_log if op is None or w[0] == op]

    def misrouted_writes(self) -> list[Write]:
        """Event writes that landed in the PRIMARY calendar (see module docstring)."""
        return list(self._misrouted)

    # ------------------------------------------------------------------ accessors for assertions

    @property
    def events(self) -> dict[str, dict[str, Any]]:
        """Live (not deleted) events of the target calendar."""
        return copy.deepcopy(self._cal.live)

    @property
    def deleted_event_ids(self) -> set[str]:
        return set(self._cal.deleted)

    @property
    def primary_events(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._primary.live)

    @property
    def tasks(self) -> list[dict[str, Any]]:
        """ALL tasks incl. completed/hidden/deleted, in creation order."""
        return copy.deepcopy(list(self._tasks.values()))

    @property
    def open_tasks(self) -> list[dict[str, Any]]:
        return [t for t in self.tasks if t["status"] == "needsAction" and not t.get("deleted")]

    def task(self, task_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._tasks[task_id])

    # ------------------------------------------------------------------ targets

    def check_targets(self) -> TargetCheck:
        """Like the real gateway: calendar first, then the list, and the error names the target that failed
        (`TargetsUnavailableError`); a failing list leaves the calendar verified."""

        def run() -> TargetCheck:
            try:
                if not self.calendar_exists:
                    self._store()  # raises the "not found" message
                if self.calendar_is_primary:
                    raise TargetUnavailableError(
                        f"„{self.calendar_summary}“ ist der Hauptkalender. Es wird nie in den Hauptkalender geschrieben.",
                        hint="Einen eigenen Kalender „Schule“ anlegen und `schulsync setup` ausführen.",
                    )
            except TargetUnavailableError as exc:
                raise TargetsUnavailableError({Target.CALENDAR: exc.to_info()}) from None
            try:
                self._require_list()
            except TargetUnavailableError as exc:
                raise TargetsUnavailableError({Target.TASKS: exc.to_info()}, verified=(Target.CALENDAR,)) from None
            return TargetCheck(self.calendar_id, self.calendar_summary, self.tasklist_id, self.tasklist_title)

        return self._call("check_targets", run)

    # ------------------------------------------------------------------ events: validation

    @staticmethod
    def _validate_when(name: str, w: Any) -> tuple[str, Any]:
        if not isinstance(w, dict):
            raise ValueError(f"{name} missing")
        d, dt = w.get("date"), w.get("dateTime")
        if (d is None) == (dt is None):
            raise ValueError(f"{name} needs exactly one of date/dateTime, got {w!r}")
        if d is not None:
            if not isinstance(d, str) or not _DATE_RE.fullmatch(d):
                raise ValueError(f"{name}.date must be yyyy-mm-dd, got {d!r}")
            return "date", date.fromisoformat(d)
        parsed = _parse_dt(dt)
        if parsed.tzinfo is None:
            tz = w.get("timeZone")
            if not tz:
                raise ValueError(f"{name}.dateTime needs an offset or timeZone")
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo(tz))
            except (KeyError, TypeError, ValueError, OSError) as exc:  # ZoneInfoNotFoundError is a KeyError
                raise ValueError(f"unknown timeZone {tz!r}") from exc
        return "dateTime", parsed

    def _validate_event(self, rec: dict[str, Any]) -> None:
        sk, sv = self._validate_when("start", rec.get("start"))
        ek, ev = self._validate_when("end", rec.get("end"))
        if sk != ek:
            raise ValueError("start and end must both be date or both dateTime")
        if sk == "date" and not ev > sv:
            raise ValueError("all-day end (exclusive) must be after start")
        if sk == "dateTime" and ev.astimezone(timezone.utc) < sv.astimezone(timezone.utc):
            raise ValueError("end before start")
        rem = rec.get("reminders")
        if rem is not None:
            if not isinstance(rem, dict):
                raise ValueError("reminders must be an object")
            if not isinstance(rem.get("useDefault", False), bool):
                raise ValueError("reminders.useDefault must be bool")
            ov = rem.get("overrides", [])
            if not isinstance(ov, list):
                raise ValueError("reminders.overrides must be a list")
            if len(ov) > 5:
                raise ValueError("at most 5 reminder overrides")
            for o in ov:
                m = o.get("minutes") if isinstance(o, dict) else None
                if (
                    not isinstance(o, dict)
                    or o.get("method") not in ("email", "popup")
                    or not isinstance(m, int)
                    or isinstance(m, bool)
                    or not 0 <= m <= 40320
                ):
                    raise ValueError(f"bad reminder override {o!r}")
        ext = rec.get("extendedProperties")
        if ext is not None:
            if not isinstance(ext, dict):
                raise ValueError("extendedProperties must be an object")
            for scope in ("private", "shared"):
                if scope in ext and not isinstance(ext[scope], dict):
                    raise ValueError(f"extendedProperties.{scope} must be an object")
        if rec.get("status", "confirmed") not in ("confirmed", "tentative", "cancelled"):
            raise ValueError("bad status")
        if rec.get("description") is not None and not isinstance(rec["description"], str):
            raise ValueError("description must be a string")

    @staticmethod
    def _normalise_event(rec: dict[str, Any]) -> None:
        ext = rec.get("extendedProperties")
        if ext:
            if "private" in ext:
                ext["private"] = _norm_private(ext["private"])
            if "shared" in ext:
                ext["shared"] = _norm_private(ext["shared"])

    # ------------------------------------------------------------------ events

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        def run():
            st = self._store()
            if event_id in st.live:
                return copy.deepcopy(st.live[event_id])
            if event_id in st.deleted and not self.deleted_event_get_returns_none:
                return {"id": event_id, "status": "cancelled"}
            return None

        return self._call("get_event", run)

    def insert_event(self, body: dict[str, Any]) -> dict[str, Any]:
        def run():
            st = self._store()
            rec = copy.deepcopy(body)
            eid = rec.get("id")
            if eid is None:
                self._event_seq += 1
                eid = f"gen{self._event_seq:08d}"
                rec["id"] = eid
            if not isinstance(eid, str) or not _EVENT_ID_RE.fullmatch(eid):
                raise ValueError(f"invalid custom event id {eid!r} (allowed: a-v, 0-9, length 5..1024)")
            self._validate_event(rec)
            if eid in st.live or eid in st.deleted:  # Google keeps deleted ids reserved
                raise ConflictError("Ein Termin mit dieser ID existiert bereits (409).")
            self._normalise_event(rec)
            rec.setdefault("status", "confirmed")
            rec.setdefault("reminders", {"useDefault": True})
            rec["kind"] = "calendar#event"
            rec["created"] = rec["updated"] = self._stamp()
            st.live[eid] = rec
            self._log("insert_event", eid, body)
            return copy.deepcopy(rec)

        return self._call("insert_event", run)

    def patch_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        def run():
            st = self._store()
            if event_id not in st.live:  # unknown or tombstone: never revived
                raise NotFoundError("Termin nicht gefunden (404).")
            patch = {k: v for k, v in body.items() if k not in ("id", "kind", "created")}
            rec = _merge(st.live[event_id], patch)
            self._validate_event(rec)
            self._normalise_event(rec)
            rec["updated"] = self._stamp()
            st.live[event_id] = rec
            self._log("patch_event", event_id, body)
            return copy.deepcopy(rec)

        return self._call("patch_event", run)

    def delete_event(self, event_id: str) -> None:
        def run():
            st = self._store()
            rec = st.live.pop(event_id, None)
            if rec is not None:  # Google keeps the id reserved; a second delete / unknown id is an accepted no-op
                st.deleted[event_id] = (rec.get("extendedProperties") or {}).get("private", {}).get(PROP_UID)
            self._log("delete_event", event_id, None)

        self._call("delete_event", run)

    def find_events_by_uid(self, uid: str) -> list[dict[str, Any]]:
        def run():
            st = self._store()
            found = [
                copy.deepcopy(r)
                for r in st.live.values()
                if (r.get("extendedProperties") or {}).get("private", {}).get(PROP_UID) == uid
            ]
            # tombstones match too, but come back stripped of extendedProperties (conservative)
            found += [{"id": i, "status": "cancelled"} for i, u in st.deleted.items() if u == uid]
            return found

        return self._call("find_events_by_uid", run)

    # ------------------------------------------------------------------ tasks: validation

    @staticmethod
    def _validate_task_fields(rec: dict[str, Any]) -> None:
        if len(rec.get("title") or "") > 1024:
            raise ValueError("task title longer than 1024 characters")
        if len(rec.get("notes") or "") > 8192:
            raise ValueError("task notes longer than 8192 characters")
        if rec.get("status", "needsAction") not in _TASK_STATUSES:
            raise ValueError(f"bad task status {rec.get('status')!r}")

    @staticmethod
    def _due(value: str) -> str:
        """Google keeps only the date of `due`; canonical form is midnight UTC. Input must be full RFC 3339."""
        if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
            raise ValueError(f"bad due {value!r}: needs a full RFC 3339 timestamp")
        datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))  # must really parse
        return value[:10] + "T00:00:00.000Z"

    # ------------------------------------------------------------------ tasks

    def list_tasks(self) -> list[dict[str, Any]]:
        def run():
            self._require_list()
            return copy.deepcopy(list(self._tasks.values()))

        return self._call("list_tasks", run)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        def run():
            self._require_list()
            t = self._tasks.get(task_id)
            return copy.deepcopy(t) if t is not None else None

        return self._call("get_task", run)

    def insert_task(self, body: dict[str, Any]) -> dict[str, Any]:
        def run():
            self._require_list()
            self._validate_task_fields(body)
            self._task_seq += 1
            tid = f"task-{self._task_seq:04d}"
            now = self._stamp()
            rec: dict[str, Any] = {
                "kind": "tasks#task",
                "id": tid,
                "etag": f'"{self._task_seq}"',
                "title": body.get("title", ""),
                "updated": now,
                "status": body.get("status", "needsAction"),
                "links": [],
            }
            if body.get("notes") is not None:
                rec["notes"] = body["notes"]
            if body.get("due") is not None:
                rec["due"] = self._due(body["due"])
            if rec["status"] == "completed":
                rec["completed"] = now
            self._tasks[tid] = rec
            self._log("insert_task", None, body)
            return copy.deepcopy(rec)

        return self._call("insert_task", run)

    def patch_task(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        def run():
            self._require_list()
            t = self._tasks.get(task_id)
            if t is None:
                raise NotFoundError("Aufgabe nicht gefunden (404).")
            patch = {k: v for k, v in body.items() if k in ("title", "notes", "due", "status")}
            new = _merge(t, patch)
            self._validate_task_fields(new)
            if "due" in new and "due" in patch:
                new["due"] = self._due(patch["due"])
            new["status"] = new.get("status", "needsAction")
            if new["status"] == "completed" and t["status"] != "completed":
                new["completed"] = self._stamp()
            if new["status"] == "needsAction":
                new.pop("completed", None)
                new.pop("hidden", None)
            new["updated"] = self._stamp()
            self._tasks[task_id] = new
            self._log("patch_task", task_id, body)
            return copy.deepcopy(new)

        return self._call("patch_task", run)

    def delete_task(self, task_id: str) -> None:
        def run():
            self._require_list()
            t = self._tasks.get(task_id)
            if t is not None and not t.get("deleted"):
                t["deleted"] = True
                t["updated"] = self._stamp()
            self._log("delete_task", task_id, None)  # unknown id / second delete: accepted no-op

        self._call("delete_task", run)

    # ------------------------------------------------------------------ simulated user actions (not logged)

    def user_complete_task(self, task_id: str) -> None:
        t = self._tasks[task_id]
        t["status"] = "completed"
        t["completed"] = self._stamp()
        t["updated"] = self._stamp()

    def user_reopen_task(self, task_id: str) -> None:
        t = self._tasks[task_id]
        t["status"] = "needsAction"
        t.pop("completed", None)
        t.pop("hidden", None)
        t["updated"] = self._stamp()

    def user_clear_completed(self) -> None:
        """"Clear completed" in the Tasks app: completed tasks become hidden."""
        for t in self._tasks.values():
            if t["status"] == "completed":
                t["hidden"] = True

    def user_delete_task(self, task_id: str, purge: bool = False) -> None:
        if task_id not in self._tasks:
            raise KeyError(task_id)
        if purge:
            del self._tasks[task_id]
        else:
            self._tasks[task_id]["deleted"] = True
            self._tasks[task_id]["updated"] = self._stamp()

    def user_edit_task_notes(self, task_id: str, text: str) -> None:
        self._tasks[task_id]["notes"] = text
        self._tasks[task_id]["updated"] = self._stamp()

    def user_move_task_due(self, task_id: str, due: date) -> None:
        self._tasks[task_id]["due"] = due.isoformat() + "T00:00:00.000Z"
        self._tasks[task_id]["updated"] = self._stamp()

    def user_delete_event(self, event_id: str) -> None:
        st = self._store()
        rec = st.live.pop(event_id)  # KeyError for unknown ids
        st.deleted[event_id] = (rec.get("extendedProperties") or {}).get("private", {}).get(PROP_UID)

    def user_edit_event_description(self, event_id: str, text: str) -> None:
        rec = self._store().live[event_id]
        rec["description"] = text
        rec["updated"] = self._stamp()

    def user_edit_event_summary(self, event_id: str, text: str) -> None:
        rec = self._store().live[event_id]
        rec["summary"] = text
        rec["updated"] = self._stamp()
