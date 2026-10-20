"""Planner: pure rules that turn a SourceItem + local state into desired Google state and actions.

No I/O. Implements docs/DESIGN.md sections 3, 4, 4.1 and 4.2 (decision tables row by row).
Everything user-visible produced here (titles, description texts) is German.

Important design points:
- Task due date = deadline date minus N CALENDAR days (never 72/168 hours), but never before today
  at first discovery ("late discovery" rule). Already existing overdue tasks are NOT moved forward
  every day: for an unchanged deadline the desired task is built with the due date that was stored
  (`task_due`), so its content hash is stable and no daily update is planned.
- Only a real deadline change (ISO of the deadline differs from `task_base_deadline`) recomputes
  the due date.
- Done / user-deleted tasks and user-deleted events are never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from datetime import date, datetime, time
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from . import textblocks as tb
from .config import RulesConfig
from .google_types import APP_PROP, PROP_KIND, PROP_SOURCE, PROP_UID, TASK_MARKER_PREFIX
from .models import (
    Action,
    DesiredEvent,
    DesiredTask,
    EventState,
    ItemState,
    ItemStatus,
    Kind,
    Source,
    SourceItem,
    TaskState,
)
from .timeutil import UTC, Window, add_real_minutes, make_local, minus_calendar_days, plus_calendar_days, to_vienna, today_vienna

# Copied school descriptions are clipped so that the personal notes keep room (limits of the Google
# APIs are applied again in textblocks.merge_description).
MAX_DESCRIPTION_CHARS = 3000
UNKNOWN_END_TEXT = "Endzeit unbekannt"

_DEADLINE_KINDS = (Kind.ASSIGNMENT, Kind.HOMEWORK)
_SOURCE_LABEL = {Source.EDUVIDUAL: "Eduvidual", Source.WEBUNTIS: "WebUntis"}


# --------------------------------------------------------------------------- plan types


class EventAction(StrEnum):
    NONE = "none"
    CREATE = "create"
    UPDATE = "update"
    SKIP = "skip"
    DELETE = "delete"  # remove our calendar entry (assignments are tasks only)


class TaskAction(StrEnum):
    NONE = "none"
    CREATE = "create"
    UPDATE = "update"
    REMOVE = "remove"
    COMPLETE = "complete"  # submitted in Eduvidual -> tick the open task off


@dataclass(frozen=True)
class EventPlan:
    action: EventAction
    change: Action | None = None  # CREATE / UPDATE / CANCEL / REACTIVATE for the report; None otherwise
    desired: DesiredEvent | None = None  # set for CREATE/UPDATE/NONE, None for SKIP
    reason: str = ""


@dataclass(frozen=True)
class TaskPlan:
    """`due` is the due date the task has/will have after the plan (None for NONE/REMOVE).
    `update_due` tells whether the Google patch must send `due` (CREATE: always True).
    `base_deadline` is the deadline ISO string to store in `task_base_deadline` (CREATE/UPDATE)."""

    action: TaskAction
    desired: DesiredTask | None = None
    update_due: bool = False
    base_deadline: str | None = None
    due: date | None = None
    reason: str = ""


@dataclass(frozen=True)
class ItemPlan:
    event: EventPlan
    task: TaskPlan


# --------------------------------------------------------------------------- basic rules


def lead_days(kind: Kind, cfg: RulesConfig) -> int | None:
    """Calendar days between work start and deadline; None for CANCELLATION (no task)."""
    if kind == Kind.ASSIGNMENT:
        return cfg.assignment_lead_days
    if kind == Kind.HOMEWORK:
        return cfg.homework_lead_days
    if kind == Kind.EXAM:
        return cfg.exam_lead_days
    return None


def deadline_of(item: SourceItem) -> datetime | date:
    """The deadline/start the rules work with (aware Vienna datetime or a plain date)."""
    start = item.when.start
    return to_vienna(start) if isinstance(start, datetime) else start


def deadline_iso(item: SourceItem) -> str:
    """ISO form of the deadline as stored in `ItemState.task_base_deadline`."""
    return deadline_of(item).isoformat()


def _end_known(item: SourceItem) -> datetime | None:
    start, end = item.when.start, item.when.end
    if item.kind in _DEADLINE_KINDS or not isinstance(start, datetime) or end is None:
        return None
    end_v = to_vienna(end)
    # Compare real instants: two aware datetimes sharing one ZoneInfo compare by wall clock (fold
    # ignored), which is wrong in the autumn DST overlap hour.
    return end_v if end_v.astimezone(UTC) > to_vienna(start).astimezone(UTC) else None  # end <= start: bad data


def is_fully_past(item: SourceItem, now: datetime) -> bool:
    """DESIGN 3: deadline kinds: datetime < now / date < today. Exam/cancellation: end <= now if the
    end is known, else datetime start < now (marker) / date < today."""
    now_v = to_vienna(now)
    now_utc = now_v.astimezone(UTC)  # real instants, see _end_known
    start = deadline_of(item)
    if item.kind not in _DEADLINE_KINDS:
        end = _end_known(item)
        if end is not None:
            return end.astimezone(UTC) <= now_utc
    return start.astimezone(UTC) < now_utc if isinstance(start, datetime) else start < today_vienna(now_v)


def _in_window(item: SourceItem, window: Window) -> bool:
    start = deadline_of(item)
    if item.kind in _DEADLINE_KINDS:
        return window.overlaps(start, None)
    return window.overlaps(start, _end_known(item))


def new_item_verdict(item: SourceItem, now: datetime, window: Window) -> tuple[bool, str]:
    """May a NEW item (no state yet) be created? Reasons: ok | already_cancelled | expired | outside_window."""
    if item.status == ItemStatus.CANCELLED:
        return False, "already_cancelled"
    if is_fully_past(item, now):
        return False, "expired"
    if not _in_window(item, window):
        return False, "outside_window"
    return True, "ok"


def task_start_date(item: SourceItem, cfg: RulesConfig) -> date:
    """Task date (Google Tasks has a date only). Eduvidual assignments: the DATE of (deadline - assignment_lead_hours), e.g.
    deadline 22.10. 18:00 -> 12:00 -> 22.10.; a date-only deadline counts as 23:59. Everything else: deadline date minus lead days."""
    if item.kind == Kind.ASSIGNMENT and cfg.assignment_lead_hours:
        d = deadline_of(item)
        dt = to_vienna(d) if isinstance(d, datetime) else make_local(d, time(23, 59))
        return add_real_minutes(dt, -60 * cfg.assignment_lead_hours).date()
    lead = lead_days(item.kind, cfg)
    assert lead is not None
    return minus_calendar_days(item.when.start_date, lead)


def compute_task_due(deadline: datetime | date, lead: int, today: date) -> date:
    """max(deadline date minus `lead` calendar days, today)."""
    d = to_vienna(deadline).date() if isinstance(deadline, datetime) else deadline
    return max(minus_calendar_days(d, lead), today)


# --------------------------------------------------------------------------- text building


def _line(text: str) -> str:
    return " ".join(text.split())


def _multiline(text: str) -> str:
    """Whitespace normalisation only: trim each line, collapse blank-line runs. No content change."""
    out: list[str] = []
    for raw in text.splitlines():
        line = _line(raw)
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


_SECRET_PARAM_RE = re.compile(r"token|sesskey|password|passwd|secret|auth|signature|apikey|api_key", re.IGNORECASE)
# Short names that only count as a WHOLE parameter name (a substring match would drop `assignment=5` or `design=1`).
_SECRET_PARAM_NAMES = frozenset({"sig", "ticket", "sid", "session", "sessionid", "jsessionid", "phpsessid", "pw", "pwd", "credential", "credentials"})
_MARKER_LINE_START_RE = re.compile(r"^(\s*)" + re.escape(TASK_MARKER_PREFIX.strip().rstrip(":")) + r"\s*:", re.IGNORECASE | re.MULTILINE)


def _is_secret_param(name: str) -> bool:
    return bool(_SECRET_PARAM_RE.search(name)) or name.strip().lower() in _SECRET_PARAM_NAMES


def _safe_url(url: str | None) -> str | None:
    """Only absolute http/https URLs without whitespace/control characters. The link ends up in a
    Google calendar that may be shared, so it must carry no secrets: URLs with credentials
    (`user:pass@`) or a token path segment (`tokenpluginfile.php/<token>`) are dropped entirely,
    secret-looking query parameters (token, sesskey, password, ...) are removed."""
    if not url:
        return None
    u = url.strip()
    if not u or any(ch.isspace() or ord(ch) < 32 for ch in u):
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    if "@" in parts.netloc or "tokenpluginfile" in parts.path.lower():
        return None
    query = "&".join(p for p in parts.query.split("&") if not _is_secret_param(p.split("=", 1)[0]))
    if query == parts.query and not parts.fragment:
        return u
    rebuilt = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))  # fragment dropped as well
    return u[: len(parts.scheme)] + rebuilt[len(parts.scheme) :]  # keep the scheme's original case


def _title(item: SourceItem) -> str:
    return _line(item.title) or "(ohne Titel)"


def _when_line(item: SourceItem) -> str:
    label = "Frist" if item.kind in _DEADLINE_KINDS else "Termin"
    start = deadline_of(item)
    text = tb.format_deadline_de(start)
    end = _end_known(item)
    if end is not None and isinstance(start, datetime):
        text += f" bis {end:%H:%M}" if end.date() == start.date() else f" bis {tb.format_deadline_de(end)}"
    return f"{label}: {text}"


def _unknown_end(item: SourceItem) -> bool:
    return item.kind not in _DEADLINE_KINDS and isinstance(item.when.start, datetime) and _end_known(item) is None


def _hints(item: SourceItem) -> list[str]:
    hints = [_line(n) for n in item.notes if n.strip()]
    if _unknown_end(item) and not any(UNKNOWN_END_TEXT in h for h in hints):
        hints.insert(0, UNKNOWN_END_TEXT)
    return hints


def _detail_lines(item: SourceItem) -> list[str]:
    """Course/subject, teacher, room, description, link, hints, source (shared by event and task)."""
    lines: list[str] = []
    course = _line(item.course or "")
    if course:
        lines.append(f"{'Kurs' if item.source == Source.EDUVIDUAL else 'Fach'}: {course}")
    if _line(item.teacher or ""):
        lines.append(f"Lehrkraft: {_line(item.teacher or '')}")
    if _line(item.room or ""):
        lines.append(f"Raum: {_line(item.room or '')}")
    # A copied line starting with the task marker prefix must not be mistaken for our identity line.
    desc = _MARKER_LINE_START_RE.sub(lambda m: m.group(1) + "schulsync id:", _multiline(item.description or ""))
    if desc:
        desc = tb.clip(desc, MAX_DESCRIPTION_CHARS)
        lines.append(("Beschreibung:\n" if "\n" in desc else "Beschreibung: ") + desc)
    url = _safe_url(item.url)
    if url:
        lines.append(f"Link: {url}")
    lines.extend(f"Hinweis: {h}" for h in _hints(item))
    lines.append(f"Quelle: {_SOURCE_LABEL[item.source]}")
    return lines


_EVENT_PREFIX = {Kind.ASSIGNMENT: "Abgabe: ", Kind.HOMEWORK: "Abgabe: ", Kind.CANCELLATION: "Entfall: "}


def _event_times(item: SourceItem) -> tuple[datetime | date, datetime | date]:
    start = deadline_of(item)
    if not isinstance(start, datetime):
        return start, plus_calendar_days(start, 1)  # all day, exclusive end date
    end = _end_known(item)
    return start, end if end is not None else add_real_minutes(start, 1)  # 1-minute marker


def build_desired_event(item: SourceItem, cfg: RulesConfig, *, cancelled: bool) -> DesiredEvent:
    if item.kind == Kind.EXAM:
        prefix = f"{_line(item.subtype or '') or 'Prüfung'}: "
    else:
        prefix = _EVENT_PREFIX[item.kind]
    summary = prefix + _title(item)
    if cancelled:
        summary = "Abgesagt: " + summary
    lines: list[str] = []
    if cancelled:
        lines.append("Status: Von der Quelle als abgesagt bzw. zurückgenommen gemeldet.")
    lines.append(_when_line(item))
    lines.extend(_detail_lines(item))
    start, end = _event_times(item)
    popup = None if cancelled or cfg.reminder_minutes <= 0 else cfg.reminder_minutes
    return DesiredEvent(
        summary=tb.clip(summary, tb.TITLE_LIMIT),
        managed_text="\n".join(lines),
        start=start,
        end=end,
        location=_line(item.room or "") or None,
        popup_minutes=popup,
        private_props={
            APP_PROP: "1",
            PROP_UID: item.uid,
            PROP_SOURCE: item.source.value,
            PROP_KIND: item.kind.value,
        },
        cancelled=cancelled,
    )


def build_desired_task(item: SourceItem, cfg: RulesConfig, due: date) -> DesiredTask:
    """Task for ASSIGNMENT/HOMEWORK ("Bearbeiten: ...") or EXAM ("Lernen: ..."). `due` = Arbeitsbeginn
    (Google Tasks has a due DATE only). The text names the REAL deadline incl. time."""
    lead = lead_days(item.kind, cfg)
    if lead is None:
        raise ValueError(f"no task for kind {item.kind.value}")
    exam = item.kind == Kind.EXAM
    title = ("Lernen: " if exam else "Bearbeiten: ") + _title(item)
    work_start = task_start_date(item, cfg)
    if item.kind == Kind.ASSIGNMENT and cfg.assignment_lead_hours:
        rel = f"{cfg.assignment_lead_hours} Stunden vor der Frist"
    elif lead == 0:
        rel = "am Tag des Termins" if exam else "am Tag der Frist"
    else:
        rel = f"{'1 Tag' if lead == 1 else f'{lead} Tage'} vor {'dem Termin' if exam else 'der Frist'}"
    lines = [_when_line(item), f"Arbeitsbeginn: {tb.format_deadline_de(work_start)} ({rel})"]
    if due > work_start:
        lines.append(
            f"Aufgabendatum in Google Tasks: {tb.format_deadline_de(due)} (der Arbeitsbeginn lag schon in der Vergangenheit)"
        )
    lines.extend(_detail_lines(item))
    lines.append(tb.task_marker_line(task_key(item)))
    return DesiredTask(title=tb.clip(title, tb.TITLE_LIMIT), managed_text="\n".join(lines), due=due)


# --------------------------------------------------------------------------- decision tables


def _plan_event(state: ItemState, now: datetime, cfg: RulesConfig) -> EventPlan:
    """DESIGN 4.1."""
    item, cancelled = state.item, state.cancelled
    if item.kind == Kind.ASSIGNMENT and not cfg.assignment_calendar:  # Eduvidual-Aufgaben: nur Task
        if state.event_state == EventState.ACTIVE and state.event_id:
            return EventPlan(EventAction.DELETE, Action.REMOVE_EVENT, reason="tasks_only")
        return EventPlan(EventAction.SKIP, reason="no_event_for_kind")
    if state.event_state == EventState.USER_DELETED:
        return EventPlan(EventAction.SKIP, reason="user_deleted")
    if state.event_state == EventState.NONE:
        if cancelled:
            return EventPlan(EventAction.SKIP, reason="already_cancelled")
        if is_fully_past(item, now):
            return EventPlan(EventAction.SKIP, reason="expired")
        return EventPlan(
            EventAction.CREATE, Action.CREATE, build_desired_event(item, cfg, cancelled=False), "new"
        )
    desired = build_desired_event(item, cfg, cancelled=cancelled)  # ACTIVE
    if desired.content_hash() == state.event_applied_hash:
        return EventPlan(EventAction.NONE, None, desired, "unchanged")
    if cancelled and not state.applied_cancelled:
        return EventPlan(EventAction.UPDATE, Action.CANCEL, desired, "cancelled")
    if not cancelled and state.applied_cancelled:
        return EventPlan(EventAction.UPDATE, Action.REACTIVATE, desired, "reactivated")
    return EventPlan(EventAction.UPDATE, Action.UPDATE, desired, "changed")


def task_key(item: SourceItem) -> str:
    """Identity of the Google task: the uid, plus '#<attempt>' for a submission the teacher handed back (a second task)."""
    return f"{item.uid}#{item.attempt}" if item.attempt else item.uid


def _plan_task(state: ItemState, now: datetime, cfg: RulesConfig) -> TaskPlan:
    """DESIGN 4.2."""
    item, cancelled = state.item, state.cancelled
    lead = lead_days(item.kind, cfg)
    if lead is None:
        return TaskPlan(TaskAction.NONE, reason="no_task_for_kind")
    ts = state.task_state
    returned = (  # the teacher handed the submission back: a NEW task, the finished one stays finished
        ts == TaskState.DONE and item.attempt is not None and item.attempt > (state.task_attempt or 0)
        and item.submitted is False and not cancelled and not is_fully_past(item, now)
    )
    if ts == TaskState.DONE and not returned:
        return TaskPlan(TaskAction.NONE, reason="done_untouched")
    if ts == TaskState.USER_DELETED:
        return TaskPlan(TaskAction.NONE, reason="user_deleted")

    today = today_vienna(now)
    past = is_fully_past(item, now)
    base = deadline_iso(item)
    due_new = max(task_start_date(item, cfg), today)

    def create(reason: str) -> TaskPlan:
        return TaskPlan(
            TaskAction.CREATE, build_desired_task(item, cfg, due_new), True, base, due_new, reason
        )

    if ts == TaskState.REMOVED:
        if cancelled:
            return TaskPlan(TaskAction.NONE, reason="removed_cancelled")
        if not past:
            return create("reactivated")
        return TaskPlan(TaskAction.NONE, reason="expired")
    if returned:
        return create("resubmission")
    if ts == TaskState.NONE:
        if cancelled:
            return TaskPlan(TaskAction.NONE, reason="already_cancelled")
        if past:
            return TaskPlan(TaskAction.NONE, reason="expired")
        if item.submitted:
            return TaskPlan(TaskAction.NONE, reason="submitted")
        return create("new")

    # OPEN
    if cancelled:
        return TaskPlan(TaskAction.REMOVE, reason="cancelled")
    if item.submitted:
        return TaskPlan(TaskAction.COMPLETE, reason="submitted")
    kept_due = state.task_due if state.task_due is not None else due_new
    if state.task_base_deadline is not None and state.task_base_deadline != base:
        if past:  # moved into the past: keep the due date, refresh title/notes only
            return TaskPlan(
                TaskAction.UPDATE, build_desired_task(item, cfg, kept_due), False, base, kept_due, "deadline_changed"
            )
        return TaskPlan(
            TaskAction.UPDATE, build_desired_task(item, cfg, due_new), True, base, due_new, "deadline_changed"
        )
    # Same deadline: build with the STORED due date so the hash does not drift with `today`.
    desired = build_desired_task(item, cfg, kept_due)
    if desired.content_hash() != state.task_applied_hash:
        return TaskPlan(TaskAction.UPDATE, desired, False, base, kept_due, "text_changed")
    return TaskPlan(TaskAction.NONE, reason="unchanged")


def plan_item(state: ItemState, now: datetime, cfg: RulesConfig) -> ItemPlan:
    """Decide what to do for one item (event and task independently)."""
    return ItemPlan(event=_plan_event(state, now, cfg), task=_plan_task(state, now, cfg))
