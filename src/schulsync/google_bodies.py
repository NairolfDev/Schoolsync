"""Pure functions: desired state -> Google API request bodies, and readers for API records.

No I/O. Everything here works on plain dicts exactly as exchanged with Calendar v3 / Tasks v1.

Google behaviour this module relies on (official docs, read 2026-10-04):

- Calendar events resource, start/end, reminders, extendedProperties, status values:
  https://developers.google.com/workspace/calendar/api/v3/reference/events
  * All-day events use ``start.date`` / ``end.date`` ("yyyy-mm-dd"); the end date is EXCLUSIVE.
  * Timed events use ``dateTime`` (RFC3339); an offset is required unless ``timeZone`` is given;
    ``timeZone`` is optional for single events.
  * ``reminders.overrides``: at most 5 entries, ``method`` "email"|"popup", ``minutes`` 0..40320.
  * ``status``: "confirmed" | "tentative" | "cancelled" (cancelled = deleted).
- events.insert, custom ids (base32hex: a-v and 0-9, length 5..1024, unique per calendar, a deleted
  event keeps its id reserved): https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
  and https://developers.google.com/workspace/calendar/api/guides/create-events
  (example ``reminders: {useDefault: false, overrides: [...]}`` and ``dateTime`` with offset + ``timeZone``).
- events.patch: "Array fields, if specified, overwrite the existing arrays", unspecified fields stay
  unchanged: https://developers.google.com/workspace/calendar/api/v3/reference/events/patch.
  "To delete a field, specify the field and set it to null" (partial updates, nested objects too):
  https://developers.google.com/workspace/calendar/api/guides/performance
- Private extended properties: key max 44 chars (longer keys are silently dropped), value max 1024 chars
  (silently truncated); a property is removed by patching it to null:
  https://developers.google.com/workspace/calendar/api/guides/extended-properties
- Tasks resource (title <= 1024, notes <= 8192, status "needsAction"|"completed", ``due`` RFC3339 whose
  time portion is discarded, ``completed`` timestamp only when completed, boolean ``deleted`` / ``hidden``):
  https://developers.google.com/workspace/tasks/reference/rest/v1/tasks
  tasks.patch "supports patch semantics":
  https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/patch

NOT documented by Google (so only used defensively, see readers): which fields a deleted-event
tombstone carries (we only rely on ``status == "cancelled"``), and which flags tasks.delete sets.
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from typing import Any

from .models import DesiredEvent, DesiredTask
from .textblocks import find_task_marker, has_removal_trace
from .timeutil import UTC, to_vienna

EVENT_TIME_ZONE = "Europe/Vienna"
MAX_PRIVATE_KEY = 44
MAX_PRIVATE_VALUE = 1024
MAX_REMINDER_MINUTES = 40320
TASK_DUE_SUFFIX = "T00:00:00.000Z"


# --------------------------------------------------------------------------- generic patch semantics


def apply_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """JSON-merge-patch as documented for Calendar patch: dicts merge, ``None`` deletes the key,
    everything else (including lists) replaces. Returns a new dict; inputs are not modified."""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = apply_patch(out[key], value)
        elif isinstance(value, dict):
            out[key] = apply_patch({}, value)  # new object: nulls inside it are simply dropped
        else:
            out[key] = copy.deepcopy(value)
    return out


# --------------------------------------------------------------------------- calendar bodies


def _iso(dt: datetime) -> str:
    return to_vienna(dt).isoformat(timespec="seconds")


def _check_when(desired: DesiredEvent) -> None:
    if desired.all_day:
        if isinstance(desired.end, datetime) or not desired.end > desired.start:  # type: ignore[operator]
            raise ValueError("all-day event needs an exclusive end date after the start date")
    else:
        if not isinstance(desired.end, datetime):
            raise ValueError("timed event needs a datetime end")
        # Compare in UTC: two datetimes sharing one ZoneInfo compare by wall clock (ignoring fold/offset),
        # which is wrong inside the autumn overlap (02:59+02:00 -> really one minute later 02:00+01:00).
        if desired.end.astimezone(UTC) < desired.start.astimezone(UTC):  # type: ignore[union-attr]
            raise ValueError("event end is before its start")


def _when_insert(v: datetime | date) -> dict[str, Any]:
    if isinstance(v, datetime):
        return {"dateTime": _iso(v), "timeZone": EVENT_TIME_ZONE}
    return {"date": v.isoformat()}


def _when_patch(v: datetime | date) -> dict[str, Any]:
    """Like `_when_insert`, but the representation we do not use is nulled so that a switch between
    all-day and timed works (Google keeps unspecified fields, and start with both date and dateTime is
    rejected). `timeZone` is left alone when switching to all-day: it is optional and harmless there."""
    if isinstance(v, datetime):
        return {"dateTime": _iso(v), "timeZone": EVENT_TIME_ZONE, "date": None}
    return {"date": v.isoformat(), "dateTime": None}


def _reminders(popup_minutes: int | None) -> dict[str, Any]:
    if popup_minutes is None:
        return {"useDefault": False, "overrides": []}
    if not 0 <= popup_minutes <= MAX_REMINDER_MINUTES:
        raise ValueError(f"popup minutes out of range 0..{MAX_REMINDER_MINUTES}: {popup_minutes}")
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": popup_minutes}]}


def _private_props(props: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in props.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("private extended properties must be str -> str")
        if len(k) > MAX_PRIVATE_KEY or len(v) > MAX_PRIVATE_VALUE:
            raise ValueError(f"private property {k!r} exceeds Google's size limit (silently dropped/truncated)")
        out[k] = v
    return out


def event_insert_body(desired: DesiredEvent, *, event_id: str, description: str) -> dict[str, Any]:
    """Full events.insert body. `event_id` is the deterministic custom id (see make_event_id)."""
    _check_when(desired)
    body: dict[str, Any] = {
        "id": event_id,
        "status": "confirmed",
        "summary": desired.summary,
        "description": description,
        "start": _when_insert(desired.start),
        "end": _when_insert(desired.end),
        "reminders": _reminders(desired.popup_minutes),
        "extendedProperties": {"private": _private_props(desired.private_props)},
    }
    if desired.location is not None:
        body["location"] = desired.location
    return body


def event_patch_body(desired: DesiredEvent, *, description: str) -> dict[str, Any]:
    """events.patch body for the fields we manage. Never sends id/status (a patch must not revive a
    deleted event); `location: None` clears the field; only our own private keys are touched."""
    _check_when(desired)
    return {
        "summary": desired.summary,
        "description": description,
        "location": desired.location,  # None -> JSON null -> field deleted
        "start": _when_patch(desired.start),
        "end": _when_patch(desired.end),
        "reminders": _reminders(desired.popup_minutes),
        "extendedProperties": {"private": _private_props(desired.private_props)},
    }


# --------------------------------------------------------------------------- task bodies


def task_due_rfc3339(d: date) -> str:
    """Google Tasks keeps only the date of `due`; midnight UTC of that date is the canonical form."""
    if isinstance(d, datetime):  # datetime is a date subclass; isoformat() would give invalid RFC3339
        d = to_vienna(d).date()
    return d.isoformat() + TASK_DUE_SUFFIX


def task_insert_body(desired: DesiredTask, *, notes: str) -> dict[str, Any]:
    return {
        "title": desired.title,
        "notes": notes,
        "due": task_due_rfc3339(desired.due),
        "status": "needsAction",
    }


def task_patch_body(desired: DesiredTask, *, notes: str, include_due: bool) -> dict[str, Any]:
    """Never contains `status` (a completed task must stay completed) and `due` only on request."""
    body: dict[str, Any] = {"title": desired.title, "notes": notes}
    if include_due:
        body["due"] = task_due_rfc3339(desired.due)
    return body


# --------------------------------------------------------------------------- readers: events


def event_is_deleted(rec: dict[str, Any]) -> bool:
    return rec.get("status") == "cancelled"


def event_description(rec: dict[str, Any]) -> str:
    return rec.get("description") or ""


def event_private_props(rec: dict[str, Any]) -> dict[str, str]:
    """Private extended properties; empty for tombstones (Google may omit them on deleted events)."""
    return dict((rec.get("extendedProperties") or {}).get("private") or {})


# --------------------------------------------------------------------------- readers: tasks


def task_due_date(rec: dict[str, Any]) -> date | None:
    """Calendar date of ``due``. Google discards the time portion, so the literal date prefix counts
    (no time-zone conversion: ``2026-10-14T00:00:00.000Z`` is 14 October)."""
    due = rec.get("due")
    if not isinstance(due, str) or len(due) < 10:
        return None
    try:
        return date.fromisoformat(due[:10])
    except ValueError:
        return None


def task_is_completed(rec: dict[str, Any]) -> bool:
    return rec.get("status") == "completed" or bool(rec.get("completed"))


def task_is_deleted(rec: dict[str, Any]) -> bool:
    return bool(rec.get("deleted"))


def task_is_hidden(rec: dict[str, Any]) -> bool:
    return bool(rec.get("hidden"))


def task_notes(rec: dict[str, Any]) -> str:
    return rec.get("notes") or ""


def task_marker_uid(rec: dict[str, Any]) -> str | None:
    """uid from the ``schulsync-id: <uid>`` marker in the notes (first match), else None.

    Delegates to textblocks.find_task_marker so that there is exactly ONE parser: the engine adopts an
    existing task by its marker and two diverging parsers could lead to a duplicate task.

    Guard: ``find_task_marker`` answers a blank uid (" ") for a bare ``schulsync-id: `` line; a blank is
    not a uid."""
    uid = find_task_marker(task_notes(rec))
    return uid.strip() or None if uid else None


def task_removed_by_us(rec: dict[str, Any], uid: str) -> bool:
    """True if the task carries SchulSync's removal trace for `uid` (left right before it deleted the open task of a cancelled
    item). A DELETED task without it was deleted by the user."""
    return has_removal_trace(task_notes(rec), uid)
