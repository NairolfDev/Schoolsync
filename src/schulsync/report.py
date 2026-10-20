"""HTML run report (DESIGN section 9, ANFORDERUNGEN section 8).

`render_html` builds one self-contained German HTML5 page from a `models.RunReport`.
Everything that comes from a school platform (titles, courses, error details, URLs) is
HOSTILE input: it is stripped of control/invisible characters, clipped, passed through
`_sanitize` (secret masking), clipped again, and `html.escape`d. The page has no scripts and no
external resources; a strict CSP `<meta>` backs this up.

`write_report` stores one file per Vienna day (`bericht-YYYY-MM-DD.html`, overwritten by
later runs of the same day) plus a plain copy `latest.html`. A PREVIEW report never replaces
those: it goes to `vorschau.html`, so a preview cannot erase the only record of a failed run.
`open_report` opens a file in the default browser of the current user session and never raises.

Honesty note: layout and wording were only checked against synthetic `RunReport` fixtures
(tests/test_report.py); no real run data was available when this module was written.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from urllib.parse import urlsplit

from . import models as m
from .config import Paths
from .errors import ErrorInfo
from .timeutil import VIENNA, today_vienna, to_vienna

__all__ = ["render_html", "write_report", "report_file_for", "prune_reports", "open_report", "latest_report", "safe_url"]

logger = logging.getLogger("schulsync.report")

_REPORT_RE = re.compile(r"^bericht-(\d{4})-(\d{2})-(\d{2})\.html$")
_TMP_RE = re.compile(r"^(?:bericht-.+\.html|latest\.html|vorschau\.html)\.\d+\.tmp$")
_TITLE_DAY_RE = re.compile(r"<title>SchulSync-Bericht (\d{2})\.(\d{2})\.(\d{4})</title>")
_PREVIEW_NAME = "vorschau.html"
# INFO notices with this code carry entries that an EARLIER start of the same day already took over into Google (the runner
# adds one per entry for a source that is not fetched again). The report file of a day is overwritten by every later start, so
# without them a retry report would claim "Keine." although the data is in Google. They are shown in the section "Bereits
# erfolgreich übernommen", not among the notices.
NOTICE_TAKEN_OVER = "already_taken_over"
_MAX_ROWS = 300  # rows per table; the rest is summarised ("... und N weitere")

# --------------------------------------------------------------------------- secret masking

_KEYS = (
    r"(?:pass(?:word|wort|wd)?|pwd|[a-z_]*token|sesskey|[a-z_]*secret|api[_-]?key|authorization"
    r"|jsessionid|sessionid|moodlesession\w*)"
)
# whole rest of the line is the value (several tokens / "k=v; k=v")
_LINE_SECRET_RE = re.compile(r"(?i)(?<![A-Za-z0-9])((?:set-)?cookie|authorization)(\s*[:=]\s*)[^\r\n]+")
_KV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(" + _KEYS + r")([\"']?\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s&;,\"'<>]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
_USERINFO_RE = re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]*@")
_TOKENISH_RE = re.compile(r"\bya29\.[\w-]+|\beyJ[\w-]{8,}\.[\w-]+\.[\w-]+|\bAIza[0-9A-Za-z_-]{30,}")


def _mask_fallback(text: str) -> str:
    """Conservative built-in masking (used on top of redact.redact and when it is missing).

    Known limitation: it matches `key=value` / `key: value` shapes and a few token formats; it
    cannot know an arbitrary secret. Over-masking (e.g. a title "Passwort: Test") is accepted.
    """
    text = _LINE_SECRET_RE.sub(lambda mo: f"{mo.group(1)}{mo.group(2)}***", text)
    text = _KV_RE.sub(lambda mo: f"{mo.group(1)}{mo.group(2)}***", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _USERINFO_RE.sub(lambda mo: f"{mo.group(1)}***@", text)
    return _TOKENISH_RE.sub("***", text)


def _sanitize(text: str) -> str:
    """Mask secrets. Uses `redact.redact` (written by another module) when importable at call
    time, then ALWAYS applies the built-in masking as a second layer."""
    try:
        from .redact import redact  # lazy on purpose: module may be missing/half-written

        masked = redact(text)
        if isinstance(masked, str):
            text = masked
    except Exception:  # noqa: BLE001 - any failure of the hook must fall back, never break the report
        pass
    return _mask_fallback(text)


# --------------------------------------------------------------------------- escaping helpers

# Stripped from every foreign string: control chars (except \n and \t), soft hyphen, Arabic letter
# mark, Mongolian vowel separator, zero-width / bidi / line-separator / invisible-operator
# characters (U+200B-200F, U+2028-202E, U+2060-206F), BOM, interlinear annotation marks and lone
# surrogates (json.loads can produce them and they make str.encode("utf-8") raise).
_STRIP_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f­؜᠎​-‏ -‮⁠-⁯"
    r"﻿￹-￻\ud800-\udfff]"
)
_MAX_PRE_CLIP = 20_000  # foreign text is clipped to at most this before masking (redact can be superlinear)
_SAFE_TAIL = 512  # see _masked_head; assumes no registered secret is longer than this
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_BAD_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f<>\"\\]")


def _masked_head(raw: str, limit: int) -> tuple[str, bool]:
    """Mask the start of `raw` after clipping it (one huge hostile field must not stall redact).

    Returns (masked text, was_clipped). The clip may cut a secret in half; masking cannot recognise
    the stub, and because masking shrinks text the stub could end up inside the displayed part. The
    stub always sits at the very end of the masked text, so when clipped the last _SAFE_TAIL
    characters are dropped after masking (a secret cut by the clip is shorter than that)."""
    take = min(limit * 4, _MAX_PRE_CLIP)
    clipped = len(raw) > take + _SAFE_TAIL
    text = _sanitize(raw[: take + _SAFE_TAIL] if clipped else raw)
    if clipped:
        text = text[: max(0, len(text) - _SAFE_TAIL)]
    return text, clipped


def _clean(value: object, limit: int) -> str:
    text, clipped = _masked_head(_STRIP_RE.sub("", "" if value is None else str(value)), limit)
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text + "…" if clipped else text


def _t(value: object, limit: int = 300) -> str:
    """Escaped, sanitized, clipped text for HTML content or attribute values."""
    return html.escape(_clean(value, limit), quote=True)


def safe_url(url: object) -> str | None:
    """Return `url` if it is a plain http(s) URL without credentials, else None."""
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u or len(u) > 2000 or _BAD_URL_CHARS.search(u):
        return None
    try:
        parts = urlsplit(u)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return u


def _anchor(url: str) -> str:
    """Link for a URL found in text; plain escaped text unless the URL is valid and secret-free."""
    shown = _STRIP_RE.sub("", url)
    ok = safe_url(shown)
    if ok is None or "***" in ok or _sanitize(ok) != ok:  # masked/secret-bearing URL: text only
        return html.escape(_clean(url, 300), quote=True)
    label = html.escape(shown if len(shown) <= 80 else shown[:79] + "…", quote=True)
    return f'<a href="{html.escape(ok, quote=True)}" rel="noopener noreferrer" target="_blank">{label}</a>'


def _rich(value: object, limit: int = 600) -> str:
    """Like `_t`, but http(s) URLs inside the text become safe links.

    Links are built from the complete URL; only the surrounding plain text is clipped to `limit`
    characters (link labels are shortened to 80 characters each). A URL that touches the end of a
    pre-clipped text may be incomplete and is shown as plain text, never linked."""
    text, precut = _masked_head(_STRIP_RE.sub("", "" if value is None else str(value)), limit)
    out: list[str] = []
    used = 0  # plain characters emitted so far
    pos = 0

    def plain(chunk: str) -> bool:
        """Emit clipped plain text; False once the budget is exhausted."""
        nonlocal used
        room = limit - used
        out.append(html.escape(chunk[:room], quote=True))
        used += min(len(chunk), room)
        if len(chunk) > room:
            out.append("…")
            return False
        return True

    for mo in _URL_RE.finditer(text):
        url = mo.group(0)
        trail = ""
        while url and (url[-1] in ".,;:!?…" or (url[-1] in _CLOSERS and url.count(url[-1]) > url.count(_CLOSERS[url[-1]]))):
            trail = url[-1] + trail  # a closing bracket stays in the URL when it has its opener inside
            url = url[:-1]
        if not plain(text[pos : mo.start()]):
            return "".join(out)
        out.append(html.escape(url, quote=True) if precut and mo.end() == len(text) else _anchor(url))
        if not plain(trail):
            return "".join(out)
        pos = mo.end()
    plain(text[pos:])
    return "".join(out)


# --------------------------------------------------------------------------- German labels

_TZ_DE = {"CEST": "MESZ", "CET": "MEZ"}

_OVERALL = {
    m.OverallStatus.SUCCESS: ("ok", "Erfolgreich", "Alles wurde erfolgreich synchronisiert."),
    m.OverallStatus.PARTIAL: (
        "warn",
        "Teilweise fehlgeschlagen",
        "Nicht alles konnte übernommen werden. Bereits Übernommenes bleibt erhalten, der Rest wird erneut versucht.",
    ),
    m.OverallStatus.FAILED: (
        "err",
        "Fehlgeschlagen",
        "Der Lauf ist fehlgeschlagen. Was bereits erledigt wurde, steht unter 'Bereits erfolgreich übernommen'; ausstehende Vorgänge werden erneut versucht.",
    ),
    m.OverallStatus.NOTHING_TO_DO: (
        "info",
        "Nichts zu tun",
        "Heute wurde bereits erfolgreich synchronisiert.",
    ),
}
_SOURCE_STATUS = {
    m.SourceStatus.SUCCESS: ("ok", "Erfolgreich"),
    m.SourceStatus.PARTIAL: ("warn", "Teilweise fehlgeschlagen"),
    m.SourceStatus.FAILED: ("err", "Fehlgeschlagen"),
    m.SourceStatus.ALREADY_DONE: ("info", "Heute bereits erledigt"),
    m.SourceStatus.DISABLED: ("off", "Abgeschaltet"),
}
_PREVIEW_SOURCE_STATUS = {
    m.SourceStatus.SUCCESS: "Vorschau: Abruf ok",
    m.SourceStatus.ALREADY_DONE: "Vorschau: heute bereits erledigt",
}
_CATEGORY_STATUS = {
    m.CategoryStatus.OK: ("ok", "OK"),
    m.CategoryStatus.FAILED: ("err", "Fehlgeschlagen"),
    m.CategoryStatus.INCOMPLETE: ("warn", "Unvollständig"),
    m.CategoryStatus.DISABLED: ("off", "Abgeschaltet"),
}
_CHANGE_STATUS = {
    m.ChangeStatus.APPLIED: ("ok", "Erledigt"),
    m.ChangeStatus.PLANNED: ("info", "Geplant"),
    m.ChangeStatus.FAILED: ("err", "Fehlgeschlagen"),
    m.ChangeStatus.DEFERRED: ("warn", "Zurückgestellt"),
    m.ChangeStatus.UNCERTAIN: ("warn", "Ausgang ungeklärt"),
}
_SOURCE_NAME = {m.Source.EDUVIDUAL: "Eduvidual", m.Source.WEBUNTIS: "WebUntis"}
_CATEGORY_NAME = {
    m.Category.ASSIGNMENTS: "Aufgaben und Abgaben",
    m.Category.HOMEWORK: "Hausaufgaben",
    m.Category.EXAMS: "Prüfungen und Schularbeiten",
    m.Category.CANCELLATIONS: "Unterrichtsentfälle",
}
_KIND_NAME = {
    m.Kind.ASSIGNMENT: "Abgabe",
    m.Kind.HOMEWORK: "Hausaufgabe",
    m.Kind.EXAM: "Prüfung",
    m.Kind.CANCELLATION: "Unterrichtsentfall",
}
_TARGET_NAME = {m.Target.CALENDAR: "Kalender", m.Target.TASKS: "Aufgabenliste"}
_ACTION_NAME = {
    m.Action.CREATE: "Anlegen",
    m.Action.UPDATE: "Aktualisieren",
    m.Action.CANCEL: "Als abgesagt markieren",
    m.Action.REACTIVATE: "Reaktivieren",
    m.Action.REMOVE_TASK: "Offene Aufgabe entfernen",
    m.Action.ADOPT: "Vorhandenen Eintrag übernehmen",
    m.Action.RESTORE: "Wiederherstellen",
    m.Action.SKIP: "Übersprungen",
    m.Action.COMPLETE: "Abgehakt (in Eduvidual abgegeben)",
    m.Action.REMOVE_EVENT: "Kalendertermin entfernen (nur noch als Task)",
}
_MODE_NAME = {m.RunMode.AUTO: "Automatisch", m.RunMode.MANUAL: "Manuell", m.RunMode.PREVIEW: "Vorschau"}
_SKIP_REASONS = {
    "expired": "Frist/Ereignis bereits vorbei",
    "outside_window": "außerhalb des Prüffensters",
    "already_cancelled": "bereits abgesagt",
    "user_deleted": "in Google bewusst gelöscht",
    "done_untouched": "Aufgabe in Google bereits erledigt (nicht angefasst)",
    "removed_cancelled": "Aufgabe bereits entfernt, Eintrag abgesagt",
    "no_task_for_kind": "für diese Art ist keine Aufgabe vorgesehen",
    "unchanged": "unverändert",
    "cancelled": "abgesagt",
    "submitted": "in Eduvidual schon abgegeben",
}
_SKIP_REASON_FALLBACK = "sonstiger Grund"
_SEVERITY_ORDER = (
    (m.Severity.ERROR, "err", "Fehler"),
    (m.Severity.WARNING, "warn", "Warnungen"),
    (m.Severity.INFO, "info", "Hinweise"),
)
_NOTICE_HINTS = {
    "no_date": "Der Eintrag hat kein Datum und wurde nicht übernommen. Er erscheint, sobald die Schule ein Datum einträgt.",
    "incomplete_data": "Angaben fehlen oder sind abgeschnitten. Was damit geschehen ist, steht in der Meldung; bitte in der Schulplattform nachsehen.",
    "user_deleted_event": "Dieser Termin wurde in Google gelöscht und wird nicht neu angelegt. Mit 'schulsync restore' lässt er sich wiederherstellen.",
    "user_deleted_task": "Diese Aufgabe wurde in Google gelöscht und wird nicht neu angelegt. Mit 'schulsync restore' lässt sie sich wiederherstellen.",
    "user_deleted": "Dieser Eintrag wurde in Google gelöscht und wird nicht neu angelegt. Mit 'schulsync restore' lässt er sich wiederherstellen.",
    "identity_uncertain": "Der Eintrag wurde nur über seinen Inhalt zugeordnet (keine feste Kennung der Schule). Bitte kontrollieren, ob er stimmt.",
    "duplicate_in_source": "Derselbe Eintrag kam in der Quelle doppelt vor; nur einer wurde übernommen.",
    "duplicate_task_marker": "In Google Tasks gibt es mehrere Aufgaben mit derselben Projektkennung. Verwendet wird die erste nicht gelöschte; überzählige bitte selbst löschen.",
    "withdrawn_confirmed": "Die Schule hat diesen Eintrag ausdrücklich zurückgenommen.",
    "restore_blocked": "Abgesagte und bereits vergangene Einträge werden nie neu angelegt; deshalb bleibt dieser Eintrag, wie er war (in 'schulsync status' weiter als gelöscht aufgeführt).",
    "restore_nothing": "Nur Einträge, die du selbst in Google gelöscht hast, lassen sich wiederherstellen. 'schulsync status' listet sie mit ihrer Kennung auf.",
    "targets_changed": "Alte Einträge im früheren Kalender bzw. in der früheren Aufgabenliste werden von SchulSync nicht mehr angefasst und können dort von Hand gelöscht werden.",
}

# error code -> generic next step (fixed order = output order)
_CODE_STEPS: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset({"network", "transient"}),
        "Internetverbindung prüfen (Browser, WLAN/Netzkabel). Danach 'schulsync run' ausführen oder den Rechner neu anmelden.",
    ),
    (
        frozenset({"outcome_unknown"}),
        "Bei unklarem Ausgang einfach erneut synchronisieren: Beim nächsten Lauf wird anhand der Projektkennung abgeglichen, es entstehen keine Doppelten.",
    ),
    (
        frozenset({"google_auth"}),
        "Google-Anmeldung erneuern: 'schulsync setup' erneut ausführen. Hinweis: Steht die OAuth-App in Google Cloud auf 'Testing', läuft die Anmeldung nach 7 Tagen ab; SchulSync öffnet dann beim nächsten Lauf den Google-Login (alternativ die App auf 'In Produktion' stellen).",
    ),
    (
        frozenset({"auth"}),
        "Anmeldedaten der Schulplattform prüfen (Benutzername/Passwort ggf. geändert?) und 'schulsync setup' erneut ausführen. Automatische Läufe wiederholen abgelehnte Anmeldungen nicht endlos.",
    ),
    (
        frozenset({"access_denied"}),
        "Berechtigungen prüfen: Der Zugriff wurde verweigert. 'schulsync check' zeigt, welche Kategorie oder welches Ziel betroffen ist.",
    ),
    (
        frozenset({"schema"}),
        "Die Plattform hat sich offenbar geändert (unerwartete Antwort). 'schulsync check' ausführen und das Ergebnis bei Bedarf dem Entwickler melden.",
    ),
    (
        frozenset({"incomplete"}),
        "Der Abruf war unvollständig; deshalb werden keine Einträge aus dieser Quelle gelöscht oder abgesagt. Später erneut versuchen ('schulsync run').",
    ),
    (
        frozenset({"target_unavailable"}),
        "Kalender 'Schule' und Aufgabenliste 'Schule' in Google prüfen (vorhanden? richtiges Konto?). Es wird nie auf einen anderen Kalender oder eine andere Liste ausgewichen; zurückgestellte Vorgänge folgen automatisch, sobald das Ziel wieder erreichbar ist.",
    ),
    (
        frozenset({"config"}),
        "Konfigurationsdatei config.toml prüfen oder 'schulsync setup' erneut ausführen.",
    ),
    (
        frozenset({"run_timeout", "timeout"}),
        "Das Zeitlimit für den Lauf wurde erreicht (oder ein Aufruf hat nicht mehr geantwortet). Bereits Übernommenes bleibt erhalten, "
        "der Rest wird beim nächsten Start nachgeholt. Tritt das wiederholt auf: Internetverbindung prüfen und 'schulsync check' ausführen.",
    ),
    (
        frozenset({"unexpected"}),
        "Im Programm ist ein unerwarteter Fehler aufgetreten. Die technischen Details stehen in der Logdatei (Ordner 'logs' im "
        "SchulSync-Datenordner); für eine Fehlermeldung bitte den Fehlertext und die zugehörigen Logzeilen kopieren. "
        "Der nächste Start versucht es erneut.",
    ),
    (
        frozenset({"targets_changed"}),
        "Kalender oder Aufgabenliste wurden gewechselt: Die Einträge werden in den neuen Zielen neu angelegt, die früheren Ziele bleiben "
        "unverändert. War das nicht beabsichtigt, die Einrichtung mit 'schulsync setup --google' prüfen.",
    ),
)
_CODES_WITH_STEP = frozenset().union(*(codes for codes, _ in _CODE_STEPS))
_GENERIC_STEP = (
    "Es ist ein Fehler aufgetreten, der sich nicht genauer einordnen ließ. 'schulsync check' ausführen und die "
    "technischen Details oben ansehen. Der nächste Lauf versucht es erneut."
)

# --------------------------------------------------------------------------- formatting helpers


def _fmt_dt(dt: datetime | None, *, long: bool = False, seconds: bool = False) -> str:
    if dt is None:
        return "unbekannt"
    if dt.tzinfo is None or dt.utcoffset() is None:  # contract violation: show it, do not crash
        return dt.strftime("%d.%m.%Y %H:%M") + " (Zeitzone unbekannt)"
    v = to_vienna(dt)
    abbr = _TZ_DE.get(v.tzname() or "", v.tzname() or "")
    stamp = v.strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")
    return f"{stamp} (Europe/Vienna, {abbr})" if long else f"{stamp} {abbr}"


def _time_tag(dt: datetime | None, *, long: bool = False, seconds: bool = False) -> str:
    text = html.escape(_fmt_dt(dt, long=long, seconds=seconds))
    if dt is None or dt.tzinfo is None:
        return text
    return f'<time datetime="{html.escape(dt.isoformat())}">{text}</time>'


def _fmt_day(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _badge(kind: str, text: str) -> str:
    icon = {"ok": "✔", "warn": "▲", "err": "✖", "info": "ℹ", "off": "–"}[kind]
    return f'<span class="badge {kind}"><span class="ic" aria-hidden="true">{icon}</span> {html.escape(text)}</span>'


def _name(mapping: dict, key: object) -> str:
    return mapping.get(key, str(key))


def _error_block(err: ErrorInfo | None) -> str:
    if err is None:
        return ""
    parts = [f'<div class="errmsg"><strong>{_t(err.message, 500)}</strong> <code>{_t(err.code, 60)}</code></div>']
    if err.hint:
        parts.append(f'<div class="hint">Tipp: {_rich(err.hint, 500)}</div>')
    if err.detail:
        parts.append(f"<details><summary>Technische Details</summary><pre>{_t(err.detail, 1500)}</pre></details>")
    return "".join(parts)


def _table(headers: Iterable[str], rows: list[list[str]], *, cls: str = "") -> str:
    head = "".join(f'<th scope="col">{html.escape(h)}</th>' for h in headers)
    shown = rows[:_MAX_ROWS]
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in shown)
    more = ""
    if len(rows) > len(shown):
        n = len(rows) - len(shown)
        more = f'<p class="muted">… und {n} weitere Zeile{"n" if n != 1 else ""} (hier nicht aufgeführt).</p>'
    return (
        f'<div class="scroll"><table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'
    )


def _all_errors(report: m.RunReport) -> list[ErrorInfo]:
    errs: list[ErrorInfo] = []
    for res in report.sources.values():
        errs.extend(res.errors)
        errs.extend(oc.error for oc in res.categories.values() if oc.error)
    errs.extend(t.error for t in (report.calendar_target, report.tasks_target) if t.error)
    errs.extend(c.error for c in report.changes if c.error)
    return errs


def _next_steps(report: m.RunReport) -> list[str]:
    codes = {e.code for e in _all_errors(report)} | {n.code for n in report.notices}
    steps: list[str] = list(report.next_steps)
    for known, text in _CODE_STEPS:
        if codes & known:
            steps.append(text)
    if any(r.stale for r in report.sources.values()):
        steps.append("Die Daten sind veraltet. Sobald das Problem behoben ist, 'schulsync run' ausführen.")
    if report.mode == m.RunMode.PREVIEW:
        steps.append("Die Vorschau hat nichts geschrieben. Zum tatsächlichen Übernehmen 'schulsync run' ausführen.")
    # A problem whose error code has no specific hint (or a failed run without any error record)
    # must never end in "no action needed".
    needs_attention = report.status in (m.OverallStatus.PARTIAL, m.OverallStatus.FAILED)
    if needs_attention and report.mode != m.RunMode.PREVIEW and not codes & _CODES_WITH_STEP:
        steps.append(_GENERIC_STEP)
    seen: set[str] = set()
    out: list[str] = []
    for s in steps:
        key = " ".join(s.split())
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    if not out:
        out.append("Keine Maßnahmen nötig.")
    return out


# --------------------------------------------------------------------------- sections


def _section_header(report: m.RunReport, generated_at: datetime) -> str:
    preview = report.mode == m.RunMode.PREVIEW
    kind, word, sentence = _OVERALL.get(report.status, ("info", str(report.status), ""))
    out = ['<header id="status">', "<h1>SchulSync-Bericht</h1>"]
    out.append(f'<p class="when">Erstellt: {_time_tag(generated_at, long=True)}</p>')
    if preview:
        out.append(
            '<div class="banner info" role="status"><strong>Vorschau - es wurde nichts geschrieben</strong>'
            "<br>Es wurde weder bei Google etwas verändert noch ein Tageserfolg verbucht.</div>"
        )
        out.append(f'<p class="overall">{_badge("info", "Vorschau")} <span class="muted">Ergebnis der Prüfung: {html.escape(word)}</span></p>')
    else:
        out.append(f'<p class="overall">{_badge(kind, word)} <span>{html.escape(sentence)}</span></p>')
    out.append('<dl class="meta">')
    out.append(f"<dt>Tag</dt><dd>{html.escape(_fmt_day(report.day))}</dd>")
    out.append(f"<dt>Modus</dt><dd>{html.escape(_name(_MODE_NAME, report.mode))}</dd>")
    out.append(f"<dt>Gestartet</dt><dd>{_time_tag(report.started_at, seconds=True)}</dd>")
    out.append(f"<dt>Beendet</dt><dd>{_time_tag(report.finished_at, seconds=True)}</dd>")
    out.append("</dl>")
    stale = [s for s, r in report.sources.items() if r.stale]
    if stale:
        names = ", ".join(_name(_SOURCE_NAME, s) for s in stale)
        out.append(
            f'<div class="banner warn" role="alert"><strong>Daten veraltet:</strong> {html.escape(names)} '
            "konnte heute nicht erfolgreich aktualisiert werden. Die Einträge in Kalender und Aufgabenliste können veraltet sein.</div>"
        )
    out.append("</header>")
    return "".join(out)


def _counts_row(label: str, c: m.Counts) -> list[str]:
    skipped = str(c.skipped)
    if c.skipped_reasons:
        items = "".join(
            f"<li>{html.escape(_SKIP_REASONS.get(r, _SKIP_REASON_FALLBACK))}: {n}</li>" for r, n in sorted(c.skipped_reasons.items())
        )
        skipped += f'<ul class="reasons">{items}</ul>'
    return [
        html.escape(label),
        str(c.new),
        str(c.updated),
        str(c.unchanged),
        str(c.cancelled),
        str(c.reactivated),
        skipped,
        f'<strong class="bad">{c.failed}</strong>' if c.failed else "0",
    ]


def _source_block(src: m.Source, res: m.SourceRunResult, preview: bool = False) -> str:
    kind, word = _SOURCE_STATUS.get(res.status, ("info", str(res.status)))
    if preview and res.status in _PREVIEW_SOURCE_STATUS:  # a preview books no success
        kind, word = "info", _PREVIEW_SOURCE_STATUS[res.status]
    sid = f"quelle-{html.escape(str(src.value))}"
    out = [f'<article class="card" id="{sid}">']
    out.append(f"<h3>{html.escape(_name(_SOURCE_NAME, src))} {_badge(kind, word)}</h3>")
    if res.last_success_at is not None:
        out.append(f'<p>Letzter erfolgreicher Abruf: {_time_tag(res.last_success_at, long=True)}</p>')
    else:
        out.append("<p>Letzter erfolgreicher Abruf: bisher kein erfolgreicher Abruf bekannt.</p>")
    if res.stale:
        out.append(
            '<div class="banner warn" role="alert"><strong>Daten veraltet</strong> – heute konnte nichts aktualisiert werden. '
            "Bitte nicht auf die Einträge dieser Quelle verlassen, bis ein Abruf wieder klappt.</div>"
        )
    for e in res.errors:
        out.append(f'<div class="problem">{_error_block(e)}</div>')
    if res.categories:
        rows = []
        for cat, oc in res.categories.items():
            ck, cw = _CATEGORY_STATUS.get(oc.status, ("info", str(oc.status)))
            why = _error_block(oc.error)
            if oc.detail:
                why += f'<div class="muted">{_t(oc.detail, 300)}</div>'
            rows.append([html.escape(_name(_CATEGORY_NAME, cat)), _badge(ck, cw), str(oc.item_count), why or "–"])
        out.append(_table(["Kategorie", "Ergebnis", "Einträge", "Grund / Hinweis"], rows))
    if preview:
        out.append('<p class="muted">Vorschau: Die Zahlen zeigen, was geändert würde; nichts wurde geschrieben.</p>')
    heads = ["Ziel", "Neu", "Aktualisiert", "Unverändert", "Abgesagt", "Reaktiviert", "Übersprungen", "Fehlgeschlagen"]
    out.append(
        _table(heads, [_counts_row("Kalender", res.calendar), _counts_row("Aufgabenliste", res.tasks)], cls="counts")
    )
    out.append("</article>")
    return "".join(out)


def _section_sources(report: m.RunReport) -> str:
    out = ['<section id="quellen"><h2>Ergebnis je Quelle</h2>']
    order = [s for s in m.Source if s in report.sources]
    if not order:
        out.append('<p class="muted">Keine Quelle war aktiviert oder gelaufen.</p>')
    out.extend(_source_block(s, report.sources[s], report.mode == m.RunMode.PREVIEW) for s in order)
    out.append("</section>")
    return "".join(out)


def _target_block(label: str, t: m.TargetStatus) -> str:
    if t.ok is None:
        badge = _badge("err", "Fehler") if t.error else _badge("off", "Nicht geprüft")
    elif t.ok:
        badge = _badge("ok", "Erreichbar")
    else:
        badge = _badge("err", "Nicht erreichbar")
    out = [f'<article class="card"><h3>{html.escape(label)} {badge}</h3><p>Name: {_t(t.name, 120)}</p>']
    if t.ok is False or t.error:
        out.append(_error_block(t.error) or '<div class="errmsg"><strong>Ziel nicht erreichbar.</strong></div>')
    if t.ok is False:
        out.append('<p class="muted">Betroffene Schreibvorgänge wurden zurückgestellt; es wird nie auf ein anderes Ziel ausgewichen.</p>')
    out.append("</article>")
    return "".join(out)


def _section_targets(report: m.RunReport) -> str:
    return (
        '<section id="ziele"><h2>Google-Ziele</h2>'
        + _target_block("Google Kalender", report.calendar_target)
        + _target_block("Google Tasks", report.tasks_target)
        + "</section>"
    )


def _change_cells(c: m.Change) -> list[str]:
    return [
        html.escape(_name(_SOURCE_NAME, c.source)),
        _t(c.title, 200),
        html.escape(_name(_KIND_NAME, c.kind)),
        html.escape(_name(_TARGET_NAME, c.target)),
        html.escape(_name(_ACTION_NAME, c.action)),
    ]


def _section_problems(report: m.RunReport) -> str:
    bad = [c for c in report.changes if c.status in (m.ChangeStatus.FAILED, m.ChangeStatus.DEFERRED, m.ChangeStatus.UNCERTAIN)]
    out = ['<section id="probleme"><h2>Fehlgeschlagene und zurückgestellte Schritte</h2>']
    if not bad:
        out.append('<p class="muted">Keine fehlgeschlagenen, zurückgestellten oder ungeklärten Schritte.</p>')
    else:
        rows = []
        for c in bad:
            ck, cw = _CHANGE_STATUS[c.status]
            why = _error_block(c.error)
            if c.message:
                why += f'<div>{_rich(c.message, 500)}</div>'
            if c.status == m.ChangeStatus.UNCERTAIN:
                why += '<div class="hint">Ausgang ungeklärt: Beim nächsten Lauf wird anhand der Projektkennung abgeglichen; es wird nichts doppelt angelegt.</div>'
            elif c.status == m.ChangeStatus.DEFERRED:
                why += '<div class="hint">Zurückgestellt: wird beim nächsten Lauf erneut versucht.</div>'
            rows.append([_badge(ck, cw), *_change_cells(c), why or "–"])
        out.append(_table(["Status", "Quelle", "Titel", "Art", "Ziel", "Vorgang", "Fehler / Hinweis"], rows))
    out.append("</section>")
    return "".join(out)


def _section_done(report: m.RunReport) -> str:
    preview = report.mode == m.RunMode.PREVIEW
    title = "Geplante Änderungen (nichts geschrieben)" if preview else "Bereits erfolgreich übernommen"
    wanted = {m.ChangeStatus.PLANNED} if preview else {m.ChangeStatus.APPLIED}
    done = [c for c in report.changes if c.status in wanted and c.action != m.Action.SKIP]  # SKIP: nothing written
    earlier = [] if preview else [n for n in report.notices if n.code == NOTICE_TAKEN_OVER]
    out = [f'<section id="uebernommen"><h2>{html.escape(title)}</h2>']
    if not done and not earlier:
        out.append('<p class="muted">Keine.</p>')
    if done:
        rows = []
        for c in done:
            ck, cw = _CHANGE_STATUS[c.status]
            rows.append([_badge(ck, cw), *_change_cells(c)])
        out.append(f"<p>{len(done)} Vorgang/Vorgänge.</p>")
        out.append(_table(["Status", "Quelle", "Titel", "Art", "Ziel", "Vorgang"], rows))
    elif earlier:
        out.append('<p class="muted">In diesem Start wurde nichts neu übernommen.</p>')
    if earlier:
        out.append(
            "<h3>Schon früher übernommen</h3>"
            "<p>Diese Quelle wurde heute bereits vollständig abgeglichen. Das ist in Google vorhanden und wurde nicht erneut abgerufen:</p>"
        )
        out.append(
            _table(
                ["Quelle", "Titel", "In Google"],
                [[html.escape(_name(_SOURCE_NAME, n.source)), _t(n.title, 200), _rich(n.message, 200)] for n in earlier],
            )
        )
    out.append("</section>")
    return "".join(out)


def _section_pending(report: m.RunReport) -> str:
    out = ['<section id="ausstehend"><h2>Ausstehende Google-Schreibvorgänge</h2>']
    n = report.pending_writes
    if n > 0:
        out.append(
            f'<div class="banner warn"><strong>{n}</strong> Schreibvorgang/Schreibvorgänge stehen noch aus '
            "und werden beim nächsten Lauf nachgeholt.</div>"
        )
    else:
        out.append("<p>Keine ausstehenden Schreibvorgänge.</p>")
    out.append("</section>")
    return "".join(out)


def _section_notices(report: m.RunReport) -> str:
    out = ['<section id="hinweise"><h2>Hinweise und Warnungen</h2>']
    notices = [n for n in report.notices if n.code != NOTICE_TAKEN_OVER]  # those are listed under "übernommen"
    if not notices:
        out.append('<p class="muted">Keine Hinweise.</p>')
    for sev, kind, label in _SEVERITY_ORDER:
        group = [n for n in notices if n.severity == sev]
        if not group:
            continue
        out.append(f"<h3>{_badge(kind, label)} <span class=\"muted\">({len(group)})</span></h3><ul class=\"notices\">")
        for n in group:
            meta = []
            if n.source is not None:
                meta.append(html.escape(_name(_SOURCE_NAME, n.source)))
            meta.append(f"<code>{_t(n.code, 60)}</code>")
            head = f"<strong>{_t(n.title, 200)}</strong> – " if n.title else ""
            hint = _NOTICE_HINTS.get(n.code)
            hint_html = f'<div class="hint">{html.escape(hint)}</div>' if hint else ""
            out.append(f'<li>{head}{_rich(n.message, 600)} <span class="muted">({", ".join(meta)})</span>{hint_html}</li>')
        out.append("</ul>")
    out.append("</section>")
    return "".join(out)


def _section_next(report: m.RunReport) -> str:
    items = "".join(f"<li>{_rich(s, 600)}</li>" for s in _next_steps(report))
    return f'<section id="naechste-schritte"><h2>Nächste Schritte</h2><ol>{items}</ol></section>'


_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1c1f24;--muted:#5a6270;--card:#fff;--line:#d5d9e0;--ok:#146c2e;--ok-bg:#e3f4e8;--warn:#8a5a00;--warn-bg:#fff1d1;--err:#a4161a;--err-bg:#fde4e4;--info:#0b5394;--info-bg:#e1eefb;--off:#555;--off-bg:#ececec;--code:#eef0f4}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e6e8ec;--muted:#a3abb8;--card:#1d2128;--line:#363c47;--ok:#7bd88f;--ok-bg:#16301f;--warn:#f2c46d;--warn-bg:#392b0c;--err:#ff9a9d;--err-bg:#3d1618;--info:#8ec5ff;--info-bg:#102b46;--off:#b5bac4;--off-bg:#2a2f38;--code:#262b34}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,"Segoe UI",Roboto,Arial,sans-serif;overflow-wrap:anywhere}
main{max-width:64rem;margin:0 auto;padding:1rem}
h1{font-size:1.6rem;margin:.2rem 0}h2{font-size:1.25rem;margin:2rem 0 .6rem;border-bottom:2px solid var(--line);padding-bottom:.2rem}h3{font-size:1.05rem;margin:.2rem 0 .5rem}
header,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem;margin:.8rem 0}
.when{margin:.2rem 0;color:var(--muted)}.overall{font-size:1.15rem;margin:.6rem 0}
.meta{display:grid;grid-template-columns:max-content 1fr;gap:.1rem 1rem;margin:.6rem 0}.meta dt{color:var(--muted)}.meta dd{margin:0}
.badge{display:inline-block;padding:.05rem .6rem;border-radius:999px;font-weight:600;font-size:.9rem;border:1px solid currentColor;white-space:nowrap}
.badge.ok,.banner.ok{color:var(--ok);background:var(--ok-bg)}.badge.warn,.banner.warn{color:var(--warn);background:var(--warn-bg)}
.badge.err,.banner.err{color:var(--err);background:var(--err-bg)}.badge.info,.banner.info{color:var(--info);background:var(--info-bg)}.badge.off{color:var(--off);background:var(--off-bg)}
.banner{border:1px solid currentColor;border-left-width:6px;border-radius:6px;padding:.6rem .8rem;margin:.6rem 0}
.banner strong{font-size:1.05rem}
.scroll{overflow-x:auto;margin:.5rem 0}table{border-collapse:collapse;width:100%;font-size:.95rem}
th,td{border:1px solid var(--line);padding:.35rem .5rem;text-align:left;vertical-align:top}th{background:var(--code)}
table.counts td:not(:first-child),table.counts th:not(:first-child){text-align:right}
.bad{color:var(--err)}.muted{color:var(--muted)}.hint{color:var(--muted);margin:.2rem 0}.errmsg{margin:.2rem 0}
.problem{border-left:4px solid var(--err);padding-left:.6rem;margin:.5rem 0}
code,pre{background:var(--code);border-radius:4px;padding:0 .3rem;font:.85rem ui-monospace,Consolas,monospace}pre{padding:.5rem;white-space:pre-wrap;overflow-x:auto}
ul.reasons{margin:.2rem 0;padding-left:1.1rem;font-size:.85rem;font-weight:400;text-align:left}ul.notices{padding-left:1.2rem}ul.notices li{margin:.5rem 0}
a{color:var(--info)}footer{margin:2rem 0 1rem;color:var(--muted);font-size:.85rem}
@media (max-width:40rem){main{padding:.5rem}.meta{grid-template-columns:1fr}}
@media print{body{background:#fff;color:#000}header,.card{border-color:#999;break-inside:avoid}section{break-inside:auto}a{color:#000}.scroll{overflow:visible}}
"""

