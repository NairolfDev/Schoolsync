"""Acceptance tests for ANFORDERUNGEN section 1 (Windows start and daily flow).

Covers section 10, bullet 2: several Windows logons on the same day and a new attempt after a failure
(each `world.start()` is one start of the program: fresh Deps over the same data directory).

Readings chosen where the text leaves room (listed in the hand-over of the acceptance module):
- "Tageserfolge getrennt je Quelle": a source that already succeeded today is not fetched again by a later
  automatic start of the same day; only the failed/open source is retried.
- The day gate applies to automatic starts. A manual start (`run` without `--auto`) is never gated.
"""

from __future__ import annotations

from datetime import date

import pytest

from schulsync.errors import AuthError, BadRequestError, ErrorInfo, NetworkError
from schulsync.lock import ProcessLock
from schulsync.models import OverallStatus, RunMode, Source, SourceStatus
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    HOMEWORK,
    TODAY,
    World,
    at,
    fail_adapter,
    read_report,
    snapshot,
    world,  # noqa: F401  (fixture)
)

EDU, WEB = Source.EDUVIDUAL, Source.WEBUNTIS


def _fetches(w: World) -> tuple[int, int]:
    return len(w.eduvidual.calls), len(w.webuntis.calls)


# --------------------------------------------------------------------------- one successful run per day


def test_first_logon_syncs_and_every_further_logon_of_the_same_time_slot_ends_silently(world: World):
    world.load_standard()
    first = world.start(at=at(2026, 10, 14, 7, 55))
    assert first.exit_code == 0 and not first.silent
    assert len(world.google.events) == 4 and len(world.google.tasks) == 3

    fetches, snap, checks = _fetches(world), snapshot(world.google), world.rec.network_checks
    world.google.reset_write_log()
    for hh, mm in ((9, 10), (17, 59)):  # further logons before the 18:00 slot
        again = world.start(at=at(2026, 10, 14, hh, mm))
        assert again.silent and again.exit_code == 0
        assert again.console_text == "" and again.report is None
    assert _fetches(world) == fetches  # not even a read of the school platforms
    assert world.google.write_log == [] and snapshot(world.google) == snap
    assert world.rec.network_checks == checks  # the gate comes before waiting for the network
    assert world.rec.opened == [] and world.rec.sleeps == []


def test_the_fixed_runs_at_6_and_18_fetch_again_and_update_a_moved_deadline_without_duplicates(world: World):
    world.load_standard()
    world.start(at=at(2026, 10, 14, 7, 55))
    events, tasks = len(world.google.events), len(world.google.tasks)
    before = _fetches(world)

    out = world.start(at=at(2026, 10, 14, 18, 0))  # scheduled 18:00 run, same day
    assert not out.silent and out.exit_code == 0
    assert _fetches(world) == (before[0] + 1, before[1] + 1)
    assert (len(world.google.events), len(world.google.tasks)) == (events, tasks)  # nothing duplicated

    assert world.start(at=at(2026, 10, 14, 18, 40)).silent  # logon after the 18:00 run: nothing to do
    assert not world.start(at=at(2026, 10, 15, 6, 0)).silent  # 06:00 next morning runs again


@pytest.mark.parametrize(
    "second_day",
    [at(2026, 10, 15, 7, 50), at(2026, 10, 19, 7, 50)],
    ids=["next day (Thursday)", "Monday after a weekend without logon"],
)
def test_every_weekday_including_monday_gets_its_own_run(world: World, second_day):
    world.load_standard()
    world.start(at=at(2026, 10, 14, 7, 55))
    before = _fetches(world)

    out = world.start(at=second_day)

    assert not out.silent and out.exit_code == 0
    assert _fetches(world) == (before[0] + 1, before[1] + 1)
    with world.store() as st:
        assert st.last_run().day == second_day.date()


