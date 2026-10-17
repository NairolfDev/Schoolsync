"""Adversarial review (round 2) tests for schulsync.report. Each test demonstrates one defect and
FAILS against the current implementation."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from schulsync import redact
from schulsync import report as rp
from schulsync.config import Paths
from schulsync.errors import ErrorInfo
from schulsync.models import (
    Action,
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
)
from schulsync.timeutil import VIENNA


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


def render(r: RunReport) -> str:
    return rp.render_html(r, generated_at=r.finished_at)


def _change(title="Mathe Test", **kw) -> Change:
    base = dict(
        uid="u1",
        source=Source.WEBUNTIS,
        kind=Kind.EXAM,
        title=title,
        target=Target.CALENDAR,
        action=Action.CREATE,
        status=ChangeStatus.FAILED,
    )
    base.update(kw)
    return Change(**base)


def _section(html_text: str, section_id: str) -> str:
    mo = re.search(rf'<section id="{section_id}">(.*?)</section>', html_text, re.S)
    assert mo, f"section {section_id} missing"
    return mo.group(1)


# --------------------------------------------------------------------------- findings


def test_incomplete_data_hint_does_not_claim_entry_was_taken_over_when_adapter_skipped_it():
    """eduvidual.py (lines ~1046-1058) emits code `incomplete_data` with messages like
    'Abgabefrist der Aufgabe ... ist nicht lesbar; uebersprungen.' (item NOT taken over). The
    hardcoded hint for that code says 'Der Eintrag wurde trotzdem uebernommen' - a direct
    contradiction on the same list entry (the hint is attached by code, not by what happened)."""
    n = Notice(
        Severity.WARNING,
        "incomplete_data",
        "Abgabefrist der Aufgabe „Referat“ ist nicht lesbar; übersprungen.",
        Source.EDUVIDUAL,
        title="Referat",
    )
    out = render(make_report(notices=[n]))
    assert "trotzdem übernommen" not in _section(out, "hinweise")


def test_failed_sentence_does_not_claim_nothing_was_deleted_when_a_task_removal_was_applied():
    """_OVERALL[FAILED] always prints 'Es wurde nichts geloescht'. A FAILED run (no source SUCCESS)
    can still contain an APPLIED REMOVE_TASK (source A removed a cancelled task, then a later write
    or source B failed). The page then states something false about data the user cares about."""
    removed = _change(
        title="Hausübung Englisch",
        target=Target.TASKS,
        action=Action.REMOVE_TASK,
        status=ChangeStatus.APPLIED,
    )
    failed = _change(uid="u2", error=ErrorInfo("network", "Keine Verbindung."))
    out = render(make_report(status=OverallStatus.FAILED, changes=[removed, failed]))
    assert "nichts gelöscht" not in out


@pytest.fixture
def registered_secret():
    secret = "hunter2-dummy-pw"
    redact.register_secret(secret)
    try:
        yield secret
    finally:
        redact.clear_registered_secrets()


def test_registered_secret_is_not_partially_leaked_when_masking_shrinks_text_before_the_clip(registered_secret):
    """`_clean` pre-clips to 4*limit BEFORE masking and claims 'a secret cut at its edge lies beyond
    what is ever shown'. That is false when masking shrinks the text: a long `token=xxxx...` value
    collapses to `token=***`, the cut-off prefix of the registered secret then moves into the
    visible 200 characters and is no longer recognised by redact (it is only a prefix)."""
    tail = " " + registered_secret[:-1]  # secret cut one char short by the pre-clip
    title = "token=" + "x" * (800 - 6 - len(tail)) + tail + registered_secret[-1]
    out = render(make_report(status=OverallStatus.FAILED, changes=[_change(title=title)]))
    assert "hunter2" not in out


def test_render_survives_lone_surrogate_in_an_identifier_used_by_the_fingerprint():
    """The footer calls `RunReport.fingerprint()` -> sha256_hex(text.encode('utf-8')), which raises
    UnicodeEncodeError for a lone surrogate in Change.uid (uid is built from the platform's
    source_id). render_html has no fallback, so the whole failure report is lost - the claim 'lone
    surrogates no longer stop the report from being written' only holds for displayed fields."""
    r = make_report(status=OverallStatus.FAILED, changes=[_change(uid="webuntis:exam:\ud800")])
    assert "SchulSync-Bericht" in render(r)


