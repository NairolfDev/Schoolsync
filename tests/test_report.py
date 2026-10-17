"""Tests for schulsync.report (HTML run report).

Honesty note: all `RunReport` fixtures are synthetic and anonymised. Nothing here proves that the
real engine fills the report the way these fixtures do; it proves that whatever it fills in is
rendered completely, escaped, free of secrets, and written safely.
"""

from __future__ import annotations

import html
import logging
import os
import re
import sys
import time
import types
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from schulsync import report as rp
from schulsync.config import Paths
from schulsync.errors import ErrorInfo
from schulsync.models import (
    Action,
    Category,
    CategoryOutcome,
    CategoryStatus,
    Change,
    ChangeStatus,
    Counts,
    Kind,
    Notice,
    OverallStatus,
    RunMode,
    RunReport,
    Severity,
    Source,
    SourceRunResult,
    SourceStatus,
    Target,
    TargetStatus,
)
from schulsync.timeutil import UTC, VIENNA

SCRIPT = "<script>alert(1)</script>"
IMG = '"><img src=x onerror=alert(1)>'
JS_LINK = "javascript:alert(document.cookie)"

# Obvious dummy secrets (never real values).
PW = "hunter2-dummy"
TOK = "tok-DUMMY-123456"
COOKIE = "cookieDUMMY999"
SESS = "sessDUMMY42"
AUTH = "authDUMMY7"
ALL_SECRETS = (PW, TOK, COOKIE, SESS, AUTH)


# --------------------------------------------------------------------------- fixtures


def make_report(**kw) -> RunReport:
    day = kw.pop("day", date(2026, 10, 14))
    base = dict(
        started_at=datetime(day.year, day.month, day.day, 8, 0, tzinfo=VIENNA),
        finished_at=datetime(day.year, day.month, day.day, 8, 5, tzinfo=VIENNA),
        mode=RunMode.AUTO,
        day=day,
        status=OverallStatus.SUCCESS,
    )
    base.update(kw)
    return RunReport(**base)


def render(r: RunReport, generated_at: datetime | None = None) -> str:
    return rp.render_html(r, generated_at=generated_at or r.finished_at)


def change(title="Mathe Test", **kw) -> Change:
    base = dict(
        uid="webuntis:exam:1",
        source=Source.WEBUNTIS,
        kind=Kind.EXAM,
        title=title,
        target=Target.CALENDAR,
        action=Action.CREATE,
        status=ChangeStatus.APPLIED,
    )
    base.update(kw)
    return Change(**base)


def rich_report() -> RunReport:
    net = ErrorInfo("network", "Keine Verbindung zu WebUntis.", "Internet prüfen.", "ConnectError: timed out")
    tgt = ErrorInfo("target_unavailable", "Aufgabenliste 'Schule' nicht gefunden.", "Liste in Google anlegen.")
    edu = SourceRunResult(
        Source.EDUVIDUAL,
        SourceStatus.SUCCESS,
        categories={
            Category.ASSIGNMENTS: CategoryOutcome(Category.ASSIGNMENTS, CategoryStatus.OK, detail="2 Seiten gelesen", item_count=4)
        },
        calendar=Counts(new=2, updated=1, unchanged=5, cancelled=0, skipped=3, skipped_reasons={"expired": 2, "outside_window": 1}),
        tasks=Counts(new=2, unchanged=5, failed=1),
        last_success_at=datetime(2026, 10, 14, 8, 3, tzinfo=VIENNA),
    )
    wun = SourceRunResult(
        Source.WEBUNTIS,
        SourceStatus.PARTIAL,
        categories={
            Category.HOMEWORK: CategoryOutcome(Category.HOMEWORK, CategoryStatus.OK, item_count=3),
            Category.EXAMS: CategoryOutcome(Category.EXAMS, CategoryStatus.FAILED, error=net),
            Category.CANCELLATIONS: CategoryOutcome(Category.CANCELLATIONS, CategoryStatus.INCOMPLETE, detail="Seite 2 fehlt"),
        },
        calendar=Counts(cancelled=1, reactivated=1, failed=2),
        errors=[net],
        last_success_at=datetime(2026, 10, 12, 7, 55, tzinfo=VIENNA),
        stale=True,
    )
    return make_report(
        status=OverallStatus.PARTIAL,
        sources={Source.EDUVIDUAL: edu, Source.WEBUNTIS: wun},
        calendar_target=TargetStatus("Schule", True),
        tasks_target=TargetStatus("Schule", False, tgt),
        changes=[
            change("Protokoll Elektrotechnik", uid="eduvidual:assignment:101", source=Source.EDUVIDUAL, kind=Kind.ASSIGNMENT),
            change("Übung Netzwerktechnik", uid="eduvidual:assignment:102", source=Source.EDUVIDUAL, kind=Kind.ASSIGNMENT,
                   target=Target.TASKS, status=ChangeStatus.DEFERRED, message="Ziel nicht erreichbar", error=tgt),
            change("Schularbeit Mathematik", uid="webuntis:exam:77", target=Target.TASKS,
                   status=ChangeStatus.UNCERTAIN, message="Antwort verloren", error=ErrorInfo("outcome_unknown", "Ausgang unklar.")),
            change("Hausübung Deutsch", uid="webuntis:homework:5", kind=Kind.HOMEWORK, action=Action.UPDATE,
                   status=ChangeStatus.FAILED, error=net),
        ],
        notices=[
            Notice(Severity.ERROR, "schema", "Unerwartete Antwort von WebUntis.", Source.WEBUNTIS),
            Notice(Severity.WARNING, "no_date", "Eintrag ohne Datum.", Source.EDUVIDUAL, title="Projektmappe"),
            Notice(Severity.WARNING, "incomplete_data", "Raum fehlt.", Source.WEBUNTIS, title="Physik Test"),
            Notice(Severity.INFO, "user_deleted_event", "Termin wurde in Google gelöscht.", Source.EDUVIDUAL, title="Lernplan"),
            Notice(Severity.INFO, "identity_uncertain", "Zuordnung nur über Inhalt.", Source.WEBUNTIS, title="Turnen"),
        ],
        pending_writes=2,
        next_steps=["Eigener Schritt aus dem Lauf."],
    )