@pytest.mark.parametrize(
    ("first", "second", "second_is_silent"),
    [
        (at(2026, 10, 14, 23, 50), at(2026, 10, 15, 0, 10), False),  # same UTC day, different Vienna day
        (at(2026, 10, 14, 0, 30), at(2026, 10, 14, 5, 50), True),  # different UTC days, same Vienna day
        (at(2026, 10, 25, 0, 30), at(2026, 10, 25, 5, 50), True),  # 25 hour day (summer time ends)
        (at(2026, 3, 29, 0, 30), at(2026, 3, 29, 5, 50), True),  # 23 hour day (summer time starts)
        (at(2026, 3, 28, 23, 50), at(2026, 3, 29, 0, 10), False),
    ],
    ids=["utc-same-vienna-differs", "vienna-same-utc-differs", "autumn-25h-day", "spring-23h-day", "spring-midnight"],
)
def test_the_calendar_day_in_europe_vienna_decides(world: World, first, second, second_is_silent):
    world.start(at=first)
    out = world.start(at=second)
    assert out.silent is second_is_silent


def test_a_manual_force_start_bypasses_the_day_limit_without_duplicates(world: World):
    world.load_standard()
    world.start(at=at(2026, 10, 14, 7, 55))
    before, snap = _fetches(world), snapshot(world.google)
    world.google.reset_write_log()

    forced = world.start(at=at(2026, 10, 14, 9, 0), force=True)

    assert not forced.silent and forced.exit_code == 0
    assert _fetches(world) == (before[0] + 1, before[1] + 1)
    assert world.google.write_log == [] and snapshot(world.google) == snap  # nothing new, nothing doubled


def test_a_manual_start_is_not_held_back_by_the_day_limit(world: World):
    """`schulsync run` (no --auto) after the automatic run: reading chosen = the gate is for automatic starts."""
    world.load_standard()
    world.start(at=at(2026, 10, 14, 7, 55))
    before = _fetches(world)

    manual = world.start(RunMode.MANUAL, at=at(2026, 10, 14, 9, 0))

    assert not manual.silent and manual.exit_code == 0
    assert _fetches(world) == (before[0] + 1, before[1] + 1)


def test_a_disabled_source_is_never_contacted(world: World):
    world.load_standard()
    world.configure(webuntis={"enabled": False})
    out = world.start()
    assert out.exit_code == 0 and world.webuntis.calls == []
    assert len(world.google.events) == 1  # only the Eduvidual assignment
    assert world.start(at=at(2026, 10, 14, 12, 0)).silent  # the enabled source alone completes the day


# --------------------------------------------------------------------------- failure, retry, per-source success


def test_failed_source_is_retried_at_the_next_logon_and_the_finished_source_is_not_read_again(world: World):
    world.load_standard()
    attempts = world.config.retry.max_attempts
    fail_adapter(world.webuntis, NetworkError("Keine Verbindung (Test)."), times=attempts)

    first = world.start(at=at(2026, 10, 14, 7, 55))

    assert first.exit_code == 1 and first.report.status == OverallStatus.PARTIAL
    assert first.report.sources[EDU].status == SourceStatus.SUCCESS
    assert first.report.sources[WEB].status == SourceStatus.FAILED
    assert _fetches(world) == (1, attempts)
    assert len(world.google.events) == 1 and len(world.google.tasks) == 1  # the Eduvidual part was taken over
    assert world.rec.opened == [first.report_path]
    today = TODAY
    with world.store() as st:  # day success is kept per source: one done, one not
        assert st.day_done(today, EDU) and not st.day_done(today, WEB)

    second = world.start(at=at(2026, 10, 14, 12, 0))  # the PC is switched on again at noon

    assert second.exit_code == 0 and not second.silent
    assert second.report.sources[WEB].status == SourceStatus.SUCCESS
    assert _fetches(world) == (1, attempts + 1)  # Eduvidual was NOT fetched again
    assert len(world.google.events) == 4 and len(world.google.tasks) == 3
    assert len(world.rec.opened) == 1  # success opens no browser page
    with world.store() as st:
        assert st.day_done(today, EDU) and st.day_done(today, WEB)
    assert world.start(at=at(2026, 10, 14, 17, 0)).silent  # now the day is complete


