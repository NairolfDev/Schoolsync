"""Acceptance tests for ANFORDERUNGEN section 8 (HTML error report) as seen from a complete run.

Section 10 bullet 10: the report opens in the user's browser (injected opener), escapes foreign HTML and
contains no secrets. Also: opens only for problems, once per identical problem and day, success opens nothing,
the stored report is refreshed anyway, an opener that fails keeps the report and logs its place.

The report's own rendering is covered by test_report*.py; these tests check what the run hands to it.
"""

from __future__ import annotations

import dataclasses
import logging
import re

import pytest

from schulsync import redact
from schulsync.errors import ErrorInfo, SchemaError
from schulsync.logging_setup import setup_logging, shutdown_logging
from schulsync.models import Category, Notice, Severity, Source, SourceStatus
from tests.support import samples
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    HOMEWORK,
    LESSON,
    World,
    all_log_text,
    at,
    cancelled,
    event_for,
    fail_adapter,
    heal_adapter,
    hrefs,
    moved,
    read_report,
    world,  # noqa: F401  (fixture)
)

EDU, WEB = Source.EDUVIDUAL, Source.WEBUNTIS
DAY1 = at(2026, 10, 14, 8, 0)


@pytest.fixture
def file_logging(world: World):
    """The real log setup (daily file + redacting filter) that `schulsync run` installs. The acceptance world calls the runner
    directly, which configures no file logging: without this fixture the log files of these tests stay EMPTY, so an assertion
    'secret not in the logs' cannot fail (verified: removing the filter left such tests green)."""
    setup_logging(world.paths, level=logging.DEBUG)
    yield
    shutdown_logging()


# --------------------------------------------------------------------------- when the browser opens


def test_a_successful_run_opens_no_browser_page(world: World):
    world.load_standard()
    out = world.start(at=DAY1)
    assert out.exit_code == 0 and world.rec.opened == []


def test_warnings_alone_are_no_reason_to_open_the_browser(world: World):
    world.load_standard()
    world.eduvidual.set_notices([Notice(Severity.WARNING, "no_date", "Eintrag ohne Datum übersprungen.", EDU, title="Referat")])
    out = world.start(at=DAY1)
    assert out.exit_code == 0 and world.rec.opened == []


def test_a_partial_run_opens_the_report_once_and_it_tells_what_the_user_needs(world: World):
    world.load_standard()
    world.webuntis.fail_category(
        Category.EXAMS, ErrorInfo("schema", "Die Prüfungsseite hat eine unerwartete Struktur (Test).")
    )

    out = world.start(at=DAY1)

    assert out.exit_code == 1
    assert world.rec.opened == [out.report_path] and out.report_path.is_file()
    assert out.report_path.parent == world.paths.reports  # a local, self-contained file
    html = read_report(out)
    assert html.startswith("<!DOCTYPE html>") and 'lang="de"' in html
    assert "14.10.2026" in html and "Europe/Vienna" in html and "MESZ" in html  # time with time zone
    assert "Teilweise fehlgeschlagen" in html  # overall status
    for text in ("Eduvidual", "WebUntis", "Google Kalender", "Google Tasks"):  # result per source and per Google target
        assert text in html, text
    for column in ("Neu", "Aktualisiert", "Unverändert", "Abgesagt", "Übersprungen"):  # the counts
        assert column in html, column
    assert "Prüfungen und Schularbeiten" in html and "unerwartete Struktur" in html  # the concretely failed step
    assert "Bereits erfolgreich übernommen" in html  # what already worked ...
    assert ASSIGNMENT.title in html and HOMEWORK.title in html  # ... by name
    assert "Letzter erfolgreicher Abruf" in html
    assert "Nächste Schritte" in html and "schulsync check" in html  # sensible next steps