def hostile_report() -> RunReport:
    err = ErrorInfo("schema", SCRIPT, IMG, SCRIPT + IMG)
    src = SourceRunResult(
        Source.EDUVIDUAL,
        SourceStatus.FAILED,
        categories={Category.ASSIGNMENTS: CategoryOutcome(Category.ASSIGNMENTS, CategoryStatus.FAILED, error=err, detail=SCRIPT)},
        errors=[err],
    )
    return make_report(
        status=OverallStatus.FAILED,
        sources={Source.EDUVIDUAL: src},
        calendar_target=TargetStatus(SCRIPT, False, err),
        changes=[
            Change("u1", Source.EDUVIDUAL, Kind.ASSIGNMENT, SCRIPT, Target.CALENDAR, Action.CREATE, ChangeStatus.FAILED, IMG, err),
            Change("u2", Source.WEBUNTIS, Kind.EXAM, IMG, Target.TASKS, Action.CREATE, ChangeStatus.APPLIED),
        ],
        notices=[Notice(Severity.WARNING, SCRIPT, f'{IMG} {JS_LINK} <a href="{JS_LINK}">x</a>', Source.EDUVIDUAL, title=SCRIPT)],
        next_steps=[SCRIPT, f"Siehe {JS_LINK}"],
    )


class Page(HTMLParser):
    """Collects tags/attributes so tests check the parsed DOM, not just substrings."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str, str | None]] = []
        self.text: list[str] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, k, v) for k, v in attrs)

    def handle_data(self, data):
        self.text.append(data)

    @property
    def visible(self) -> str:
        return "".join(self.text)


def assert_no_active_content(text: str) -> Page:
    page = Page(text)
    assert "script" not in page.tags and "iframe" not in page.tags and "img" not in page.tags
    assert "object" not in page.tags and "embed" not in page.tags and "form" not in page.tags
    assert "link" not in page.tags
    for tag, key, val in page.attrs:
        assert not key.startswith("on"), (tag, key, val)
        if key == "href":
            assert val and re.match(r"https?://", val), (tag, key, val)
        assert key != "src", (tag, key, val)
        assert key != "style", (tag, key, val)
    return page


# --------------------------------------------------------------------------- structure


def test_rich_report_contains_every_section_and_its_content():
    out = render(rich_report())
    for section_id in ("status", "quellen", "ziele", "probleme", "uebernommen", "ausstehend", "hinweise", "naechste-schritte"):
        assert f'id="{section_id}"' in out, section_id
    for heading in (
        "Ergebnis je Quelle",
        "Google-Ziele",
        "Fehlgeschlagene und zurückgestellte Schritte",
        "Bereits erfolgreich übernommen",
        "Ausstehende Google-Schreibvorgänge",
        "Hinweise und Warnungen",
        "Nächste Schritte",
    ):
        assert heading in out, heading
    assert 'lang="de"' in out and out.startswith("<!DOCTYPE html>")
    # one block per source, both Google targets in the counts of each
    assert 'id="quelle-eduvidual"' in out and 'id="quelle-webuntis"' in out
    for word in ("Neu", "Aktualisiert", "Unverändert", "Abgesagt", "Übersprungen", "Fehlgeschlagen"):
        assert f">{word}</th>" in out
    assert out.count('<table class="counts">') == 2
    assert "<tr><td>Kalender</td><td>2</td><td>1</td><td>5</td><td>0</td><td>0</td>" in out  # Eduvidual calendar
    assert "<tr><td>Aufgabenliste</td><td>2</td><td>0</td><td>5</td>" in out  # Eduvidual tasks
    assert "Frist/Ereignis bereits vorbei: 2" in out and "außerhalb des Prüffensters: 1" in out
    # categories with status and reasons
    assert "Aufgaben und Abgaben" in out and "2 Seiten gelesen" in out
    assert "Prüfungen und Schularbeiten" in out and "Keine Verbindung zu WebUntis." in out
    assert "Unvollständig" in out and "Seite 2 fehlt" in out
    # Google targets
    assert "Erreichbar" in out and "Nicht erreichbar" in out
    assert "Aufgabenliste &#x27;Schule&#x27; nicht gefunden." in out
    # failed / deferred / uncertain with affected entries
    for title in ("Übung Netzwerktechnik", "Schularbeit Mathematik", "Hausübung Deutsch"):
        assert title in out
    for word in ("Zurückgestellt", "Ausgang ungeklärt", "Fehlgeschlagen"):
        assert word in out
    assert "Ausgang unklar." in out and "Tipp: Internet prüfen." in out
    # applied
    assert "Protokoll Elektrotechnik" in out and "Erledigt" in out
    # pending
    assert "<strong>2</strong> Schreibvorgang" in out
    # notices grouped by severity (errors first), with hints
    assert out.index("Fehler</span>") < out.index("Warnungen</span>") < out.index("Hinweise</span>")
    assert "schulsync restore" in out and "kein Datum" in out and "keine feste Kennung" in out
    # next steps
    assert "Eigener Schritt aus dem Lauf." in out


def test_header_has_time_zone_status_word_and_icon_not_colour_alone():
    out = render(make_report(status=OverallStatus.FAILED), generated_at=datetime(2026, 10, 14, 8, 5, tzinfo=VIENNA))
    assert "Erstellt: <time" in out and "14.10.2026 08:05 (Europe/Vienna, MESZ)" in out
    assert " Fehlgeschlagen</span>" in out
    assert 'class="ic" aria-hidden="true">✖' in out  # symbol next to colour
    ok = render(make_report(status=OverallStatus.SUCCESS))
    assert "Erfolgreich" in ok and "✔" in ok


@pytest.mark.parametrize(
    "when, expected",
    [
        (datetime(2026, 7, 1, 6, 5, tzinfo=UTC), "01.07.2026 08:05 (Europe/Vienna, MESZ)"),
        (datetime(2026, 1, 15, 7, 5, tzinfo=UTC), "15.01.2026 08:05 (Europe/Vienna, MEZ)"),
        # DST change days: 29.03.2026 (01:00 UTC -> CEST) and 25.10.2026 (01:00 UTC -> CET)
        (datetime(2026, 3, 29, 0, 30, tzinfo=UTC), "29.03.2026 01:30 (Europe/Vienna, MEZ)"),
        (datetime(2026, 3, 29, 1, 30, tzinfo=UTC), "29.03.2026 03:30 (Europe/Vienna, MESZ)"),
        (datetime(2026, 10, 25, 0, 30, tzinfo=UTC), "25.10.2026 02:30 (Europe/Vienna, MESZ)"),
        (datetime(2026, 10, 25, 1, 30, tzinfo=UTC), "25.10.2026 02:30 (Europe/Vienna, MEZ)"),
    ],
)
def test_time_zone_text_in_summer_winter_and_on_dst_days(when, expected):
    assert expected in render(make_report(), generated_at=when)


def test_naive_datetime_does_not_crash_the_report():
    out = render(make_report(), generated_at=datetime(2026, 10, 14, 8, 5))
    assert "Zeitzone unbekannt" in out


def test_stale_data_warning_and_last_success():
    out = render(rich_report())
    assert "Daten veraltet" in out
    assert 'role="alert"' in out
    assert "12.10.2026 07:55 (Europe/Vienna, MESZ)" in out  # last successful WebUntis fetch
    assert "14.10.2026 08:03 (Europe/Vienna, MESZ)" in out  # Eduvidual
    assert "Sobald das Problem behoben ist" in out
    fresh = render(make_report(sources={Source.EDUVIDUAL: SourceRunResult(Source.EDUVIDUAL, SourceStatus.SUCCESS)}))
    assert "Daten veraltet" not in fresh
    assert "bisher kein erfolgreicher Abruf bekannt" in fresh


def test_preview_mode_wording():
    r = make_report(mode=RunMode.PREVIEW, status=OverallStatus.SUCCESS,
                    changes=[change("Geplante Arbeit", status=ChangeStatus.PLANNED)])
    out = render(r)
    assert "Vorschau - es wurde nichts geschrieben" in out
    assert "Geplante Änderungen (nichts geschrieben)" in out and "Geplante Arbeit" in out
    assert "Bereits erfolgreich übernommen" not in out
    assert "Alles wurde erfolgreich synchronisiert." not in out
    assert "Vorschau - es wurde nichts geschrieben" not in render(make_report())


def test_pending_writes_none_and_nothing_to_do():
    out = render(make_report(status=OverallStatus.NOTHING_TO_DO))
    assert "Keine ausstehenden Schreibvorgänge." in out and "Nichts zu tun" in out
    assert "Keine Maßnahmen nötig." in out


def test_notice_groups_and_restore_hint():
    r = make_report(notices=[
        Notice(Severity.INFO, "user_deleted_task", "Aufgabe gelöscht.", Source.EDUVIDUAL, title="Lernplan"),
        Notice(Severity.ERROR, "x", "Schlimm."),
        Notice(Severity.WARNING, "no_date", "Ohne Datum."),
    ])
    out = render(r)
    assert out.count("schulsync restore") == 1
    assert out.index("Schlimm.") < out.index("Ohne Datum.") < out.index("Aufgabe gelöscht.")


# --------------------------------------------------------------------------- next steps


@pytest.mark.parametrize(
    "code, needle",
    [
        ("network", "Internetverbindung prüfen"),
        ("transient", "Internetverbindung prüfen"),
        ("auth", "schulsync setup"),
        ("google_auth", "7 Tagen"),
        ("schema", "schulsync check"),
        ("target_unavailable", "Aufgabenliste 'Schule' in Google"),
        ("access_denied", "Berechtigungen"),
        ("incomplete", "unvollständig"),
        ("outcome_unknown", "keine Doppelten"),
        ("config", "config.toml"),
    ],
)
def test_next_steps_from_error_codes(code, needle):
    r = make_report(status=OverallStatus.FAILED, changes=[change(status=ChangeStatus.FAILED, error=ErrorInfo(code, "Fehler."))])
    out = render(r)
    section = html.unescape(re.search(r'<section id="naechste-schritte">(.*?)</section>', out, re.S).group(1))
    assert needle in section
    assert "Keine Maßnahmen nötig" not in section


def test_google_auth_step_mentions_testing_status_seven_day_limit():
    r = make_report(status=OverallStatus.FAILED, calendar_target=TargetStatus("Schule", False, ErrorInfo("google_auth", "Abgelaufen.")))
    section = re.search(r'<section id="naechste-schritte">(.*?)</section>', render(r), re.S).group(1)
    assert "schulsync setup" in section and "Testing" in section and "7 Tagen" in section


def test_next_steps_are_deduplicated_and_ordered():
    e = ErrorInfo("network", "x")
    r = make_report(status=OverallStatus.FAILED, next_steps=["Zuerst das.", "Zuerst das."],
                    changes=[change(status=ChangeStatus.FAILED, error=e), change("B", status=ChangeStatus.FAILED, error=e)])
    section = re.search(r'<section id="naechste-schritte">(.*?)</section>', render(r), re.S).group(1)
    assert section.count("Zuerst das.") == 1 and section.count("Internetverbindung prüfen") == 1
    assert section.index("Zuerst das.") < section.index("Internetverbindung")


@pytest.mark.parametrize("code", ["conflict", "not_found", "error"])
def test_unmapped_error_code_still_gets_a_next_step(code):
    r = make_report(status=OverallStatus.FAILED, changes=[change(status=ChangeStatus.FAILED, error=ErrorInfo(code, "Hm."))])
    section = re.search(r'<section id="naechste-schritte">(.*?)</section>', render(r), re.S).group(1)
    assert "Keine Maßnahmen nötig" not in section and "schulsync check" in section


def test_failed_run_without_error_records_still_gets_a_next_step():
    section = re.search(
        r'<section id="naechste-schritte">(.*?)</section>', render(make_report(status=OverallStatus.PARTIAL, pending_writes=1)), re.S
    ).group(1)
    assert "Keine Maßnahmen nötig" not in section


def test_preview_next_step_points_to_run():
    section = re.search(r'<section id="naechste-schritte">(.*?)</section>', render(make_report(mode=RunMode.PREVIEW)), re.S).group(1)
    assert "schulsync run" in section


# --------------------------------------------------------------------------- security


def test_csp_present_no_scripts_no_external_resources():
    for r in (rich_report(), hostile_report(), make_report()):
        out = render(r)
        assert "default-src 'none'" in out and "style-src 'unsafe-inline'" in out and "img-src data:" in out
        assert "http-equiv=\"Content-Security-Policy\"" in out
        assert "<script" not in out.lower()
        page = assert_no_active_content(out)
        assert "style" in page.tags and "meta" in page.tags
        assert not re.search(r"""(src|srcset|action|poster|data)\s*=\s*["']?\s*(https?:)?//""", out, re.I)
        assert "@import" not in out and "url(" not in out.replace("url(data:", "")
        assert "fonts.googleapis" not in out


