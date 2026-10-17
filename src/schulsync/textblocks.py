"""Managed description blocks (merge), clipping, German date format, task marker line.

Pure functions, no I/O.

Why a managed block: SchulSync overwrites title/time/copied data on source changes but must never
touch what the user wrote. Everything the program owns lives between two markers ("Automatisch
übernommen"); everything outside is "Persönliche Notizen" and is preserved byte for byte.

External facts this module relies on (cited):
- Google Calendar API, Events resource, field `description`: "Description of the event. Can contain
  HTML. Optional." No maximum length is documented (https://developers.google.com/workspace/calendar/api/v3/reference/events,
  checked 2026-10-04). Google's web UI therefore may store a manually edited description with
  `<br>`, tags and entities. This is handled by the tolerant marker search and the `<br>` mode below;
  how the real UI rewrites text is NOT verified against a real account (only synthetic fixtures).
- Google Tasks API, Task resource: `notes` max 8192 characters, `title` max 1024 characters
  (https://developers.google.com/tasks/reference/rest/v1/tasks, checked 2026-10-04). No HTML is
  documented for notes, so tasks use the plain mode (`html=False`).
"""

from __future__ import annotations

import html as _html
import re
from datetime import date, datetime

from .google_types import TASK_MARKER_PREFIX
from .timeutil import to_vienna

# --------------------------------------------------------------------------- constants

BEGIN_TEXT = "=== Automatisch übernommen (wird bei jedem Abgleich ersetzt) ==="
END_TEXT = "=== Ende des automatisch übernommenen Bereichs ==="
PERSONAL_HEADER = "=== Persönliche Notizen (hier darfst du schreiben) ==="
CLIP_MARKER = "[… gekürzt]"

DEFAULT_LIMIT = 8000  # description budget we allow ourselves (Calendar documents no limit)
EVENT_DESCRIPTION_LIMIT = DEFAULT_LIMIT
TASK_NOTES_LIMIT = 8192  # Tasks API: notes max 8192 characters (documented)
TITLE_LIMIT = 1024  # Tasks API: title max 1024 characters (documented); we apply it to event summaries too
WEEKDAYS_DE = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")

# Whitespace between marker words may be real whitespace, &nbsp; or HTML tags (Google's editor).
_GAP = r"(?:\s|&nbsp;|&#160;|&#xa0;|<[^>]*>)+"
# Umlauts in marker text may come back from Google as HTML entities (not verified against a real account).
_UMLAUTS = {"ü": "(?:ü|&uuml;|&#252;|&#xfc;)", "ö": "(?:ö|&ouml;|&#246;|&#xf6;)"}


def _marker_regex(text: str) -> re.Pattern[str]:
    words = []
    for w in text.split():
        w = re.escape(w)
        for ch, alt in _UMLAUTS.items():
            w = w.replace(ch, alt)
        words.append(w)
    return re.compile(_GAP.join(words), re.IGNORECASE)


_BEGIN_RE = _marker_regex(BEGIN_TEXT)
_END_RE = _marker_regex(END_TEXT)
_PERSONAL_RE = _marker_regex(PERSONAL_HEADER)
# Line-break-ish HTML. Plain texts we wrote never contain a real line-break tag outside the block.
_HTML_BREAK_RE = re.compile(r"<br\s*/?>|</p\s*>|</div\s*>", re.IGNORECASE)
# A break right after the BEGIN marker, possibly behind closing/formatting tags (`<b>BEGIN</b><br>`).
_LEADING_BR_RE = re.compile(r"(?:\s|<(?!br)[^>]*>)*<br\s*/?>", re.IGNORECASE)
_MARKER_LINE_RE = re.compile(r"^\s*" + re.escape(TASK_MARKER_PREFIX.strip()) + r"\s*(\S.*?)\s*$", re.MULTILINE)
_LINE_BREAK_SPLIT_RE = re.compile(r"\r\n|\r|\n|<br\s*/?>", re.IGNORECASE)


