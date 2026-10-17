"""DryRunGateway: preview wrapper that reads for real but NEVER writes to Google.

Reads delegate to the inner gateway. If there is no inner gateway, or an inner read raises a
SchulSyncError (not logged in, offline, target missing ...), the read answers "nothing there" and the
preview remembers it in `reads_unavailable` / `read_errors`, so the report can say that the preview is
based on incomplete knowledge of Google's current state.

Writes never reach the inner gateway. They are recorded in `planned_writes` and answered with results
shaped like the real API answers (Calendar events echoed with status "confirmed", Tasks with fake ids).
Every write is also applied to a private overlay, so later reads inside the same preview see the
simulated state (insert -> get/find/list; patch -> merged record; delete_task -> `deleted: true`, exactly
like the real list with showDeleted). An insert whose id already exists in Google (or in the overlay)
answers ConflictError like the real API, so the engine's ADOPT path is previewed faithfully.

Patch semantics (arrays replace, null deletes) follow the official docs, see google_bodies.apply_patch.
"""

from __future__ import annotations

import copy
from typing import Any, NamedTuple

from .errors import ConflictError, ErrorInfo, NotFoundError, SchulSyncError
from .google_bodies import apply_patch, event_is_deleted
from .google_types import PROP_UID, GoogleGateway, TargetCheck

_FAKE_TARGETS = TargetCheck("dry-run-calendar", "Schule", "dry-run-tasklist", "Schule")


class PlannedWrite(NamedTuple):
    """Same shape as FakeGoogle.write_log: (op, id_or_none, body). `insert_task` has no id yet (None)."""

    op: str
    id: str | None
    body: Any


class DryRunGateway:
    def __init__(self, inner: GoogleGateway | None) -> None:
        self._inner = inner
        self.planned_writes: list[PlannedWrite] = []
        self.read_errors: list[ErrorInfo] = []
        self._no_inner = inner is None
        self._events: dict[str, dict[str, Any]] = {}  # simulated/patched events by id
        self._tasks: dict[str, dict[str, Any]] = {}  # simulated/patched/deleted tasks by id
        self._task_order: list[str] = []  # ids of simulated NEW tasks, in creation order
        self._seq = 0
        self._failed_reads: set[tuple[str, Any]] = set()  # (read fn, args) whose latest read failed

    # ------------------------------------------------------------------ read availability

    @property
    def reads_unavailable(self) -> bool:
        """True if at least one read could not be answered by Google (or there is no inner gateway)."""
        return self._no_inner or bool(self.read_errors)

    def _read(self, fn_name: str, *args: Any, default: Any = None) -> Any:
        if self._inner is None:
            return default
        try:
            result = getattr(self._inner, fn_name)(*args)
        except SchulSyncError as exc:
            self._failed_reads.add((fn_name, args))
            info = exc.to_info()
            if info not in self.read_errors:
                self.read_errors.append(info)
            return default
        self._failed_reads.discard((fn_name, args))
        return result

    def _patch_base(self, base: dict[str, Any] | None, id_: str, what: str, read_fn: str) -> dict[str, Any]:
        """Base for a patch. Google answers 404 for unknown ids (and for deleted events, which a patch must
        never revive). Only if THIS id could not be read (no inner gateway / its own read failed) is the
        miss unknowable and the patch echoed on a bare record; unrelated failed reads do not matter."""
        if base is None:
            if self._inner is not None and (read_fn, (id_,)) not in self._failed_reads:
                raise NotFoundError(f"{what} nicht gefunden (Vorschau, 404).")
            return {"id": id_}
        return base

    def _plan(self, op: str, id_: str | None, body: Any) -> None:
        self.planned_writes.append(PlannedWrite(op, id_, copy.deepcopy(body)))

    # ------------------------------------------------------------------ targets (delegated)

    def check_targets(self) -> TargetCheck:
        """Delegates; a failing check raises, as in a real run. Without inner gateway the targets
        cannot be verified: placeholder answer, `reads_unavailable` is set."""
        if self._inner is None:
            return _FAKE_TARGETS
        return self._inner.check_targets()

    # ------------------------------------------------------------------ events

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        if event_id in self._events:
            return copy.deepcopy(self._events[event_id])
        return self._read("get_event", event_id)

    def insert_event(self, body: dict[str, Any]) -> dict[str, Any]:
        event_id = body.get("id")
        if event_id is None:  # the engine always supplies ids; keep the fake answer shaped like Google's
            self._seq += 1
            event_id = f"dryrun-event-{self._seq:04d}"
        if event_id in self._events or self.get_event(event_id) is not None:
            raise ConflictError("Ein Termin mit dieser ID existiert bereits (Vorschau, 409).")
        rec = copy.deepcopy(body)
        rec["id"] = event_id
        rec.setdefault("status", "confirmed")
        rec.setdefault("kind", "calendar#event")
        self._events[event_id] = rec
        self._plan("insert_event", event_id, body)
        return copy.deepcopy(rec)

    def patch_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        base = self.get_event(event_id)
        if base is not None and event_is_deleted(base):
            raise NotFoundError("Termin wurde gelöscht (Vorschau, 404).")
        base = self._patch_base(base, event_id, "Termin", "get_event")
        rec = apply_patch(base, {k: v for k, v in body.items() if k != "id"})
        self._events[event_id] = rec
        self._plan("patch_event", event_id, body)
        return copy.deepcopy(rec)

    def find_events_by_uid(self, uid: str) -> list[dict[str, Any]]:
        found = {r["id"]: r for r in self._read("find_events_by_uid", uid, default=[])}
        for eid, rec in self._events.items():
            if (rec.get("extendedProperties") or {}).get("private", {}).get(PROP_UID) == uid or eid in found:
                found[eid] = rec
        return copy.deepcopy(list(found.values()))

    # ------------------------------------------------------------------ tasks

    def list_tasks(self) -> list[dict[str, Any]]:
        real = self._read("list_tasks", default=[])
        out = [copy.deepcopy(self._tasks.get(t["id"], t)) for t in real]
        known = {t["id"] for t in real}
        out += [copy.deepcopy(self._tasks[i]) for i in self._task_order if i not in known]
        return out

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        if task_id in self._tasks:
            return copy.deepcopy(self._tasks[task_id])
        return self._read("get_task", task_id)

    def insert_task(self, body: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        tid = f"dryrun-task-{self._seq:04d}"
        rec: dict[str, Any] = {"kind": "tasks#task", "id": tid, "title": body.get("title", ""), "status": body.get("status", "needsAction")}
        for key in ("notes", "due"):
            if body.get(key) is not None:
                rec[key] = body[key]
        self._tasks[tid] = rec
        self._task_order.append(tid)
        self._plan("insert_task", None, body)
        return copy.deepcopy(rec)

    def patch_task(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        base = self._patch_base(self.get_task(task_id), task_id, "Aufgabe", "get_task")
        rec = apply_patch(base, {k: v for k, v in body.items() if k in ("title", "notes", "due", "status")})
        if body.get("status") == "needsAction":  # Google drops these when a task is reopened
            rec.pop("completed", None)
            rec.pop("hidden", None)
        self._tasks[task_id] = rec
        self._plan("patch_task", task_id, body)
        return copy.deepcopy(rec)

    def delete_task(self, task_id: str) -> None:
        base = self.get_task(task_id)
        if base is not None:
            self._tasks[task_id] = {**base, "deleted": True}
        self._plan("delete_task", task_id, None)