def test_hostile_strings_are_escaped_everywhere():
    out = render(hostile_report())
    assert "<script" not in out.lower()
    assert "<img" not in out.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in out
    assert_no_active_content(out)


def test_javascript_links_are_never_linked():
    out = render(hostile_report())
    assert "href=\"javascript" not in out.lower()
    assert_no_active_content(out)
    r = make_report(changes=[change(status=ChangeStatus.FAILED, message="Siehe JAVASCRIPT:alert(1) und data:text/html,<b>x</b>")])
    out = render(r)
    assert "javascript:" not in " ".join(v or "" for _, k, v in Page(out).attrs if k == "href").lower()
    assert "&lt;b&gt;" in out


def test_http_links_are_rendered_safely_and_others_stay_text():
    msg = "Details: https://eduvidual.example/mod/assign/view.php?id=101&x=1. ftp://files.example/a und https://u:pw@evil.example/"
    out = render(make_report(changes=[change(status=ChangeStatus.FAILED, message=msg)]))
    page = Page(out)
    hrefs = [v for _, k, v in page.attrs if k == "href"]
    assert hrefs == ["https://eduvidual.example/mod/assign/view.php?id=101&x=1"]
    assert 'rel="noopener noreferrer"' in out
    assert "ftp://files.example/a" in page.visible  # shown as text, not linked
    assert "pw@evil" not in out  # credentials in URL masked