_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"


def _fingerprint(report: m.RunReport) -> str:
    try:
        return report.fingerprint()
    except UnicodeEncodeError:  # lone surrogate in an identifier (uid/name): never cost the report
        keys = "\n".join(report.problem_keys()).encode("utf-8", errors="replace")
        return hashlib.sha256(keys).hexdigest()[:16]


def render_html(report: m.RunReport, *, generated_at: datetime) -> str:
    """Render `report` as one self-contained German HTML5 page (deterministic for equal input)."""
    title = f"SchulSync-Bericht {_fmt_day(report.day)}"
    body = [
        _section_header(report, generated_at),
        _section_sources(report),
        _section_targets(report),
        _section_problems(report),
        _section_done(report),
        _section_pending(report),
        _section_notices(report),
        _section_next(report),
        f"<footer>Dieser Bericht wurde lokal erzeugt; es werden keine Daten übertragen. Fehlerkennung: {html.escape(_fingerprint(report))}</footer>",
    ]
    return (
        "<!DOCTYPE html>\n"
        '<html lang="de"><head><meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="referrer" content="no-referrer">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{html.escape(title)}</title>\n<style>{_CSS}</style></head>\n"
        f"<body><main>\n{chr(10).join(body)}\n</main></body></html>\n"
    )


# --------------------------------------------------------------------------- files


