"""Adversarial review tests for schulsync.state (each test demonstrates one confirmed defect)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from schulsync.errors import SchulSyncError
from schulsync.models import ItemState, ItemTime, Kind, OverallStatus, RunMode, Source, SourceItem
from schulsync.state import StateStore
from schulsync.timeutil import VIENNA

T0 = datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA)
DAY = date(2026, 10, 14)


def _state(source_id="1", description=None) -> ItemState:
    item = SourceItem(
        source=Source.EDUVIDUAL, source_id=source_id, kind=Kind.ASSIGNMENT, title="Protokoll",
        when=ItemTime(datetime(2026, 10, 20, 23, 59, tzinfo=VIENNA)), description=description,
    )
    return ItemState(item=item, item_hash=item.content_hash(), cancelled=False, first_seen_at=T0, last_seen_at=T0)


# --------------------------------------------------------------------------- datetime is a date subclass


def test_record_run_with_datetime_day_must_not_poison_last_run():
    """`day: date` accepts a datetime (a date subclass; e.g. the runner passes `now` instead of `today_vienna(now)`).
    It is stored via isoformat() as '2026-10-14T08:00:00+02:00'; from then on last_run() raises ValueError for every
    caller (status/report) until prune_old removes the row. A write must either be rejected or stay readable."""
    store = StateStore(":memory:")
    try:
        store.record_run(T0, T0, T0, RunMode.AUTO, OverallStatus.SUCCESS)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return  # rejected at the boundary: fine
    assert store.last_run() is not None  # must not raise


def test_prune_old_with_datetime_cutoff_must_keep_rows_of_that_day():
    """prune_old(before) deletes `day < before`. With a datetime cutoff (e.g. now - timedelta(days=n)) the cutoff string
    is '2026-10-14T08:00:00+02:00', which sorts AFTER the plain day '2026-10-14', so the rows OF that very day are deleted
    although the calendar day is not before the cutoff day."""
    store = StateStore(":memory:")
    store.record_fetch(DAY, Source.EDUVIDUAL, T0, True)
    store.record_writes_done(DAY, Source.EDUVIDUAL, T0)
    try:
        store.prune_old(T0)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return
    assert store.day_done(DAY, Source.EDUVIDUAL), "today's completed day was pruned -> the daily gate forgets the success"


def test_mark_report_opened_with_datetime_day_must_be_found_again_by_date():
    """Same class: mark_report_opened(day=datetime) stores a key that report_opened_fingerprint(date) never finds, so the
    tab-flood protection (ANFORDERUNGEN 8) silently stops working and a tab is opened on every run."""
    store = StateStore(":memory:")
    try:
        store.mark_report_opened(T0, "fp1", T0)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return
    assert store.report_opened_fingerprint(DAY) == "fp1"


# --------------------------------------------------------------------------- opening needs the write lock


def test_opening_a_second_connection_must_not_need_the_write_lock(tmp_path):
    """DESIGN 7: 'status'/'report' style commands may run while a sync holds a write transaction. `_migrate()` takes
    BEGIN IMMEDIATE on EVERY open, even when the schema is current, so a plain read-only open fails with TransientError
    while another connection merely holds an open transaction (the docstring claims readers are never blocked)."""
    path = tmp_path / "state.sqlite3"
    a = StateStore(path)
    try:
        with a.transaction():
            a.set_meta("k", "v")
            b = StateStore(path, busy_timeout_ms=100)  # raises TransientError (schema is already current)
            try:
                assert b.get_meta("k") is None  # snapshot of committed data
            finally:
                b.close()
    finally:
        a.close()


# --------------------------------------------------------------------------- error translation after open


def test_disk_full_is_translated_to_a_german_schulsync_error(tmp_path):
    """Everything surfaced to the user must be a SchulSyncError with a German message (ANFORDERUNGEN 8). A real
    'database or disk is full' (SQLITE_FULL, simulated with max_page_count) escapes as a raw English
    sqlite3.OperationalError from upsert()/transaction() instead."""
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        store._conn.execute("PRAGMA max_page_count = 6")
        with pytest.raises(Exception) as ei:
            for i in range(200):
                store.upsert(_state(str(i), description="x" * 2000))
        assert isinstance(ei.value, SchulSyncError), f"raw {type(ei.value).__module__}.{type(ei.value).__name__}: {ei.value}"
    finally:
        store.close()


def test_disk_full_inside_outer_transaction_is_not_masked_by_savepoint_error(tmp_path):
    """SQLITE_FULL makes SQLite roll back the WHOLE transaction by itself. The nested block then runs
    `ROLLBACK TO sp1` against a savepoint that no longer exists and raises 'no such savepoint: sp1', replacing the real
    cause (disk full) with a meaningless error in the report. The primary error must survive."""
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        store._conn.execute("PRAGMA max_page_count = 6")
        with pytest.raises(Exception) as ei:
            with store.transaction():
                store.set_meta("a", "1")
                for i in range(200):
                    store.upsert(_state(str(i), description="x" * 2000))
        assert "no such savepoint" not in str(ei.value), f"real cause lost: {ei.value!r}"
    finally:
        store.close()