def test_url_with_secret_parameters_is_masked_and_not_linked():
    msg = f"https://eduvidual.example/x?sesskey={SESS}&authtoken={AUTH}"
    out = render(make_report(changes=[change(status=ChangeStatus.FAILED, message=msg)]))
    assert SESS not in out and AUTH not in out
    assert not [1 for _, k, _v in Page(out).attrs if k == "href"]


def test_safe_url_validation():
    ok = ["https://a.example/x?y=1#z", "http://localhost:8080/a", " https://a.example/ "]
    for u in ok:
        assert rp.safe_url(u) == u.strip()
    bad = [
        "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "file:///c:/x", "ftp://a.example/",
        "//a.example/x", "https://", "", "   ", 'https://a.example/"onmouseover=1', "https://a.example/<b>",
        "https://a.example/a b", "https://u:p@a.example/", "https://a.example:99999999/", "https://a.example/\x00",
        "https://a.example/" + "a" * 2100, None, 5, b"https://a.example/",
    ]
    for u in bad:
        assert rp.safe_url(u) is None, u


def test_link_text_uses_full_url_in_href_even_when_message_is_clipped():
    msg = "x " * 200 + "https://eduvidual.example/mod/assign/view.php?id=" + "9" * 200
    out = render(make_report(changes=[change(status=ChangeStatus.FAILED, message=msg)]))
    hrefs = [v for _, k, v in Page(out).attrs if k == "href"]
    assert hrefs and all("…" not in h for h in hrefs)