def _report_name(day: date) -> str:
    return f"bericht-{day.isoformat()}.html"


def report_file_for(paths: Paths, report: m.RunReport) -> Path:
    """The file `write_report` writes for `report` (also for the log line when writing it fails)."""
    if report.mode == m.RunMode.PREVIEW:
        return paths.reports / _PREVIEW_NAME
    return paths.reports / _report_name(report.day)


def _atomic_write(target: Path, data: bytes) -> None:
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")  # never matches bericht-*.html
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_latest(paths: Paths, data: bytes, target: Path) -> None:
    try:
        _atomic_write(paths.reports / "latest.html", data)
    except OSError as exc:
        logger.warning("latest.html konnte nicht aktualisiert werden (%s); Bericht liegt unter %s", type(exc).__name__, target)


def write_report(paths: Paths, report: m.RunReport) -> Path:
    """Write `reports/bericht-<Vienna day>.html` atomically (same-day runs overwrite) and refresh
    `reports/latest.html` as a plain copy. Returns the dated file. A failing `latest.html` copy is
    logged but never fails the call (a browser may hold the file open on Windows). If the dated file
    itself cannot be written, `latest.html` is still refreshed before the OSError propagates, so the
    report is not lost.

    A PREVIEW report is written to `vorschau.html` only (dated file and `latest.html` stay as they
    are): a preview must not replace the record of a real, possibly failed, run."""
    paths.reports.mkdir(parents=True, exist_ok=True)
    # errors="replace": a lone surrogate must never cost the failure report (foreign text is stripped
    # of them already; this is the second layer).
    data = render_html(report, generated_at=report.finished_at).encode("utf-8", errors="replace")
    target = report_file_for(paths, report)
    if report.mode == m.RunMode.PREVIEW:
        _atomic_write(target, data)
        return target
    try:
        _atomic_write(target, data)
    except OSError:
        _write_latest(paths, data, target)
        raise
    _write_latest(paths, data, target)
    return target


