"""Tests for schulsync.state (real SQLite, files in tmp_path; no external services)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import fields
from datetime import date, datetime, timezone

import pytest

from schulsync.errors import ConfigError, TransientError
from schulsync.models import (
    EventState,
    ItemState,
    ItemStatus,
    ItemTime,
    Kind,
    OverallStatus,
    RunMode,
    Source,
    SourceItem,
    Target,
    TaskState,
)
from schulsync.state import SCHEMA_VERSION, StateStore
from schulsync.timeutil import VIENNA

T0 = datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA)
DAY = date(2026, 10, 14)


def make_item(source_id="101", source=Source.EDUVIDUAL, kind=Kind.ASSIGNMENT, **kw) -> SourceItem:
    kw.setdefault("title", "Protokoll Labor 3")
    kw.setdefault("when", ItemTime(datetime(2026, 10, 20, 23, 59, tzinfo=VIENNA)))
    return SourceItem(source=source, source_id=source_id, kind=kind, **kw)


def make_state(item: SourceItem | None = None, **kw) -> ItemState:
    item = item or make_item()
    kw.setdefault("item_hash", item.content_hash())
    kw.setdefault("cancelled", False)
    kw.setdefault("first_seen_at", T0)
    kw.setdefault("last_seen_at", T0)
    return ItemState(item=item, **kw)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.sqlite3"


@pytest.fixture
def store():
    with StateStore(":memory:") as s:
        yield s


# ------------------------------------------------------------------ schema / migration


def test_schema_created_and_migration_idempotent(db_path):
    with StateStore(db_path) as s:
        assert s.schema_version == SCHEMA_VERSION == 4  # v2 (H6): several report fingerprints per day, see test_harden_state.py
        s.upsert(make_state())
    with StateStore(db_path) as s:  # reopen: no re-migration, data intact
        assert s.schema_version == 4
        assert s.get(make_item().uid) is not None
    con = sqlite3.connect(db_path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    con.close()
    assert {"items", "intents", "source_days", "source_status", "runs", "report_opened", "meta"} <= tables
    assert mode == "wal"


def test_pragmas_applied(db_path):
    with StateStore(db_path) as s:
        assert s._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert s._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert s._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 3000


def test_newer_schema_version_is_config_error(db_path):
    StateStore(db_path).close()
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    with pytest.raises(ConfigError) as ei:
        StateStore(db_path)
    assert "neuer" in ei.value.message
    assert ei.value.hint


def test_corrupt_file_raises_config_error_and_keeps_file(db_path):
    garbage = b"this is definitely not a sqlite database " * 50
    db_path.write_bytes(garbage)
    with pytest.raises(ConfigError) as ei:
        StateStore(db_path)
    assert "Datenbank" in ei.value.message and ei.value.hint
    assert db_path.read_bytes() == garbage  # not deleted, not modified
    db_path.unlink()  # file handle was released (would fail on Windows otherwise)


def test_missing_directory_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        StateStore(tmp_path / "nope" / "state.sqlite3")


# ------------------------------------------------------------------ ItemState round trip


def full_state() -> ItemState:
    item = make_item(
        "cmid-7", Source.WEBUNTIS, Kind.EXAM,
        title="Schularbeit Mathematik – Ü", course="MAT 3AHEL", description="Stoff: Kap. 4–6\nZeile 2",
        room="R 2.07", teacher="Mag. Muster", url="https://example.invalid/x?a=1&b=2",
        status=ItemStatus.CANCELLED, id_stable=False, notes=("Endzeit unbekannt", "Hinweis ü"), subtype="Schularbeit",
        when=ItemTime(datetime(2026, 11, 3, 8, 0, tzinfo=VIENNA), datetime(2026, 11, 3, 9, 40, tzinfo=VIENNA)),
    )
    return ItemState(
        item=item, item_hash="h" * 64, cancelled=True,
        first_seen_at=datetime(2026, 10, 3, 6, 15, 30, 123456, tzinfo=VIENNA),
        last_seen_at=datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA),
        event_id="abcde0123", event_gen=3, event_state=EventState.ACTIVE, event_applied_hash="e" * 64,
        applied_cancelled=True, dirty=False, task_id="tsk_1", task_state=TaskState.REMOVED, task_applied_hash="t" * 64,
        task_base_deadline="2026-11-03T08:00:00+01:00", task_due=date(2026, 10, 27), task_removed_reason="cancelled",
        id_uncertain=True,
    )


def test_every_itemstate_field_round_trips(store):
    st = full_state()
    store.upsert(st)
    back = store.get(st.uid)
    assert back == st
    for f in fields(ItemState):
        assert getattr(back, f.name) == getattr(st, f.name), f.name
    assert back.item.when.end == st.item.when.end and back.item.notes == st.item.notes


def test_defaults_and_nones_round_trip(store):
    st = make_state()
    store.upsert(st)
    back = store.get(st.uid)
    assert back == st
    assert back.task_due is None and back.event_id is None and back.dirty is True
    assert store.get("eduvidual:assignment:unknown") is None


def test_all_day_item_round_trips(store):
    st = make_state(make_item("d1", when=ItemTime(date(2026, 10, 30))))
    store.upsert(st)
    assert store.get(st.uid).item.when.start == date(2026, 10, 30)


@pytest.mark.parametrize(
    "stamp",
    [
        datetime(2026, 7, 1, 12, 0, tzinfo=VIENNA),  # CEST +02:00
        datetime(2026, 12, 1, 12, 0, tzinfo=VIENNA),  # CET +01:00
        datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=0),  # ambiguous, first occurrence (+02:00)
        datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=1),  # ambiguous, second occurrence (+01:00)
        datetime(2026, 3, 29, 3, 30, tzinfo=VIENNA),  # first hour after spring-forward
    ],
)
def test_dst_offsets_survive(store, stamp):
    st = make_state(first_seen_at=stamp, last_seen_at=stamp)
    store.upsert(st)
    back = store.get(st.uid)
    # same tzinfo object => `==` ignores fold, so compare offset and UTC instant explicitly
    assert back.first_seen_at.utcoffset() == stamp.utcoffset()
    assert back.first_seen_at.astimezone(timezone.utc) == stamp.astimezone(timezone.utc)
    assert back.first_seen_at.isoformat() == stamp.isoformat()


def test_utc_input_returns_same_instant_in_vienna(store):
    utc = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
    store.upsert(make_state(first_seen_at=utc))
    back = store.get(make_item().uid)
    assert back.first_seen_at == utc and back.first_seen_at.utcoffset().total_seconds() == 7200


def test_naive_datetime_rejected(store):
    with pytest.raises(ValueError):
        store.upsert(make_state(first_seen_at=datetime(2026, 10, 14, 8, 0)))
    assert store.all_items() == []


def test_upsert_idempotent_and_replaces(store):
    st = make_state()
    store.upsert(st)
    store.upsert(st)
    assert len(store.all_items()) == 1
    st.dirty = False
    st.event_state = EventState.ACTIVE
    st.event_id = "evt1"
    store.upsert(st)
    back = store.get(st.uid)
    assert (back.dirty, back.event_state, back.event_id) == (False, EventState.ACTIVE, "evt1")
    assert len(store.all_items()) == 1


def test_item_queries(store):
    a = make_state(make_item("1"))
    b = make_state(make_item("2", kind=Kind.HOMEWORK, source=Source.WEBUNTIS), dirty=False)
    c = make_state(make_item("3", source=Source.WEBUNTIS, kind=Kind.EXAM))
    for s in (c, a, b):
        store.upsert(s)
    assert [s.uid for s in store.all_items()] == sorted([a.uid, b.uid, c.uid])
    assert [s.uid for s in store.items_for_source(Source.WEBUNTIS)] == sorted([b.uid, c.uid])
    assert [s.uid for s in store.items_for_source("eduvidual")] == [a.uid]
    assert [s.uid for s in store.items_by_source_kind(Source.WEBUNTIS, Kind.EXAM)] == [c.uid]
    assert store.items_by_source_kind(Source.EDUVIDUAL, Kind.EXAM) == []
    assert store.dirty_uids() == sorted([a.uid, c.uid])
    assert store.dirty_uids(Source.WEBUNTIS) == [c.uid]
    assert store.dirty_uids(Source.EDUVIDUAL) == [a.uid]


# ------------------------------------------------------------------ transactions


def test_transaction_commits(store):
    with store.transaction():
        store.upsert(make_state())
    assert store.get(make_item().uid) is not None


def test_transaction_rolls_back_on_exception(store):
    store.upsert(make_state(make_item("keep")))
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.upsert(make_state(make_item("new")))
            store.set_meta("k", "v")
            store.record_fetch(DAY, Source.EDUVIDUAL, T0, True)
            raise RuntimeError("boom")
    assert [s.item.source_id for s in store.all_items()] == ["keep"]
    assert store.get_meta("k") is None
    assert store.get_source_day(DAY, Source.EDUVIDUAL) is None
    assert store.last_complete_fetch(Source.EDUVIDUAL) is None
    assert not store._conn.in_transaction
    store.upsert(make_state(make_item("after")))  # store still usable


def test_nested_transaction_joins_outer(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            with store.transaction():
                store.upsert(make_state())
            assert store._conn.in_transaction  # inner exit did not commit
            raise RuntimeError("outer fails")
    assert store.all_items() == []


def test_intent_resolve_and_result_commit_atomically(store):
    iid = store.open_intent("eduvidual:assignment:1", Target.CALENDAR, "create", "evt1", "h")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.resolve_intent(iid, "ok")
            store.upsert(make_state())
            raise RuntimeError("crash before commit")
    assert store.get_intent(iid).state == "open" and store.all_items() == []
    with store.transaction():
        store.resolve_intent(iid, "ok")
        store.upsert(make_state())
    assert store.get_intent(iid).state == "resolved" and len(store.all_items()) == 1


# ------------------------------------------------------------------ intents


def test_intent_lifecycle(store):
    i1 = store.open_intent("eduvidual:assignment:1", Target.CALENDAR, "create", "evt1", "h1", at=T0)
    i2 = store.open_intent("webuntis:homework:9", "tasks", "delete", "tsk9", None)
    assert i1 != i2
    got = {i.id: i for i in store.open_intents()}
    assert got[i1].target is Target.CALENDAR and got[i1].action == "create" and got[i1].key == "evt1"
    assert got[i1].payload_hash == "h1" and got[i1].created_at == T0 and got[i1].state == "open"
    assert got[i1].source is Source.EDUVIDUAL and got[i2].source is Source.WEBUNTIS
    assert got[i2].target is Target.TASKS and got[i2].payload_hash is None
    assert [i.id for i in store.open_intents(Source.WEBUNTIS)] == [i2]
    assert [i.id for i in store.open_intents("eduvidual")] == [i1]

    store.resolve_intent(i1, "angelegt")
    store.abort_intent(i2, "nicht gesendet")
    assert store.open_intents() == []
    assert store.get_intent(i1).state == "resolved" and store.get_intent(i1).note == "angelegt"
    assert store.get_intent(i2).state == "aborted"
    with pytest.raises(KeyError):  # cannot close twice / unknown id
        store.resolve_intent(i1)
    with pytest.raises(KeyError):
        store.abort_intent(12345)


def test_intent_rejects_bad_action_and_target(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.open_intent("eduvidual:assignment:1", Target.TASKS, "explode", "k", None)
    with pytest.raises(ValueError):
        store.open_intent("eduvidual:assignment:1", "mailbox", "create", "k", None)
    assert store.open_intents() == []


def test_open_intent_inside_transaction_refused(store):
    with store.transaction():
        with pytest.raises(RuntimeError):
            store.open_intent("eduvidual:assignment:1", Target.TASKS, "create", "k", None)


def test_intent_durable_visible_to_second_connection_and_after_crash(db_path):
    a = StateStore(db_path)
    iid = a.open_intent("webuntis:exam:5", Target.TASKS, "create", "schulsync-id: webuntis:exam:5", "ph", at=T0)
    # visible to an independent connection immediately, while `a` is still open and has not resolved anything
    with StateStore(db_path) as b:
        seen = b.open_intents()
        assert [(i.id, i.uid, i.key, i.state) for i in seen] == [(iid, "webuntis:exam:5", "schulsync-id: webuntis:exam:5", "open")]
    # "crash": drop the connection without resolving, then restart
    a.close()
    with StateStore(db_path) as c:
        again = c.open_intents()
        assert len(again) == 1 and again[0].id == iid and again[0].created_at == T0
        c.resolve_intent(iid)
    with StateStore(db_path) as d:
        assert d.open_intents() == []


def test_second_writer_gets_clear_error_instead_of_hanging(db_path):
    a = StateStore(db_path)
    b = StateStore(db_path, busy_timeout_ms=100)
    try:
        with a.transaction():
            a.upsert(make_state())
            t = time.monotonic()
            with pytest.raises(TransientError) as ei:
                b.open_intent("eduvidual:assignment:1", Target.CALENDAR, "create", "k", None)
            assert time.monotonic() - t < 2
            assert "anderen SchulSync-Vorgang" in ei.value.message
            with pytest.raises(TransientError):
                b.upsert(make_state(make_item("2")))
            # readers are not blocked in WAL mode and see only committed data
            assert b.all_items() == []
        # lock released: b works now and `b` was left in a clean state
        b.upsert(make_state(make_item("2")))
        assert len(a.all_items()) == 2
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------------ day tracking


def test_day_gate_needs_complete_fetch_and_writes(store):
    s = Source.EDUVIDUAL
    assert store.get_source_day(DAY, s) is None and not store.day_done(DAY, s)
    store.record_fetch(DAY, s, T0, complete=True)
    assert not store.day_done(DAY, s)  # fetch complete, writes missing
    sd = store.get_source_day(DAY, s)
    assert sd.fetch_complete and sd.fetched_at == T0 and sd.writes_done_at is None and not sd.done
    store.record_writes_done(DAY, s, T0.replace(minute=5))
    assert store.day_done(DAY, s) and store.get_source_day(DAY, s).writes_done_at == T0.replace(minute=5)
    assert not store.day_done(DAY, Source.WEBUNTIS)  # per source
    assert not store.day_done(date(2026, 10, 15), s)  # per day


def test_incomplete_fetch_is_never_day_done(store):
    s = Source.WEBUNTIS
    store.record_fetch(DAY, s, T0, complete=False)
    store.record_writes_done(DAY, s, T0)
    assert not store.day_done(DAY, s)
    assert store.last_complete_fetch(s) is None


def test_writes_before_fetch_not_done(store):
    store.record_writes_done(DAY, Source.EDUVIDUAL, T0)
    assert not store.day_done(DAY, Source.EDUVIDUAL)
    store.record_fetch(DAY, Source.EDUVIDUAL, T0, complete=True)
    assert not store.day_done(DAY, Source.EDUVIDUAL)  # new fetch resets writes_done_at


def test_new_fetch_resets_writes_done(store):
    s = Source.EDUVIDUAL
    store.record_fetch(DAY, s, T0, True)
    store.record_writes_done(DAY, s, T0)
    assert store.day_done(DAY, s)
    store.record_fetch(DAY, s, T0.replace(hour=12), True)  # second run, new data, writes not yet confirmed
    assert not store.day_done(DAY, s)


def test_last_complete_fetch_any_day_and_survives_later_incomplete(store):
    s = Source.EDUVIDUAL
    assert store.last_complete_fetch(s) is None
    d1 = datetime(2026, 10, 10, 7, 0, tzinfo=VIENNA)
    d2 = datetime(2026, 10, 13, 7, 0, tzinfo=VIENNA)
    store.record_fetch(date(2026, 10, 13), s, d2, True)
    store.record_fetch(date(2026, 10, 10), s, d1, True)  # older arrives later: max stays d2
    assert store.last_complete_fetch(s) == d2
    store.record_fetch(date(2026, 10, 14), s, T0, False)  # incomplete: not a success
    assert store.last_complete_fetch(s) == d2
    assert store.last_complete_fetch(Source.WEBUNTIS) is None


def test_last_complete_fetch_compares_instants_across_dst(store):
    # 2026-10-25 02:30 fold=0 (+02:00) is EARLIER than fold=1 (+01:00), though their ISO strings sort the other way
    s = Source.WEBUNTIS
    early = datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=0)
    late = datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=1)
    store.record_fetch(date(2026, 10, 25), s, late, True)
    store.record_fetch(date(2026, 10, 24), s, early, True)
    got = store.last_complete_fetch(s)
    assert got.utcoffset() == late.utcoffset()


# ------------------------------------------------------------------ source status


def test_auth_block_set_and_clear(store):
    s = Source.WEBUNTIS
    assert not store.is_auth_blocked(s) and store.get_source_status(s) is None
    store.set_auth_blocked(s, T0, "auth")
    assert store.is_auth_blocked(s) and not store.is_auth_blocked(Source.EDUVIDUAL)
    row = store.get_source_status(s)
    assert row.auth_blocked and row.auth_blocked_at == T0 and row.auth_block_code == "auth"
    store.record_attempt(s, T0.replace(minute=1), "auth")  # attempts do not change the block
    assert store.is_auth_blocked(s)
    store.clear_auth_block(s)
    assert not store.is_auth_blocked(s)
    assert store.get_source_status(s).auth_block_code is None
    store.clear_auth_block(Source.EDUVIDUAL)  # clearing a never-blocked source is harmless
    assert not store.is_auth_blocked(Source.EDUVIDUAL)


def test_record_attempt(store):
    s = Source.EDUVIDUAL
    store.record_attempt(s, T0, "network")
    row = store.get_source_status(s)
    assert (row.last_attempt_at, row.last_error_code, row.auth_blocked) == (T0, "network", False)
    store.record_attempt(s, T0.replace(minute=9), None)
    row = store.get_source_status(s)
    assert row.last_attempt_at == T0.replace(minute=9) and row.last_error_code is None


# ------------------------------------------------------------------ runs / reports


def test_runs_and_report_opened(store):
    assert store.last_run() is None
    r1 = store.record_run(T0, T0.replace(minute=2), DAY, RunMode.AUTO, OverallStatus.PARTIAL, "fp1", "C:/b/bericht.html")
    r2 = store.record_run(T0.replace(hour=9), T0.replace(hour=9, minute=1), DAY, "manual", "success")
    assert r2 > r1
    last = store.last_run()
    assert (last.id, last.mode, last.status, last.fingerprint, last.report_path) == (
        r2, RunMode.MANUAL, OverallStatus.SUCCESS, None, None)
    assert last.started_at == T0.replace(hour=9) and last.day == DAY
    with pytest.raises(ValueError):
        store.record_run(T0, T0, DAY, "bogus", "success")

    assert store.report_opened_fingerprint(DAY) is None
    store.mark_report_opened(DAY, "fp1", T0)
    assert store.report_opened_fingerprint(DAY) == "fp1"
    store.mark_report_opened(DAY, "fp2", T0)  # replace
    assert store.report_opened_fingerprint(DAY) == "fp2"
    assert store.report_opened_fingerprint(date(2026, 10, 15)) is None


# ------------------------------------------------------------------ pending_count


def test_pending_count_dirty_plus_intents_per_source(store):
    assert store.pending_count() == 0
    store.upsert(make_state(make_item("1")))  # eduvidual dirty
    store.upsert(make_state(make_item("2"), dirty=False))  # eduvidual clean
    store.upsert(make_state(make_item("3", Source.WEBUNTIS, Kind.EXAM)))  # webuntis dirty
    assert store.pending_count() == 2
    assert store.pending_count(Source.EDUVIDUAL) == 1 and store.pending_count("webuntis") == 1
    i = store.open_intent("webuntis:exam:3", Target.TASKS, "create", "k", None)
    store.open_intent("webuntis:exam:77", Target.CALENDAR, "create", "k2", None)  # uid without stored item
    assert store.pending_count() == 4
    assert store.pending_count(Source.WEBUNTIS) == 3 and store.pending_count(Source.EDUVIDUAL) == 1
    store.resolve_intent(i)
    assert store.pending_count(Source.WEBUNTIS) == 2
    clean = store.get("webuntis:exam:3")
    clean.dirty = False
    store.upsert(clean)
    assert store.pending_count(Source.WEBUNTIS) == 1 and store.pending_count() == 2


# ------------------------------------------------------------------ copy_to_memory


def test_copy_to_memory_isolated_both_directions(db_path):
    with StateStore(db_path) as orig:
        orig.upsert(make_state(make_item("1")))
        orig.open_intent("eduvidual:assignment:1", Target.CALENDAR, "create", "e1", None)
        orig.record_fetch(DAY, Source.EDUVIDUAL, T0, True)
        with orig.copy_to_memory() as copy:
            assert copy.path == ":memory:" and copy.schema_version == SCHEMA_VERSION
            assert copy.all_items() == orig.all_items()
            assert len(copy.open_intents()) == 1
            assert copy.last_complete_fetch(Source.EDUVIDUAL) == T0
            # write to the copy -> original untouched
            st = copy.get("eduvidual:assignment:1")
            st.dirty = False
            copy.upsert(st)
            copy.upsert(make_state(make_item("copy-only")))
            copy.resolve_intent(copy.open_intents()[0].id)
            copy.record_writes_done(DAY, Source.EDUVIDUAL, T0)
            assert orig.get("eduvidual:assignment:1").dirty is True
            assert orig.get("eduvidual:assignment:copy-only") is None
            assert len(orig.open_intents()) == 1 and not orig.day_done(DAY, Source.EDUVIDUAL)
            # write to the original -> copy untouched
            orig.upsert(make_state(make_item("orig-only")))
            orig.record_run(T0, T0, DAY, RunMode.AUTO, OverallStatus.SUCCESS)
            assert copy.get("eduvidual:assignment:orig-only") is None and copy.last_run() is None
        # copy closed; original still fine and persistent
        assert orig.get("eduvidual:assignment:orig-only") is not None
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    con.close()


def test_copy_of_memory_store(store):
    store.upsert(make_state())
    with store.copy_to_memory() as c:
        assert c.get(make_item().uid) == store.get(make_item().uid)


# ------------------------------------------------------------------ prune_old


def test_prune_old_keeps_items_and_recent_rows(store):
    old, recent = date(2026, 9, 1), date(2026, 10, 14)
    store.upsert(make_state(make_item("1")))
    store.open_intent("eduvidual:assignment:1", Target.CALENDAR, "create", "k", None)
    for d in (old, recent):
        store.record_fetch(d, Source.EDUVIDUAL, datetime.combine(d, T0.timetz()), True)
        store.record_writes_done(d, Source.EDUVIDUAL, T0)
        store.record_run(T0, T0, d, RunMode.AUTO, OverallStatus.SUCCESS)
        store.mark_report_opened(d, "fp", T0)
    store.record_attempt(Source.EDUVIDUAL, T0, None)

    deleted = store.prune_old(date(2026, 10, 1))
    assert deleted == 3
    assert not store.day_done(old, Source.EDUVIDUAL) and store.day_done(recent, Source.EDUVIDUAL)
    assert store.report_opened_fingerprint(old) is None and store.report_opened_fingerprint(recent) == "fp"
    assert store.last_run().day == recent
    assert len(store.all_items()) == 1 and len(store.open_intents()) == 1  # never items / intents
    assert store.last_complete_fetch(Source.EDUVIDUAL) is not None
    assert store.get_source_status(Source.EDUVIDUAL) is not None
    assert store.prune_old(date(2026, 10, 14)) == 0  # boundary: day == before is kept
    assert store.day_done(recent, Source.EDUVIDUAL)
    store.prune_old(date(2027, 1, 1))
    assert len(store.all_items()) == 1 and store.last_run() is None
    assert store.last_complete_fetch(Source.EDUVIDUAL) is not None