def test_every_foreign_field_is_sanitized_secrets_never_appear():
    leaky = (
        f"Passwort={PW}; token={TOK}; Cookie: MoodleSessioneduvidual={COOKIE}; sesskey={SESS}; "
        f"https://user:{PW}@eduvidual.example/p?authtoken={AUTH}"
    )
    err = ErrorInfo("schema", leaky, leaky, leaky)
    src = SourceRunResult(
        Source.EDUVIDUAL, SourceStatus.FAILED,
        categories={Category.ASSIGNMENTS: CategoryOutcome(Category.ASSIGNMENTS, CategoryStatus.FAILED, error=err, detail=leaky)},
        errors=[err],
    )
    r = make_report(
        status=OverallStatus.FAILED,
        sources={Source.EDUVIDUAL: src},
        calendar_target=TargetStatus(leaky, False, err),
        tasks_target=TargetStatus("Schule", None, err),
        changes=[change(leaky, status=ChangeStatus.FAILED, message=leaky, error=err)],
        notices=[Notice(Severity.ERROR, leaky, leaky, Source.EDUVIDUAL, title=leaky)],
        next_steps=[leaky],
    )
    out = render(r)
    for secret in ALL_SECRETS:
        assert secret not in out, secret
    assert "***" in out


def test_title_that_looks_like_a_password_assignment_is_masked():
    out = render(make_report(changes=[change(f"Login passwort: {PW}")]))
    assert PW not in out


# --------------------------------------------------------------------------- _sanitize hook / fallback


def test_sanitize_fallback_when_redact_module_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "schulsync.redact", None)  # import raises ImportError
    samples = {
        f"password={PW}": PW,
        f"Passwort: {PW}": PW,
        f"token={TOK}": TOK,
        f"access_token: {TOK}": TOK,
        f"Cookie: MoodleSessioneduvidual={COOKIE}; other=1": COOKIE,
        f"Set-Cookie: s={COOKIE}": COOKIE,
        f"sesskey={SESS}": SESS,
        f"https://x.example/?authtoken={AUTH}&a=1": AUTH,
        f"Authorization: Bearer abcdef123456{TOK}": TOK,
        f"Bearer {TOK}": TOK,
        f"https://user:{PW}@x.example/": PW,
        '{"password": "' + PW + '"}': PW,
        "ya29.A0dummyGoogleAccessToken_123": "A0dummyGoogleAccessToken",
    }
    for text, secret in samples.items():
        masked = rp._sanitize(text)
        assert secret not in masked, text
        assert "***" in masked, text


def test_sanitize_fallback_keeps_harmless_text_and_renders_without_redact(monkeypatch):
    monkeypatch.setitem(sys.modules, "schulsync.redact", None)
    assert rp._sanitize("Schularbeit Mathematik am 14.10.2026 in Raum 3B") == "Schularbeit Mathematik am 14.10.2026 in Raum 3B"
    out = render(make_report(changes=[change(f"Test password={PW}")]))
    assert PW not in out and "Test" in out


def test_sanitize_falls_back_when_redact_raises(monkeypatch):
    mod = types.ModuleType("schulsync.redact")

    def boom(_text):
        raise RuntimeError("kaputt")

    mod.redact = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "schulsync.redact", mod)
    assert PW not in rp._sanitize(f"password={PW}")


def test_sanitize_uses_redact_when_available_and_still_applies_fallback(monkeypatch):
    mod = types.ModuleType("schulsync.redact")
    mod.redact = lambda t: t.replace("GEHEIM", "<x>")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "schulsync.redact", mod)
    assert rp._sanitize("a GEHEIM b") == "a <x> b"
    assert PW not in rp._sanitize(f"GEHEIM password={PW}")


def test_sanitize_ignores_non_string_redact_result(monkeypatch):
    mod = types.ModuleType("schulsync.redact")
    mod.redact = lambda t: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "schulsync.redact", mod)
    assert rp._sanitize("hallo") == "hallo"


def test_real_redact_module_is_used_when_present():
    from schulsync import redact

    redact.register_secret("zzRegisteredDummySecretzz")
    try:
        out = render(make_report(changes=[change("Titel zzRegisteredDummySecretzz Ende")]))
    finally:
        redact.clear_registered_secrets()
    assert "zzRegisteredDummySecretzz" not in out


def test_hostile_huge_text_is_clipped_before_it_reaches_redact(monkeypatch):
    seen: list[int] = []
    mod = types.ModuleType("schulsync.redact")

    def spy(text: str) -> str:
        seen.append(len(text))
        return text

    mod.redact = spy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "schulsync.redact", mod)
    err = ErrorInfo("schema", "Antwort", None, "a-" * 500_000)
    render(make_report(status=OverallStatus.FAILED, changes=[change(status=ChangeStatus.FAILED, error=err)]))
    assert seen and max(seen) <= 20_000


# --------------------------------------------------------------------------- text handling


def test_very_long_text_is_clipped():
    long = "A" * 50_000
    r = make_report(changes=[change(long, status=ChangeStatus.FAILED, message=long, error=ErrorInfo("schema", long, long, long))])
    out = render(r)
    assert len(out) < 40_000
    assert "…" in out and "A" * 1600 not in out