def prune_reports(paths: Paths, keep_days: int, now: datetime) -> list[Path]:
    """Delete `bericht-*.html` files older than `keep_days` (by the date in the file name; mtime for
    names without a date) and orphaned `*.tmp` leftovers of a killed write (older than
    max(keep_days, 1) days by mtime). Nothing else (e.g. latest.html) is touched. Returns the
    deleted paths."""
    if keep_days < 0:
        raise ValueError("keep_days darf nicht negativ sein")
    if not paths.reports.is_dir():
        return []
    today = today_vienna(now)
    deleted: list[Path] = []

    def remove(f: Path) -> None:
        try:
            f.unlink()
            deleted.append(f)
        except OSError as exc:
            logger.warning("Alter Bericht konnte nicht gelöscht werden: %s (%s)", f, type(exc).__name__)

    for f in sorted(paths.reports.glob("bericht-*.html")):
        if not f.is_file() or f.is_symlink():
            continue
        mo = _REPORT_RE.match(f.name)
        try:
            try:
                if not mo:
                    raise ValueError("no date in file name")
                file_day = date(int(mo.group(1)), int(mo.group(2)), int(mo.group(3)))
            except ValueError:  # no or impossible date in the name (e.g. 2026-13-45): use mtime
                file_day = datetime.fromtimestamp(f.stat().st_mtime, VIENNA).date()
        except (ValueError, OSError):
            continue
        if (today - file_day).days > keep_days:
            remove(f)
    tmp_cutoff = now - timedelta(days=max(keep_days, 1))
    for f in sorted(paths.reports.glob("*.tmp")):
        if not _TMP_RE.match(f.name) or not f.is_file() or f.is_symlink():
            continue
        try:
            old = datetime.fromtimestamp(f.stat().st_mtime, VIENNA) < tmp_cutoff
        except OSError:
            continue
        if old:
            remove(f)
    return deleted