def test_the_time_zone_in_the_report_follows_winter_time(world: World):
    world.load_standard()
    world.webuntis.fail_category(Category.EXAMS, ErrorInfo("schema", "Seitenstruktur geändert (Test)."))
    out = world.start(at=at(2026, 11, 3, 8, 0))  # CET
    html = read_report(out)
    assert "MEZ" in html and "MESZ" not in html


def test_identical_problems_open_one_tab_per_day_but_the_stored_report_is_refreshed_each_time(world: World):
    world.load_standard()
    fail_adapter(world.webuntis, times=200)

    first = world.start(at=at(2026, 10, 14, 8, 0))
    second = world.start(at=at(2026, 10, 14, 9, 0))  # next logon, same problem
    third = world.start(at=at(2026, 10, 14, 10, 0))

    assert world.rec.opened == [first.report_path]  # exactly one tab for three identical failures
    assert second.report_path == first.report_path == third.report_path  # one report file per day ...
    assert "10:00:00" in read_report(third) and "09:00:00" not in read_report(third)  # ... always the latest content

    tomorrow = world.start(at=at(2026, 10, 15, 8, 0))  # a new day, the problem persists
    assert world.rec.opened == [first.report_path, tomorrow.report_path]


def test_a_different_problem_on_the_same_day_opens_the_report_again(world: World):
    world.load_standard()
    fail_adapter(world.webuntis)
    world.start(at=at(2026, 10, 14, 8, 0))
    assert len(world.rec.opened) == 1
    heal_adapter(world.webuntis)
    world.webuntis.login_fails(ErrorInfo("auth", "Anmeldung abgelehnt (Test)."))  # not the same error any more

    world.start(at=at(2026, 10, 14, 9, 0))

    assert len(world.rec.opened) == 2


def test_open_on_never_keeps_the_browser_closed_but_the_report_is_stored(world: World):
    world.load_standard()
    world.configure(report={"open_on": "never"})
    fail_adapter(world.webuntis)
    out = world.start(at=DAY1)
    assert world.rec.opened == [] and out.report_path.is_file()


def test_if_the_browser_cannot_be_opened_the_report_stays_and_its_place_is_logged(world: World, caplog):
    caplog.set_level(logging.DEBUG)
    world.load_standard()
    fail_adapter(world.webuntis)
    world.rec.open_result = False  # the opener reports failure

    out = world.start(at=at(2026, 10, 14, 8, 0))

    assert out.exit_code == 1 and out.report_path.is_file()
    logged = caplog.text + all_log_text(world.paths)
    assert out.report_path.name in logged, "the place of the report must be logged when opening fails"

    world.rec.open_result = True
    world.start(at=at(2026, 10, 14, 9, 0))  # a failed opening is not "already shown today"
    assert len(world.rec.opened) == 2


# --------------------------------------------------------------------------- counts, outdated data


def test_the_report_counts_new_updated_unchanged_cancelled_and_skipped_entries_per_source(world: World):
    expired = samples.assignment(cmid="1", title="Alte Abgabe", due=at(2026, 10, 13, 23, 59))
    far_exam = samples.exam(eid="far", start=at(2026, 11, 4, 0, 0), end=at(2026, 11, 4, 0, 45))
    exam = samples.exam()
    world.eduvidual.set_items([ASSIGNMENT, expired])
    world.webuntis.set_items([HOMEWORK, exam, LESSON, far_exam])

    day1 = world.start(at=DAY1)

    edu, web = day1.report.sources[EDU], day1.report.sources[WEB]
    assert (edu.calendar.new, edu.tasks.new) == (1, 1)
    assert edu.calendar.skipped == 1 and edu.calendar.skipped_reasons.get("expired") == 1
    assert (web.calendar.new, web.tasks.new) == (3, 2)
    assert web.calendar.skipped == 1 and web.calendar.skipped_reasons.get("outside_window") == 1

    world.eduvidual.set_items([moved(ASSIGNMENT, at(2026, 10, 24, 18, 0)), expired])
    world.webuntis.set_items([HOMEWORK, cancelled(exam), LESSON])
    day2 = world.start(at=at(2026, 10, 15, 8, 0))

    edu, web = day2.report.sources[EDU], day2.report.sources[WEB]
    assert (edu.calendar.updated, edu.tasks.updated, edu.calendar.new) == (1, 1, 0)
    assert (web.calendar.cancelled, web.calendar.unchanged, web.calendar.new) == (1, 2, 0)