def test_unicode_is_preserved_and_invisible_characters_removed():
    out = render(make_report(changes=[change("Übung Größe Čeština 日本語 😀 ß")]))
    for text in ("Übung Größe Čeština 日本語 😀 ß",):
        assert text in out
    title = "A\u061cB\u2060C\u2028D\u00adE\u202eF\ud800G\x00H\x07I"
    out = render(make_report(changes=[change(title)]))
    for bad in ("\u061c", "\u2060", "\u2028", "\u00ad", "\u202e", "\ud800", "\x00", "\x07"):
        assert bad not in out
    assert "ABCDEFGHI" in out


def test_lone_surrogate_does_not_prevent_writing_the_report(paths: Paths):
    p = rp.write_report(paths, make_report(changes=[change("Test \ud800 Mathe")]))
    assert "Mathe" in p.read_text(encoding="utf-8")


def test_many_rows_are_capped_with_a_summary_line():
    many = [change(f"Aufgabe {i}", uid=f"u{i}") for i in range(rp._MAX_ROWS + 25)]
    out = render(make_report(changes=many))
    assert "Aufgabe 0<" in out and f"Aufgabe {rp._MAX_ROWS + 24}<" not in out
    assert "und 25 weitere Zeilen" in out


def test_target_error_is_shown_even_if_ok_flag_is_none():
    err = ErrorInfo("config", "Kalender-ID fehlt in der Konfiguration.")
    out = render(make_report(status=OverallStatus.PARTIAL, calendar_target=TargetStatus("Kalender", None, err)))
    assert "Kalender-ID fehlt" in out


def test_unchecked_target_is_marked_not_checked():
    assert "Nicht geprüft" in render(make_report())


# --------------------------------------------------------------------------- determinism / minimal / empty


def test_output_is_deterministic_for_equal_input():
    a, b = render(rich_report()), render(rich_report())
    assert a == b
    assert render(hostile_report()) == render(hostile_report())


def test_fingerprint_comes_from_the_report():
    r = rich_report()
    assert r.fingerprint() in render(r)


def test_minimal_and_empty_reports_render():
    out = render(make_report())
    assert "Keine Quelle war aktiviert oder gelaufen." in out
    assert "Keine fehlgeschlagenen, zurückgestellten oder ungeklärten Schritte." in out
    assert "Keine Hinweise." in out and "Keine." in out
    Page(out)
    only = SourceRunResult(Source.WEBUNTIS, SourceStatus.DISABLED)
    out = render(make_report(sources={Source.WEBUNTIS: only}))
    assert "Abgeschaltet" in out and 'id="quelle-webuntis"' in out and 'id="quelle-eduvidual"' not in out


def test_responsive_dark_mode_and_print_styles():
    out = render(make_report())
    assert "prefers-color-scheme:dark" in out and "@media print" in out
    assert 'name="viewport"' in out and "color-scheme" in out and "max-width:40rem" in out
    assert "overflow-x:auto" in out  # wide tables scroll instead of breaking the layout


# --------------------------------------------------------------------------- write_report


def test_write_report_creates_dated_file_and_latest_copy(paths: Paths):
    r = rich_report()
    p = rp.write_report(paths, r)
    assert p == paths.reports / "bericht-2026-10-14.html"
    latest = paths.reports / "latest.html"
    assert latest.is_file() and not latest.is_symlink()
    assert p.read_bytes() == latest.read_bytes()
    text = p.read_text(encoding="utf-8")
    assert text == rp.render_html(r, generated_at=r.finished_at)
    assert not list(paths.reports.glob("*.tmp"))


def test_write_report_same_day_overwrites_and_other_day_adds(paths: Paths):
    rp.write_report(paths, make_report(status=OverallStatus.FAILED))
    p = rp.write_report(paths, make_report(status=OverallStatus.SUCCESS))
    names = sorted(f.name for f in paths.reports.iterdir())
    assert names == ["bericht-2026-10-14.html", "latest.html"]
    assert "Alles wurde erfolgreich synchronisiert." in p.read_text(encoding="utf-8")
    rp.write_report(paths, make_report(day=date(2026, 10, 15)))
    names = sorted(f.name for f in paths.reports.iterdir())
    assert names == ["bericht-2026-10-14.html", "bericht-2026-10-15.html", "latest.html"]
    assert "15.10.2026" in (paths.reports / "latest.html").read_text(encoding="utf-8")


def test_write_report_file_name_uses_the_vienna_day_of_the_report(paths: Paths):
    r = make_report(day=date(2026, 3, 29))
    assert rp.write_report(paths, r).name == "bericht-2026-03-29.html"


def test_write_report_is_atomic_on_failure(paths: Paths, monkeypatch):
    first = make_report(status=OverallStatus.SUCCESS)
    target = rp.write_report(paths, first)
    before = target.read_bytes()
    real_replace = os.replace

    def flaky(src, dst):
        if Path(dst).name.startswith("bericht-"):
            raise PermissionError(5, "Zugriff verweigert")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        rp.write_report(paths, make_report(status=OverallStatus.FAILED))
    assert target.read_bytes() == before  # old report untouched
    assert not list(paths.reports.glob("*.tmp"))  # no half-written leftovers
    # even then, the new report is not lost: latest.html carries it
    assert "Fehlgeschlagen" in (paths.reports / "latest.html").read_text(encoding="utf-8")