def open_report(path: Path, *, opener: Callable[[str], object] | None = None) -> bool:
    """Open `path` in the default browser of the current user session. Never raises.

    Default: `webbrowser.open(file-URI)`, falling back to `os.startfile` (Windows). An injected
    `opener(uri)` is used alone (return value False or an exception = failure). On failure the
    report stays on disk and its location is logged."""
    try:
        uri = path.as_uri() if path.is_absolute() else path.resolve().as_uri()
        if opener is not None:
            ok = opener(uri) is not False
        else:
            ok = bool(webbrowser.open(uri))
            if not ok and hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only
                ok = True
    except Exception as exc:  # noqa: BLE001 - opening a browser must never break a run
        logger.warning("Bericht konnte nicht geöffnet werden (%s). Gespeichert unter: %s", type(exc).__name__, path)
        return False
    if not ok:
        logger.warning("Bericht konnte nicht im Browser geöffnet werden. Gespeichert unter: %s", path)
    return ok


def latest_report(paths: Paths) -> Path | None:
    """Newest report: `reports/latest.html`, unless a newer dated `bericht-YYYY-MM-DD.html` exists
    (a failed `latest.html` copy leaves a stale one behind); else the newest dated file, else None."""
    latest = paths.reports / "latest.html"
    dated = (
        sorted(f for f in paths.reports.glob("bericht-*.html") if f.is_file() and _REPORT_RE.match(f.name))
        if paths.reports.is_dir()
        else []
    )
    if not latest.is_file():
        return dated[-1] if dated else None
    if dated:
        try:
            # name order = date order; mtime also catches a same-day rerun whose latest copy failed
            if _title_day(latest) < _REPORT_RE.match(dated[-1].name).groups() or dated[-1].stat().st_mtime > latest.stat().st_mtime:
                return dated[-1]
        except OSError:
            pass
    return latest


def _title_day(f: Path) -> tuple[str, str, str]:
    """(YYYY, MM, DD) from the `<title>` of a report file, ("", "", "") if unreadable."""
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            mo = _TITLE_DAY_RE.search(fh.read(4096))
    except OSError:
        return ("", "", "")
    return (mo.group(3), mo.group(2), mo.group(1)) if mo else ("", "", "")
