"""Audit 3 (ANFORDERUNGEN sections 4 and 5): tests that demonstrate gaps. Each test is EXPECTED TO FAIL today.

Only gaps that can be fixed in code/docs are pinned here; everything that needs a real Google/Eduvidual/WebUntis
account is listed in the audit table instead (a fake cannot prove it).
"""

from __future__ import annotations

from pathlib import Path

from schulsync.models import ChangeStatus, Target
from tests.support.acceptance_harness import World, at
from tests.support.fake_google import FakeGoogle

README = Path(__file__).resolve().parents[1] / "README.md"
DAY1 = at(2026, 10, 14, 8, 0)


def _readme() -> str:
    assert README.is_file(), "README.md fehlt (pyproject.toml und cli.py verweisen darauf)"
    return README.read_text(encoding="utf-8").lower()


# --------------------------------------------------------------------------- 4: "Dokumentiere Einrichtung ..."


def test_readme_documents_the_google_cloud_setup_step_by_step():
    """ANFORDERUNGEN 4: "Dokumentiere Einrichtung von Google-Cloud-Projekt, Calendar API, Tasks API und Desktop-OAuth-Zugang
    verständlich." cli._setup_google tells the user "die Anleitung steht in der README" - there is no README."""
    text = _readme()
    for needle in ("google cloud", "calendar api", "tasks api", "desktop", "oauth"):
        assert needle in text, needle


def test_readme_describes_a_setup_for_permanent_personal_use_and_the_seven_day_limit():
    """ANFORDERUNGEN 4: "Beachte, dass Refresh-Tokens bei externer OAuth-App im Status 'Testing' ... nach sieben Tagen ablaufen
    können. Beschreibe eine für den persönlichen Dauerbetrieb geeignete Einrichtung ... Versprich keine unbegrenzt gültige
    Anmeldung." Today this exists only as one sentence in the wizard output and as a report hint."""
    text = _readme()
    assert "testing" in text and ("7 tage" in text or "sieben tage" in text)
    assert "produktion" in text  # "In Produktion" / publishing status as the way out
    assert "unbegrenzt" not in text.replace("keine unbegrenzte", "").replace("nicht unbegrenzt", "")


def test_readme_says_reminders_are_not_retroactive_and_depend_on_the_calendar_app_settings():
    """ANFORDERUNGEN 5: "Bei kurzfristig entdeckten Terminen keine rückwirkende Benachrichtigung versprechen. Die Anzeige hängt
    außerdem von den Benachrichtigungseinstellungen meiner Google-Kalender-App ab." Nothing user-facing says so."""
    text = _readme()
    assert "benachrichtigung" in text
    assert "rückwirkend" in text or "nachträglich" in text


# --------------------------------------------------------------------------- 4: "die betroffenen Schreibvorgänge zurückstellen"


def test_a_missing_task_list_defers_only_the_task_writes_the_calendar_entries_are_still_written(paths):
    """ANFORDERUNGEN 4: "Ist ein Ziel nicht erreichbar, die betroffenen Schreibvorgänge zurückstellen". Only the Tasks writes are
    affected by a missing task list, yet SyncEngine._connect halts EVERY write (ACCEPTANCE_NOTES A4 chose that on purpose;
    this test pins the wording of the requirement instead). Today: 0 events, 0 tasks, calendar changes DEFERRED too."""
    g = FakeGoogle(tasklist_exists=False)
    w = World(paths, google=g)
    w.load_standard()

    out = w.start(at=DAY1)

    assert len(g.events) == 4, "the calendar 'Schule' is reachable: its entries must not wait for the task list"
    assert g.tasks == []
    deferred = {c.target for c in out.report.changes if c.status == ChangeStatus.DEFERRED}
    assert deferred == {Target.TASKS}


def test_a_missing_calendar_defers_only_the_calendar_writes_the_tasks_are_still_written(paths):
    """Same as above for the other target: a missing calendar must not keep the (reachable) task list empty."""
    g = FakeGoogle(calendar_exists=False)
    w = World(paths, google=g)
    w.load_standard()

    out = w.start(at=DAY1)

    assert g.events == {} and g.primary_events == {}  # never a fallback calendar
    assert len(g.tasks) == 3, "the task list 'Schule' is reachable: its tasks must not wait for the calendar"
    deferred = {c.target for c in out.report.changes if c.status == ChangeStatus.DEFERRED}
    assert deferred == {Target.CALENDAR}