def test_failing_latest_copy_is_logged_but_does_not_fail(paths: Paths, monkeypatch, caplog):
    real_replace = os.replace

    def flaky(src, dst):
        if Path(dst).name == "latest.html":
            raise PermissionError(5, "von Browser gehalten")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with caplog.at_level(logging.WARNING, logger="schulsync.report"):
        p = rp.write_report(paths, make_report())
    assert p.is_file() and "latest.html" in caplog.text
    assert not list(paths.reports.glob("*.tmp"))


def test_write_report_creates_missing_reports_dir(tmp_path):
    p = Paths(tmp_path / "neu")
    out = rp.write_report(p, make_report())
    assert out.is_file() and out.parent == p.reports


def test_report_file_is_utf8_and_has_no_bom(paths: Paths):
    p = rp.write_report(paths, make_report(changes=[change("Größe 日本")]))
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and "Größe 日本".encode() in raw


def test_preview_is_written_to_vorschau_html_and_leaves_real_report_and_latest_alone(paths: Paths):
    real = rp.write_report(paths, make_report(status=OverallStatus.FAILED, changes=[change("Wichtige Schularbeit Deutsch", status=ChangeStatus.FAILED)]))
    before = (real.read_bytes(), (paths.reports / "latest.html").read_bytes())
    p = rp.write_report(paths, make_report(mode=RunMode.PREVIEW, status=OverallStatus.SUCCESS))
    assert p == paths.reports / "vorschau.html"
    assert "Vorschau - es wurde nichts geschrieben" in p.read_text(encoding="utf-8")
    assert (real.read_bytes(), (paths.reports / "latest.html").read_bytes()) == before
    assert "Wichtige Schularbeit Deutsch" in real.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- prune_reports


NOW = datetime(2026, 10, 14, 9, 0, tzinfo=VIENNA)


def _touch(path: Path, age_days: float = 0.0) -> Path:
    path.write_text("x", encoding="utf-8")
    t = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (t, t))
    return path


def test_prune_deletes_only_old_matching_files(paths: Paths):
    old = _touch(paths.reports / "bericht-2026-01-01.html")
    edge_keep = _touch(paths.reports / "bericht-2026-09-14.html")  # exactly 30 days: kept
    edge_drop = _touch(paths.reports / "bericht-2026-09-13.html")  # 31 days: deleted
    new = _touch(paths.reports / "bericht-2026-10-14.html")
    latest = _touch(paths.reports / "latest.html", 400)
    other = _touch(paths.reports / "notizen.txt", 400)
    preview = _touch(paths.reports / "vorschau.html", 400)
    deleted = rp.prune_reports(paths, 30, NOW)
    assert sorted(d.name for d in deleted) == ["bericht-2026-01-01.html", "bericht-2026-09-13.html"]
    assert not old.exists() and not edge_drop.exists()
    assert edge_keep.exists() and new.exists() and latest.exists() and other.exists() and preview.exists()


def test_prune_uses_vienna_day_not_utc_day(paths: Paths):
    f = _touch(paths.reports / "bericht-2026-10-13.html")
    # 14.10. 00:30 Vienna is 13.10. 22:30 UTC: still "today" in Vienna -> yesterday is 1 day old
    assert rp.prune_reports(paths, 1, datetime(2026, 10, 14, 0, 30, tzinfo=VIENNA)) == []
    assert f.exists()
    assert [d.name for d in rp.prune_reports(paths, 0, datetime(2026, 10, 14, 0, 30, tzinfo=VIENNA))] == [f.name]


def test_prune_handles_unparsable_names_by_mtime_and_missing_dir(paths: Paths, tmp_path):
    old = _touch(paths.reports / "bericht-alt.html", 90)
    fresh = _touch(paths.reports / "bericht-neu.html", 0)
    impossible = _touch(paths.reports / "bericht-2026-13-45.html", 90)
    rp.prune_reports(paths, 30, NOW)
    assert not old.exists() and not impossible.exists() and fresh.exists()
    assert rp.prune_reports(Paths(tmp_path / "gibtsnicht"), 30, NOW) == []
    with pytest.raises(ValueError):
        rp.prune_reports(paths, -1, NOW)


def test_prune_removes_orphaned_tmp_files_but_not_fresh_or_foreign_ones(paths: Paths):
    orphan = _touch(paths.reports / "bericht-2026-01-01.html.4242.tmp", 90)
    orphan_latest = _touch(paths.reports / "latest.html.4242.tmp", 90)
    fresh = _touch(paths.reports / "bericht-2026-10-14.html.4242.tmp", 0)  # may belong to a running write
    foreign = _touch(paths.reports / "notizen.tmp", 90)
    rp.prune_reports(paths, 30, NOW)
    assert not orphan.exists() and not orphan_latest.exists()
    assert fresh.exists() and foreign.exists()
    # keep_days=0 must not kill a tmp file of a write that is running right now
    rp.prune_reports(paths, 0, NOW)
    assert fresh.exists()


# --------------------------------------------------------------------------- latest_report


def test_latest_report(paths: Paths):
    assert rp.latest_report(paths) is None
    assert rp.latest_report(Paths(paths.home / "x")) is None
    p = rp.write_report(paths, make_report(day=date(2026, 10, 14)))
    assert rp.latest_report(paths) == paths.reports / "latest.html"
    (paths.reports / "latest.html").unlink()
    rp.write_report(paths, make_report(day=date(2026, 10, 15)))
    (paths.reports / "latest.html").unlink()
    assert rp.latest_report(paths) == paths.reports / "bericht-2026-10-15.html"
    assert p.exists()


# --------------------------------------------------------------------------- open_report