def test_outdated_data_and_the_last_successful_fetch_are_shown(world: World):
    world.load_standard()
    world.start(at=DAY1)
    fail_adapter(world.eduvidual)

    out = world.start(at=at(2026, 10, 15, 8, 0))

    res = out.report.sources[EDU]
    assert res.status == SourceStatus.FAILED and res.stale
    assert res.last_success_at is not None and res.last_success_at.date() == DAY1.date()
    html = read_report(out)
    assert "Daten veraltet" in html and "14.10.2026 08:00" in html  # the day of the last real success is named
    assert not out.report.sources[WEB].stale


# --------------------------------------------------------------------------- hostile text and secrets


HOSTILE_TITLE = '<script>alert("x")</script> & <img src=x onerror=alert(1)>'


def test_foreign_html_is_escaped_links_are_validated_and_nothing_is_executable(world: World):
    item = dataclasses.replace(
        ASSIGNMENT,
        source_id="assign:666",
        title=HOSTILE_TITLE,
        course="<b>Kurs</b>",
        description='javascript:alert(1) <iframe src="//evil.example/x"></iframe>',
        url="javascript:alert(1)",
    )
    world.eduvidual.set_items([item])
    world.eduvidual.set_notices(
        [
            Notice(
                Severity.WARNING,
                "incomplete_data",
                "Titel <script>steal()</script> siehe https://www.eduvidual.at/mod/assign/view.php?id=1&x=2 "
                "oder javascript:alert(2) oder https://user:pw@evil.example/x",
                EDU,
                title="<img src=x onerror=alert(3)>",
            )
        ]
    )
    world.webuntis.set_items([HOMEWORK])
    world.webuntis.fail_category(
        Category.EXAMS,
        ErrorInfo("schema", "Seite <b>geändert</b>", hint="Tipp <script>alert(4)</script>", detail="<iframe src=//evil.example></iframe>"),
    )

    out = world.start(at=DAY1)

    assert HOSTILE_TITLE in event_for(world.google, item.uid)["summary"]  # copied to Google as plain text, unaltered
    html = read_report(out)
    low = html.lower()
    for tag in ("<script", "<img", "<iframe", "<b>", "<link", "@import", "url("):
        assert tag not in low, tag
    assert re.search(r"<[a-z][^>]*\ssrc\s*=", low) is None  # no tag with a source attribute
    assert "&lt;script&gt;" in html  # shown as text
    assert re.search(r"<[a-z][^>]*\bon[a-z]+\s*=", low) is None  # no event handler attribute on any tag
    assert "default-src 'none'" in html  # restrictive content security policy
    for href in hrefs(html):
        assert re.match(r"https?://", href) and "@" not in href and "evil.example" not in href, href
    assert any(h.startswith("https://www.eduvidual.at/") for h in hrefs(html))  # the valid link is a link


_SECRET_DETAIL = (
    "login failed: password=hunter2-dummy; Cookie: MoodleSession=abc123def456; "
    "Authorization: Bearer abcdef.ghijkl.mnopqr-secret; sesskey=Zk3jd9sd0; "
    "https://schueler:pw-dummy-9@host.example/?token=SECRETTOKEN123"
)
_SECRETS = ("hunter2-dummy", "abc123def456", "ghijkl.mnopqr-secret", "Zk3jd9sd0", "pw-dummy-9", "SECRETTOKEN123")