# --------------------------------------------------------------------------- small helpers


def clip(text: str, limit: int) -> str:
    """Clip `text` to at most `limit` characters, ending with a visible "[… gekürzt]" marker.

    If `limit` is smaller than the marker itself the marker alone is returned (it is never cut).
    """
    if len(text) <= limit:
        return text
    if limit <= len(CLIP_MARKER):
        return CLIP_MARKER
    keep = limit - len(CLIP_MARKER) - 1
    return text[:keep].rstrip() + " " + CLIP_MARKER


def format_deadline_de(value: datetime | date) -> str:
    """"Mi., 14.10.2026 23:59" (datetime, Vienna time) or "Mi., 14.10.2026" (date). No locale needed."""
    if isinstance(value, datetime):
        v = to_vienna(value)
        return f"{WEEKDAYS_DE[v.weekday()]}, {v:%d.%m.%Y %H:%M}"
    return f"{WEEKDAYS_DE[value.weekday()]}, {value:%d.%m.%Y}"


def task_marker_line(uid: str) -> str:
    return f"{TASK_MARKER_PREFIX}{uid}"


def find_task_marker(notes: str | None) -> str | None:
    """uid from a `schulsync-id: <uid>` line in task notes (first match), else None."""
    if not notes:
        return None
    for line in _LINE_BREAK_SPLIT_RE.split(notes):
        m = _MARKER_LINE_RE.match(line)
        if m:
            return m.group(1)
    return None


TASK_REMOVED_PREFIX = "schulsync-removed: "


def task_removed_line(uid: str) -> str:
    """The trace SchulSync leaves in a task's notes right before it deletes the OPEN task of a cancelled item. A deleted task
    that carries it was removed by SchulSync (and may come back after a reactivation); one without it was deleted by the user."""
    return f"{TASK_REMOVED_PREFIX}{uid}"


def has_removal_trace(notes: str | None, uid: str) -> bool:
    """True if the notes contain SchulSync's own removal trace for `uid` (see `task_removed_line`)."""
    return bool(notes) and task_removed_line(uid) in {line.strip() for line in _LINE_BREAK_SPLIT_RE.split(notes or "")}


def managed_lines(existing: str | None) -> list[str]:
    """The non-empty lines inside the managed block of an existing description (HTML line breaks, tags and entities
    resolved, so a description that Google's editor rewrote reads the same). [] without a complete block."""
    if not existing:
        return []
    span = _find_block(existing)
    if span is None:
        return []
    inner = existing[span[0] : span[1]]
    begin, ends = _BEGIN_RE.search(inner), list(_END_RE.finditer(inner))
    if begin is None or not ends:
        return []
    inner = inner[begin.end() : ends[-1].start()]
    lines = (_html.unescape(re.sub(r"<[^>]*>", "", line)).strip() for line in _LINE_BREAK_SPLIT_RE.split(inner))
    return [line for line in lines if line]


def _neutralize_markers(text: str) -> str:
    """Make sure copied school text can never be mistaken for one of our markers."""
    for rx in (_BEGIN_RE, _END_RE, _PERSONAL_RE):
        text = rx.sub(lambda m: m.group(0).replace("===", "= = ="), text)
    return text