def test_a_permanently_rejected_google_write_fails_that_change_without_retry_and_is_retried_at_the_next_start(world: World):
    world.eduvidual.set_items([ASSIGNMENT])
    world.webuntis.set_items([HOMEWORK])
    world.google.fail_next("insert_event", BadRequestError("Anfrage abgelehnt (Test)."))

    first = world.start(at=at(2026, 10, 14, 7, 55))

    assert first.exit_code == 1 and first.report.status == OverallStatus.PARTIAL
    assert world.google.calls["insert_event"] == 2  # each event tried once: a permanent 400 is not retried
    assert len(world.google.events) == 1
    assert len(world.rec.opened) == 1

    second = world.start(at=at(2026, 10, 14, 13, 0))

    assert second.exit_code == 0
    assert len(world.google.events) == 2 and len(world.google.tasks) == 2
    assert world.google.calls["insert_event"] == 3  # only the failed one was attempted again
    assert world.start(at=at(2026, 10, 14, 17, 30)).silent


# --------------------------------------------------------------------------- no internet right after logon, limits


def test_retries_are_few_bounded_and_wait_longer_each_time(world: World):
    world.load_standard()
    world.configure(retry={"delays_seconds": (5.0, 20.0, 45.0)})
    fail_adapter(world.eduvidual, NetworkError("Zeitüberschreitung (Test)."), times=2)

    out = world.start()

    assert out.exit_code == 0
    assert len(world.eduvidual.calls) == 3  # two failures, third attempt succeeds
    assert world.rec.sleeps == [5.0, 20.0]  # increasing waiting time, taken from the configuration
    assert world.rec.opened == []


def test_when_all_attempts_fail_the_error_is_reported_once_after_the_retries_not_per_attempt(world: World):
    world.load_standard()
    world.configure(retry={"delays_seconds": (5.0, 20.0, 45.0)})
    attempts = world.config.retry.max_attempts
    fail_adapter(world.eduvidual)
    fail_adapter(world.webuntis)

    out = world.start()

    assert out.exit_code == 1 and out.report.status == OverallStatus.FAILED
    assert _fetches(world) == (attempts, attempts)  # exactly max_attempts per source, never endless
    assert sorted(world.rec.sleeps) == sorted([5.0, 20.0, 45.0] * 2)
    assert world.rec.opened == [out.report_path]  # one browser page for the whole run
    assert world.google.write_log == []
    assert "Internetverbindung prüfen" in read_report(out)  # sensible next step for a connection problem


def test_the_run_has_a_hard_time_limit(world: World):
    world.load_standard()
    world.configure(
        eduvidual={"enabled": False},
        retry={"delays_seconds": (15.0, 60.0, 180.0), "run_timeout_seconds": 20.0},
    )
    fail_adapter(world.webuntis)
    started = world.clock.monotonic()

    out = world.start()

    assert out.exit_code == 1
    assert len(world.webuntis.calls) < world.config.retry.max_attempts
    assert world.clock.monotonic() - started <= 20.0 + 1e-6
    assert len(world.rec.opened) == 1


def test_missing_internet_right_after_logon_is_waited_for_and_then_the_run_proceeds(world: World):
    world.load_standard()
    world.rec.network_script = [False, False, False]  # then the connection is there

    out = world.start()

    assert out.exit_code == 0
    assert world.rec.network_checks >= 4
    assert 0 < sum(world.rec.sleeps) <= world.config.retry.network_wait_seconds
    # one pause per negative check, and the pauses GROW ("zunehmende Wartezeit"), they are not one constant poll
    assert len(world.rec.sleeps) == 3 and world.rec.sleeps == sorted(world.rec.sleeps) and len(set(world.rec.sleeps)) == 3
    assert len(world.google.events) == 4  # fetched only after the network was up
    assert world.rec.opened == []