def test_no_secret_reaches_the_report_the_console_text_or_the_logs(world: World, file_logging, caplog):
    caplog.set_level(logging.DEBUG)
    world.load_standard()
    world.webuntis.fail_category(Category.EXAMS, ErrorInfo("schema", "Seitenstruktur geändert", detail=_SECRET_DETAIL))
    world.eduvidual.set_notices([Notice(Severity.WARNING, "incomplete_data", f"Debug: {_SECRET_DETAIL}", EDU, title="Details")])

    out = world.start(at=DAY1)

    logs = all_log_text(world.paths)
    assert "Eduvidual" in logs or "WebUntis" in logs or "Lauf" in logs  # the log files were really written: the leg below is not vacuous
    for where, text in (("report", read_report(out)), ("console", out.console_text), ("log files", logs)):
        for secret in _SECRETS:
            assert secret not in text, f"{secret!r} leaked into the {where}"


def _run_with_registered_secret(world: World, secret: str):
    world.load_standard()
    world.webuntis.fail_category(Category.EXAMS, ErrorInfo("schema", f"Anmeldung mit {secret} fehlgeschlagen", detail=f"Eingabe war {secret}"))
    return world.start(at=DAY1)


def test_a_registered_secret_is_masked_in_the_report_and_the_logs(world: World, file_logging):
    secret = "Sommer-Passwort-2026-dummy"
    redact.register_secret(secret)
    try:
        out = _run_with_registered_secret(world, secret)
        assert secret not in read_report(out)
        logs = all_log_text(world.paths)
        assert logs.strip()  # the log files were really written: the assertion below is not vacuous
        assert secret not in logs
    finally:
        redact.clear_registered_secrets()


def test_a_registered_secret_is_masked_in_the_console_text_too(world: World):
    """Defense in depth: the console summary may be captured by a terminal or a scheduler."""
    secret = "Sommer-Passwort-2026-dummy"
    redact.register_secret(secret)
    try:
        out = _run_with_registered_secret(world, secret)
        assert secret not in out.console_text
    finally:
        redact.clear_registered_secrets()


def test_a_secret_that_really_reaches_a_log_call_is_masked_there_and_in_the_report(world: World, file_logging):
    """The two tests above put the secret into texts the runner never logs (a category error detail, a notice), so their 'log
    files' leg could hardly fail. Here the secret sits in the message of an error that the runner DOES log (a whole source
    failing, `Runner._source_failed`). Non-vacuous: the log must show the failure with the mask in place of the secret, a
    registered password and an unregistered `key=value` secret alike; without the redacting filter the secret is in the file."""
    registered, unregistered = "Sommer-Passwort-2026-dummy", "Zk3jd9sd0unregistered"
    redact.register_secret(registered)
    try:
        world.load_standard()
        fail_adapter(world.eduvidual, SchemaError(f"Antwort unerwartet: {registered} sowie sesskey={unregistered}"))

        out = world.start(at=DAY1)

        logs = all_log_text(world.paths)
        assert "Eduvidual" in logs and "(schema)" in logs and "***" in logs  # the secret's line IS in the log, masked
        for where, text in (("log files", logs), ("report", read_report(out)), ("console", out.console_text)):
            assert registered not in text and unregistered not in text, f"a secret leaked into the {where}"
    finally:
        redact.clear_registered_secrets()


# --------------------------------------------------------------------------- limited retention


def test_old_reports_and_logs_are_removed_recent_ones_stay(world: World):
    world.paths.reports.mkdir(parents=True, exist_ok=True)
    world.paths.logs.mkdir(parents=True, exist_ok=True)
    old_report, new_report = world.paths.reports / "bericht-2026-08-01.html", world.paths.reports / "bericht-2026-10-10.html"
    old_log, new_log = world.paths.logs / "schulsync-2026-08-01.log", world.paths.logs / "schulsync-2026-10-10.log"
    for f in (old_report, new_report, old_log, new_log):
        f.write_text("alt", encoding="utf-8")
    world.load_standard()

    world.start(at=DAY1)  # keep_days / log_keep_days = 30

    assert not old_report.exists() and not old_log.exists()
    assert new_report.exists() and new_log.exists()
