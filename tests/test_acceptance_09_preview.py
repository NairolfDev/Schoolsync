"""Acceptance tests for ANFORDERUNGEN section 9 (preview) and section 10, last bullet.

"Eine Vorschau darf keine Synchronisation als erfolgreich verbuchen und keine spaeter erforderlichen
Schreibvorgaenge unterdruecken": the preview reads the sources, shows the planned Google changes, and leaves
Google AND the local bookkeeping exactly as they were.

Reading chosen where the text leaves room: a preview also leaves the source status (an auth block after a
rejected login) alone, because it is bookkeeping of the real runs.
"""

from __future__ import annotations

from schulsync.errors import AuthError, GoogleAuthError
from schulsync.models import Action, ChangeStatus, RunMode, Source, Target
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    EXAM,
    HOMEWORK,
    LESSON,
    TODAY,
    World,
    at,
    due_of,
    event_for,
    event_span,
    fail_adapter,
    heal_adapter,
    moved,
    read_report,
    same_instant,
    snapshot,
    task_for,
    world,  # noqa: F401  (fixture)
)
from tests.support.fake_google import WRITE_OPS, FakeGoogle

EDU, WEB = Source.EDUVIDUAL, Source.WEBUNTIS
STANDARD = (ASSIGNMENT, HOMEWORK, EXAM, LESSON)
DAY1, DAY2 = at(2026, 10, 14, 8, 0), at(2026, 10, 15, 8, 0)
SHIFTED = at(2026, 10, 24, 18, 0)


def _planned(report) -> set[tuple[str, Target]]:
    return {(c.uid, c.target) for c in report.changes if c.status == ChangeStatus.PLANNED}


def test_a_preview_writes_nothing_to_google_and_books_no_success(world: World):
    world.load_standard()

    out = world.preview(at=DAY1)

    g = world.google
    assert g.write_log == [] and all(g.calls[op] == 0 for op in WRITE_OPS)  # not even an attempt
    assert g.events == {} and g.tasks == [] and g.primary_events == {}
    assert out.exit_code == 0 and not out.silent and out.console_text  # something to read on the console
    assert out.report.mode == RunMode.PREVIEW and not out.report.needs_attention
    assert world.rec.opened == []  # a preview opens no browser page
    wanted = {(i.uid, Target.CALENDAR) for i in STANDARD} | {(i.uid, Target.TASKS) for i in (ASSIGNMENT, HOMEWORK, EXAM)}
    assert wanted <= _planned(out.report)  # what WOULD be done is listed ...
    assert not any(c.status == ChangeStatus.APPLIED for c in out.report.changes)  # ... and nothing is claimed as done

    with world.store() as st:  # no bookkeeping of a success
        assert st.all_items() == [] and st.last_run() is None and st.pending_count() == 0
        for src in (EDU, WEB):
            assert st.get_source_day(TODAY, src) is None and not st.day_done(TODAY, src)
            assert st.last_complete_fetch(src) is None
        assert st.report_opened_fingerprint(TODAY) is None
    assert out.report_path.name == "vorschau.html" and "nichts geschrieben" in read_report(out)
    assert list(world.paths.reports.glob("bericht-*.html")) == []  # no dated report that looks like a real run


def test_a_preview_does_not_suppress_the_writes_a_real_run_still_has_to_do(world: World):
    world.load_standard()
    world.preview(at=DAY1)
    world.preview(at=at(2026, 10, 14, 8, 10))

    out = world.start(at=at(2026, 10, 14, 8, 30))  # the automatic start of the day after the previews

    assert out.exit_code == 0 and not out.silent
    assert len(world.google.writes("insert_event")) == 4 and len(world.google.writes("insert_task")) == 3
    assert len(world.google.events) == 4 and len(world.google.tasks) == 3


def test_a_preview_after_a_real_run_shows_the_pending_change_and_leaves_state_and_google_untouched(world: World):
    world.load_standard()
    world.start(at=DAY1)
    with world.store() as st:
        items_before, run_before = st.all_items(), st.last_run()
        fetch_before = st.last_complete_fetch(EDU)
    snap = snapshot(world.google)
    world.google.reset_write_log()
    world.eduvidual.set_items([moved(ASSIGNMENT, SHIFTED)])

    out = world.preview(at=DAY2)

    assert {(ASSIGNMENT.uid, Target.CALENDAR), (ASSIGNMENT.uid, Target.TASKS)} <= _planned(out.report)
    assert any(c.uid == ASSIGNMENT.uid and c.target == Target.CALENDAR and c.action == Action.UPDATE for c in out.report.changes)
    assert world.google.write_log == [] and snapshot(world.google) == snap
    with world.store() as st:
        assert st.all_items() == items_before  # the real state still says "update pending"
        assert st.last_run() == run_before and st.last_complete_fetch(EDU) == fetch_before
        assert st.get_source_day(DAY2.date(), EDU) is None

    real = world.start(at=at(2026, 10, 15, 9, 0))  # the real run still performs the update
    assert real.exit_code == 0
    assert same_instant(event_span(event_for(world.google, ASSIGNMENT.uid))[0], SHIFTED)
    assert due_of(task_for(world.google, ASSIGNMENT.uid)).isoformat() == "2026-10-21"


def test_a_preview_is_not_held_back_by_the_day_limit(world: World):
    world.load_standard()
    world.start(at=DAY1)
    out = world.preview(at=at(2026, 10, 14, 9, 0))
    assert not out.silent and out.console_text and out.report is not None


def test_a_preview_never_blocks_a_source_nor_opens_anything_when_a_login_fails(world: World):
    world.load_standard()
    world.eduvidual.raise_next(AuthError("Anmeldung abgelehnt (Test)."))

    out = world.preview(at=DAY1)

    assert world.google.write_log == [] and world.rec.opened == []
    assert out.report is not None
    with world.store() as st:
        assert not st.is_auth_blocked(EDU)  # bookkeeping of the real runs stays untouched
    real = world.start(at=at(2026, 10, 14, 9, 0))  # and the real run reads the source again
    assert len(world.eduvidual.calls) == 2 and real.exit_code == 0
    assert len(world.google.events) == 4


def test_a_preview_never_replaces_the_report_of_a_real_run(world: World):
    world.load_standard()
    fail_adapter(world.webuntis)
    real = world.start(at=DAY1)
    real_bytes = real.report_path.read_bytes()
    latest = world.paths.reports / "latest.html"
    latest_bytes = latest.read_bytes() if latest.exists() else None
    heal_adapter(world.webuntis)

    prev = world.preview(at=at(2026, 10, 14, 9, 0))

    assert prev.report_path != real.report_path and prev.report_path.name == "vorschau.html"
    assert real.report_path.read_bytes() == real_bytes  # the failure record of the real run is still there
    assert latest_bytes is None or latest.read_bytes() == latest_bytes
    assert len(world.rec.opened) == 1  # only the failure of the real run opened a page


def test_a_preview_works_when_google_cannot_be_reached_and_still_writes_nothing(world: World):
    world.load_standard()
    world.gateway_error = GoogleAuthError("Die Google-Anmeldung ist abgelaufen (Test).")

    out = world.preview(at=DAY1)

    assert out.report is not None and world.google.write_log == []
    with world.store() as st:
        assert st.all_items() == [] and st.last_run() is None


def test_a_preview_with_an_unavailable_calendar_writes_nothing_anywhere(paths):
    g = FakeGoogle(calendar_exists=False)
    w = World(paths, google=g)
    w.load_standard()

    out = w.preview(at=DAY1)

    assert out.report is not None
    assert g.write_log == [] and g.misrouted_writes() == [] and g.primary_events == {}