def render_managed_block(managed_text: str, *, html: bool = False) -> str:
    """BEGIN marker, the managed lines, END marker. `html=True`: `<br>`-joined, HTML-escaped lines."""
    body = _neutralize_markers(managed_text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = body.split("\n")
    if html:
        lines = [_html.escape(line, quote=False) for line in lines]
        return "<br>".join([BEGIN_TEXT, *lines, END_TEXT])
    return "\n".join([BEGIN_TEXT, *lines, END_TEXT])


def _find_block(text: str) -> tuple[int, int] | None:
    """Span of the first complete block: first END that has a BEGIN before it, paired with the
    LAST BEGIN before that END (so a stray earlier BEGIN does not swallow user text)."""
    begins = list(_BEGIN_RE.finditer(text))
    if not begins:
        return None
    for end in _END_RE.finditer(text):
        prior = [b for b in begins if b.end() <= end.start()]
        if prior:
            return prior[-1].start(), end.end()
    return None


def _clip_managed(managed: str, budget: int) -> str:
    """Clip the managed text but keep a trailing task marker line (it identifies the task)."""
    lines = managed.rstrip("\n").split("\n")
    if len(lines) > 1 and lines[-1].startswith(TASK_MARKER_PREFIX):
        marker = lines[-1]
        head = "\n".join(lines[:-1])
        return clip(head, budget - len(marker) - 1) + "\n" + marker if len(managed) > budget else managed
    return clip(managed, budget)


def merge_description(
    existing: str | None,
    managed_text: str,
    *,
    limit: int = DEFAULT_LIMIT,
    html: bool | None = None,
    keep_personal: bool = False,
) -> str:
    """Complete description to send to Google: managed block replaced, everything else preserved.

    - existing empty/None: fresh template (managed block + empty "Persönliche Notizen" section).
    - existing with a complete block: only the span BEGIN..END is replaced; text before/after stays
      byte for byte. Duplicate blocks: the first is replaced, later ones stay as personal text.
    - damaged or missing block (BEGIN without END, END before BEGIN, none at all): a fresh block is
      PREPENDED and the old text is kept entirely.
    - `html`: True = `<br>`-joined, escaped lines; False = plain newlines; None = auto-detect from the
      text outside the block (line-break tags) or the existing block's own separators.
    - `limit`: the managed part is clipped first ("[… gekürzt]"). If the personal text alone is so long
      that even a minimal managed part does not fit, the result is still guaranteed to be <= `limit`
      (Tasks rejects longer notes): the personal text is clipped at its end with the same marker.
      `keep_personal=True` never clips personal text instead (use it for calendar descriptions,
      whose limit is only our own budget); the result may then exceed `limit`.
    - Idempotent: merging the same managed text again returns the identical string.
    """
    if existing is None or not existing.strip():
        mode = bool(html)
        sep = "<br>" if mode else "\n"
        before, after = "", sep * 2 + PERSONAL_HEADER + sep
    else:
        span = _find_block(existing)
        if span is not None:
            before, after = existing[: span[0]], existing[span[1] :]
            if html is None:
                begin = _BEGIN_RE.search(existing, span[0])
                inner = existing[begin.end() : span[1]] if begin else ""
                mode = bool(_HTML_BREAK_RE.search(before + after) or _LEADING_BR_RE.match(inner))
            else:
                mode = html
        else:
            mode = _HTML_BREAK_RE.search(existing) is not None if html is None else html
            sep = "<br>" if mode else "\n"
            before = ""
            after = sep * 2 + ("" if _PERSONAL_RE.search(existing) else PERSONAL_HEADER + sep) + existing

    def assemble(managed: str) -> str:
        return before + render_managed_block(managed, html=mode) + after

    full = assemble(managed_text)
    if len(full) <= limit:
        return full
    floor = len(CLIP_MARKER)
    lo, hi, best = floor, len(managed_text), None  # largest budget whose result fits
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = assemble(_clip_managed(managed_text, mid))
        if len(candidate) <= limit:
            best, lo = candidate, mid + 1
        else:
            hi = mid - 1
    if best is not None:
        return best
    block = render_managed_block(_clip_managed(managed_text, floor), html=mode)
    if keep_personal or len(before) + len(block) + len(after) <= limit:
        return before + block + after
    room = limit - len(block)
    if room <= 0:
        return block  # cannot happen with real limits (block is ~150 chars); never send less than the block
    if len(before) >= room:
        return clip(before, room) + block if room > len(CLIP_MARKER) else block
    after_room = room - len(before)
    return before + block + (clip(after, after_room) if after_room > len(CLIP_MARKER) else "")