def test_waiting_for_the_network_is_limited_and_is_followed_by_an_attempt_anyway(world: World):
    world.load_standard()
    world.configure(retry={"network_wait_seconds": 30.0})
    world.rec.network_default = False  # the check never succeeds

    out = world.start()

    # the whole budget is used (a 1-second wait would also be "<= 30") and never exceeded, then the attempt follows anyway
    assert 25.0 <= sum(world.rec.sleeps) <= 30.0
    assert _fetches(world) == (1, 1)  # tried although the check said no
    assert out.exit_code == 0 and len(world.google.events) == 4


# --------------------------------------------------------------------------- permanent login failures


@pytest.mark.parametrize("how", ["exception", "login_error"])
def test_rejected_credentials_are_not_retried_and_block_automatic_runs_until_a_manual_start_works(world: World, how: str):
    world.load_standard()
    if how == "exception":
        world.eduvidual.raise_next(AuthError("Anmeldung abgelehnt (Test)."))
    else:
        world.eduvidual.login_fails(ErrorInfo("auth", "Anmeldung abgelehnt (Test)."))

    first = world.start(at=at(2026, 10, 14, 7, 55))

    assert first.exit_code == 1 and first.report.sources[EDU].status == SourceStatus.FAILED
    assert first.report.sources[WEB].status == SourceStatus.SUCCESS  # the other source is unaffected
    assert len(world.eduvidual.calls) == 1 and world.rec.sleeps == []  # no retry, no waiting
    assert "Anmeldedaten der Schulplattform prüfen" in read_report(first)
    with world.store() as st:
        assert st.is_auth_blocked(EDU) and not st.is_auth_blocked(WEB)

    world.start(at=at(2026, 10, 14, 12, 0))  # later logon the same day
    world.start(at=at(2026, 10, 15, 7, 50))  # and the next day
    assert len(world.eduvidual.calls) == 1  # never retried automatically while blocked

    world.eduvidual.login_ok()  # password fixed
    manual = world.start(RunMode.MANUAL, at=at(2026, 10, 15, 9, 0))
    assert len(world.eduvidual.calls) == 2 and manual.exit_code == 0
    with world.store() as st:
        assert not st.is_auth_blocked(EDU)
    assert len(world.google.events) == 4

    world.start(at=at(2026, 10, 16, 7, 50))
    assert len(world.eduvidual.calls) == 3  # automatic runs read the source again


# --------------------------------------------------------------------------- days without logon, process lock


def test_after_days_off_the_current_data_is_checked_once_without_fictitious_catch_up_days(world: World):
    world.load_standard()
    world.start(at=at(2026, 10, 14, 7, 55))
    before = _fetches(world)

    out = world.start(at=at(2026, 10, 19, 7, 50))  # Thu-Sun without a logon

    assert out.exit_code == 0
    assert _fetches(world) == (before[0] + 1, before[1] + 1)  # one check of the current data
    with world.store() as st:
        for missed in (15, 16, 17, 18):
            assert st.get_source_day(date(2026, 10, missed), EDU) is None
            assert st.get_source_day(date(2026, 10, missed), WEB) is None
        assert st.last_run().day == date(2026, 10, 19)


def test_a_second_instance_is_kept_out_by_the_process_lock_and_the_lock_is_released_afterwards(world: World):
    world.load_standard()
    lock = ProcessLock(world.paths.lock)
    assert lock.acquire()
    try:
        auto = world.start()
        assert auto.silent and auto.exit_code == 0 and auto.report is None
        manual = world.start(RunMode.MANUAL, force=True)
        assert manual.report is None and "läuft bereits" in manual.console_text.lower()
    finally:
        lock.release()
    assert world.eduvidual.calls == [] and world.webuntis.calls == []
    assert world.google.write_log == [] and world.rec.opened == []

    out = world.start()  # the other instance is gone: normal run

    assert out.exit_code == 0 and len(world.google.events) == 4
    again = ProcessLock(world.paths.lock)  # and the finished run released its own lock
    assert again.acquire()
    again.release()


def test_the_lock_is_released_after_a_failed_run(world: World):
    world.load_standard()
    fail_adapter(world.eduvidual)
    fail_adapter(world.webuntis)
    assert world.start().exit_code == 1
    lock = ProcessLock(world.paths.lock)
    assert lock.acquire()
    lock.release()