def test_open_report_success_uses_file_uri(paths: Paths):
    p = rp.write_report(paths, make_report())
    calls: list[str] = []
    assert rp.open_report(p, opener=lambda uri: calls.append(uri) or True) is True
    assert calls == [p.as_uri()] and calls[0].startswith("file:///")
    assert rp.open_report(p, opener=lambda uri: None) is True  # opener without return value


def test_open_report_failure_never_raises_keeps_file_and_logs_path(paths: Paths, caplog):
    p = rp.write_report(paths, make_report())

    def boom(_uri):
        raise RuntimeError("kein Browser")

    with caplog.at_level(logging.WARNING, logger="schulsync.report"):
        assert rp.open_report(p, opener=boom) is False
        assert rp.open_report(p, opener=lambda uri: False) is False
    assert p.exists()
    assert str(p) in caplog.text


def test_open_report_default_uses_webbrowser_then_startfile(paths: Paths, monkeypatch):
    p = rp.write_report(paths, make_report())
    seen: list[str] = []
    monkeypatch.setattr(rp.webbrowser, "open", lambda uri: seen.append(uri) or True)
    assert rp.open_report(p) is True and seen == [p.as_uri()]

    started: list[str] = []
    monkeypatch.setattr(rp.webbrowser, "open", lambda uri: False)
    monkeypatch.setattr(rp.os, "startfile", lambda f: started.append(f), raising=False)
    assert rp.open_report(p) is True and started == [str(p)]

    def bad_start(_f):
        raise OSError("keine Zuordnung")

    monkeypatch.setattr(rp.os, "startfile", bad_start, raising=False)
    assert rp.open_report(p) is False

    monkeypatch.setattr(rp.webbrowser, "open", lambda uri: (_ for _ in ()).throw(RuntimeError("x")))
    assert rp.open_report(p) is False


def test_open_report_does_not_fall_back_when_opener_injected(paths: Paths, monkeypatch):
    p = rp.write_report(paths, make_report())
    monkeypatch.setattr(rp.webbrowser, "open", lambda uri: pytest.fail("must not be called"))
    monkeypatch.setattr(rp.os, "startfile", lambda f: pytest.fail("must not be called"), raising=False)
    assert rp.open_report(p, opener=lambda uri: False) is False


def test_open_report_relative_path_does_not_raise(paths: Paths, monkeypatch):
    p = rp.write_report(paths, make_report())
    monkeypatch.chdir(p.parent)
    calls: list[str] = []
    assert rp.open_report(Path(p.name), opener=lambda uri: calls.append(uri) or True) is True
    assert calls and calls[0].startswith("file:///")


# --------------------------------------------------------------------------- eyeball sample


def test_a_rendered_report_file_has_the_sections_the_requirements_name_and_survives_a_utf8_round_trip(tmp_path):
    """ANFORDERUNGEN 8: what the report must show, as the headings of the file that is opened. (This was an 'eyeballing' test
    that wrote into the source tree and asserted only a file size; it now writes to a temp dir and checks the content.)"""
    page = render(rich_report())
    target = tmp_path / "bericht.html"
    target.write_text(page, encoding="utf-8")
    assert target.read_text(encoding="utf-8") == page  # umlauts etc. survive the file
    assert page.startswith("<!DOCTYPE html>") and '<html lang="de">' in page and '<meta charset="utf-8">' in page
    headings = re.findall(r"<h[12][^>]*>(.*?)</h[12]>", page)
    assert headings == [
        "SchulSync-Bericht", "Ergebnis je Quelle", "Google-Ziele", "Fehlgeschlagene und zurückgestellte Schritte",
        "Bereits erfolgreich übernommen", "Ausstehende Google-Schreibvorgänge", "Hinweise und Warnungen", "Nächste Schritte",
    ]
    preview = render(make_report(mode=RunMode.PREVIEW, status=OverallStatus.SUCCESS))
    assert "Vorschau" in preview and "<h1" in preview


# --------------------------------------------------------------------------- round-2 fixes


def test_link_trailing_punctuation_and_unbalanced_bracket_are_not_part_of_the_url():
    out = render(make_report(changes=[change(status=ChangeStatus.FAILED, message="(siehe https://example.at/a/b) und https://example.at/c.")]))
    hrefs = re.findall(r'href="([^"]*)"', out)
    assert hrefs[:2] == ["https://example.at/a/b", "https://example.at/c"]


def test_clipped_long_text_never_shows_a_cut_registered_secret():
    from schulsync import redact

    redact.register_secret("sekret-dummy-value")
    try:
        title = "A" * 790 + "sekret-dummy-val"  # cut mid-secret by the pre-clip
        out = render(make_report(changes=[change(title=title + "x" * 2000)]))
    finally:
        redact.clear_registered_secrets()
    assert "sekret" not in out


def test_latest_report_prefers_a_same_day_dated_file_that_is_newer_than_latest(paths: Paths):
    rp.write_report(paths, make_report(day=date(2026, 10, 14)))
    dated = paths.reports / "bericht-2026-10-14.html"
    latest = paths.reports / "latest.html"
    os.utime(latest, (1_000_000_000, 1_000_000_000))  # latest copy failed to refresh -> older
    assert rp.latest_report(paths) == dated


def test_failed_report_does_not_assert_that_nothing_was_deleted():
    out = render(make_report(status=OverallStatus.FAILED, changes=[change(error=ErrorInfo("schema", "x"))]))
    assert "nichts gelöscht" not in out
