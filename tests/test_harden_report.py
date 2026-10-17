"""H9 (report part): the codes 'run_timeout', 'timeout', 'unexpected' and 'targets_changed' get specific German next
steps instead of the generic fallback; the new restore notices carry hints."""

from __future__ import annotations

import re

import pytest

from schulsync import report as rp
from schulsync.errors import ErrorInfo
from schulsync.models import (
    ChangeStatus,
    Notice,
    OverallStatus,
    Severity,
    Source,
    SourceRunResult,
    SourceStatus,
)
from tests.test_report import change, make_report, render

GENERIC_MARK = "nicht genauer einordnen"


def steps_of(r) -> str:
    return re.search(r'<section id="naechste-schritte">(.*?)</section>', render(r), re.S).group(1)


@pytest.mark.parametrize(
    "code, expected",
    [
        ("run_timeout", "Zeitlimit"),
        ("timeout", "Zeitlimit"),
        ("unexpected", "Logdatei"),
    ],
)
def test_error_codes_get_a_specific_step_not_the_generic_fallback(code, expected):
    r = make_report(status=OverallStatus.FAILED, changes=[change(status=ChangeStatus.FAILED, error=ErrorInfo(code, "Es ging etwas schief."))])
    section = steps_of(r)
    assert expected in section and GENERIC_MARK not in section
    assert code in rp._CODES_WITH_STEP


def test_source_level_timeout_error_is_covered_too():
    res = SourceRunResult(Source.WEBUNTIS, SourceStatus.FAILED, errors=[ErrorInfo("run_timeout", "Das Zeitlimit für den Lauf wurde erreicht.")])
    section = steps_of(make_report(status=OverallStatus.FAILED, sources={Source.WEBUNTIS: res}))
    assert "Zeitlimit" in section and "nächsten Start" in section and GENERIC_MARK not in section


def test_the_timeout_step_tells_what_to_do_when_it_repeats():
    r = make_report(status=OverallStatus.FAILED, changes=[change(status=ChangeStatus.FAILED, error=ErrorInfo("timeout", "Hart abgebrochen."))])
    section = steps_of(r)
    assert "Internetverbindung" in section and "schulsync check" in section


def test_targets_changed_notice_gets_its_own_step_even_in_a_successful_report():
    r = make_report(
        status=OverallStatus.SUCCESS,
        notices=[Notice(Severity.WARNING, "targets_changed", "Kalender oder Aufgabenliste wurden gewechselt.")],
    )
    section = steps_of(r)
    assert "Keine Maßnahmen nötig" not in section and "neuen Zielen" in section and "schulsync setup --google" in section


def test_restore_notices_show_a_hint_in_the_notice_list():
    r = make_report(
        notices=[
            Notice(Severity.WARNING, "restore_blocked", "Nicht wiederhergestellt: Der Eintrag ist abgesagt.", Source.EDUVIDUAL, "eduvidual:assignment:assign:1", "Laborprotokoll"),
            Notice(Severity.INFO, "restore_done", "Wiederhergestellt: Der Eintrag wird in Google neu angelegt.", Source.EDUVIDUAL, "eduvidual:assignment:assign:2", "Protokoll"),
        ]
    )
    html = render(r)
    assert "Nicht wiederhergestellt" in html and "werden nie neu angelegt" in html
    assert "restore_blocked" in html and "restore_done" in html


def test_targets_changed_notice_has_a_hint_text():
    html = render(make_report(notices=[Notice(Severity.WARNING, "targets_changed", "Kalender oder Aufgabenliste wurden gewechselt.")]))
    assert "targets_changed" in html and "früheren" in html


def test_report_file_for_names_the_file_write_report_writes(tmp_path):
    from schulsync.config import Paths
    from schulsync.models import RunMode

    paths = Paths(tmp_path / "S")
    normal, preview = make_report(), make_report(mode=RunMode.PREVIEW)
    assert rp.report_file_for(paths, normal) == rp.write_report(paths, normal)
    assert rp.report_file_for(paths, preview) == rp.write_report(paths, preview)