def test_preview_report_does_not_show_a_success_badge_for_a_source():
    """ANFORDERUNGEN 9/10: a preview must not book a success. The header is careful, but the source
    card still carries the badge 'Erfolgreich' (and 'Heute bereits erledigt')."""
    res = SourceRunResult(Source.EDUVIDUAL, SourceStatus.SUCCESS, calendar=Counts(new=2), tasks=Counts(new=2))
    out = render(make_report(mode=RunMode.PREVIEW, sources={Source.EDUVIDUAL: res}))
    card = re.search(r'<article class="card" id="quelle-eduvidual">(.*?)</article>', out, re.S)
    assert card
    assert "Erfolgreich" not in card.group(1)


def test_latest_report_does_not_return_a_stale_latest_copy_when_the_newer_dated_file_exists(tmp_path, monkeypatch):
    """write_report tolerates a failing latest.html copy (logged only). latest_report() then still
    prefers latest.html, i.e. the OLD report of an earlier day, although a newer dated report exists
    -> `schulsync report` opens outdated content."""
    paths = Paths(tmp_path)
    rp.write_report(paths, make_report(day=date(2026, 10, 13), status=OverallStatus.FAILED,
                                       changes=[_change(title="Alter Fehler vom Dienstag")]))
    real_replace = os.replace

    def flaky(src, dst):
        if Path(dst).name == "latest.html":
            raise PermissionError(5, "Zugriff verweigert")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    newer = rp.write_report(paths, make_report(day=date(2026, 10, 14), status=OverallStatus.FAILED,
                                               changes=[_change(title="Neuer Fehler vom Mittwoch")]))
    assert rp.latest_report(paths) == newer


@pytest.mark.parametrize("reason", ["done_untouched", "removed_cancelled", "no_task_for_kind"])
def test_skip_reasons_produced_by_rules_are_shown_in_german_not_as_raw_identifiers(reason):
    """rules.py produces the plan reasons done_untouched / removed_cancelled / no_task_for_kind (and
    Counts.skip(reason) keys them verbatim). `_SKIP_REASONS` maps only 4 codes; every other reason
    is printed as the raw English identifier (DESIGN section 0: everything user-visible in German)."""
    c = Counts()
    c.skip(reason)
    res = SourceRunResult(Source.WEBUNTIS, SourceStatus.SUCCESS, tasks=c)
    out = render(make_report(sources={Source.WEBUNTIS: res}))
    assert reason not in out


def test_skip_change_is_not_listed_under_successfully_taken_over():
    """Action.SKIP means nothing was written. A SKIP change (status APPLIED, or PLANNED in a
    preview) is rendered as a row 'Uebersprungen' inside 'Bereits erfolgreich uebernommen'."""
    skip = _change(title="Abgelaufene Abgabe", action=Action.SKIP, status=ChangeStatus.APPLIED)
    out = render(make_report(changes=[skip]))
    assert "Abgelaufene Abgabe" not in _section(out, "uebernommen")


def test_link_keeps_closing_parenthesis_that_belongs_to_the_url():
    """Trailing ')' is always stripped from a matched URL, so 'Mathematik_(Fach)' becomes a link
    to a different (wrong) page ending in '_(Fach'."""
    msg = "Siehe https://de.wikipedia.org/wiki/Mathematik_(Fach) für Details"
    out = render(make_report(changes=[_change(message=msg)]))
    hrefs = re.findall(r'href="([^"]*)"', out)
    assert hrefs
    assert hrefs[0].endswith("(Fach)"), hrefs
