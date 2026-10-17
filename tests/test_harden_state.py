"""H6: public StateStore methods replace the engine's `store._conn` access; schema v2 (several report fingerprints per
day) with a migration from a real v1 database file."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from schulsync import state as state_module
from schulsync.models import EventState, Target, TaskState, canonical_json
from schulsync.state import SCHEMA_VERSION, StateStore
from tests.test_state import DAY, T0, make_item, make_state

SRC = Path(__file__).resolve().parent.parent / "src" / "schulsync"


@pytest.fixture
def store():
    with StateStore(":memory:") as s:
        yield s


# --------------------------------------------------------------------------- the engine no longer reaches into the connection


def test_sync_py_uses_only_the_public_store_api():
    text = (SRC / "sync.py").read_text(encoding="utf-8")
    assert not re.search(r"\._conn\b", text) and "noqa: SLF001" not in text


def test_runner_has_no_report_opened_json_workaround_any_more():
    text = (SRC / "runner.py").read_text(encoding="utf-8")
    assert "report_opened.json" not in text and "_OPENED_FILE" not in text and "_remember_opened" not in text


# --------------------------------------------------------------------------- delete_item


def test_delete_item_removes_only_that_row(store):
    a, b = make_state(make_item("1")), make_state(make_item("2"))
    store.upsert(a)
    store.upsert(b)
    assert store.delete_item(a.uid) is True
    assert store.get(a.uid) is None and store.get(b.uid) is not None
    assert store.delete_item(a.uid) is False  # unknown uid: nothing happens, no error


def test_delete_item_joins_an_outer_transaction_and_rolls_back_with_it(store):
    a = make_state()
    store.upsert(a)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.delete_item(a.uid)
            raise RuntimeError("boom")
    assert store.get(a.uid) is not None


# --------------------------------------------------------------------------- event_id_owner


def test_event_id_owner_names_another_item_but_never_the_asker(store):
    a = make_state(make_item("1"), event_id="ssabc", event_state=EventState.ACTIVE)
    b = make_state(make_item("2"))
    store.upsert(a)
    store.upsert(b)
    assert store.event_id_owner("ssabc", b.uid) == a.uid
    assert store.event_id_owner("ssabc", a.uid) is None  # the owner itself does not count
    assert store.event_id_owner("ssunbekannt", b.uid) is None


# --------------------------------------------------------------------------- forget_applied


def test_forget_applied_calendar_clears_only_the_event_hash(store):
    st = make_state(
        event_id="e1", event_state=EventState.ACTIVE, event_applied_hash="h-event",
        task_id="t1", task_state=TaskState.OPEN, task_applied_hash="h-task", task_base_deadline="2026-10-20", task_due=date(2026, 10, 17),
    )
    store.upsert(st)
    store.forget_applied(st.uid, Target.CALENDAR)
    got = store.get(st.uid)
    assert got.event_applied_hash is None
    assert got.task_applied_hash == "h-task" and got.task_base_deadline == "2026-10-20"


def test_forget_applied_tasks_clears_the_task_hash_and_optionally_sets_the_base_deadline(store):
    st = make_state(event_applied_hash="h-event", task_applied_hash="h-task", task_base_deadline="2026-10-20", task_state=TaskState.OPEN, task_id="t1")
    store.upsert(st)
    store.forget_applied(st.uid, Target.TASKS)
    got = store.get(st.uid)
    assert got.task_applied_hash is None and got.task_base_deadline == "2026-10-20" and got.event_applied_hash == "h-event"
    store.forget_applied(st.uid, Target.TASKS, base_deadline="unknown")
    assert store.get(st.uid).task_base_deadline == "unknown"


# --------------------------------------------------------------------------- report_opened: several fingerprints per day


def test_several_fingerprints_per_day_are_remembered(store):
    assert store.report_opened_fingerprints(DAY) == set()
    store.mark_report_opened(DAY, "A", T0)
    store.mark_report_opened(DAY, "B", T0)
    store.mark_report_opened(DAY, "A", T0)  # shown again: still one entry, but now the newest
    assert store.report_opened_fingerprints(DAY) == {"A", "B"}
    assert store.report_opened_fingerprint(DAY) == "A"  # old single-value API: the latest one
    store.mark_report_opened(DAY, "C", T0)
    assert store.report_opened_fingerprint(DAY) == "C"
    assert store.report_opened_fingerprints(date(2026, 10, 15)) == set()


def test_prune_old_removes_old_report_fingerprints_of_every_kind(store):
    old = DAY - timedelta(days=40)
    store.mark_report_opened(old, "A", T0)
    store.mark_report_opened(old, "B", T0)
    store.mark_report_opened(DAY, "C", T0)
    store.prune_old(DAY - timedelta(days=30))
    assert store.report_opened_fingerprints(old) == set() and store.report_opened_fingerprints(DAY) == {"C"}


# --------------------------------------------------------------------------- migration v1 -> v2


def _build_v1_database(path: Path) -> str:
    """A database exactly as the PREVIOUS code left it: the v1 DDL (state._V1 is the frozen, append-only record of it),
    user_version 1, a day with ONE report_opened row (day was the primary key) and a stored item. Returns the item uid."""
    item = make_item("v1-item")
    con = sqlite3.connect(path)
    for stmt in state_module._V1:
        con.execute(stmt)
    con.execute("PRAGMA user_version = 1")
    con.execute("INSERT INTO report_opened (day, fingerprint, opened_at) VALUES (?,?,?)", ("2026-10-14", "alt-fp", T0.isoformat()))
    con.execute("INSERT INTO report_opened (day, fingerprint, opened_at) VALUES (?,?,?)", ("2026-10-13", "gestern-fp", T0.isoformat()))
    con.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    con.execute(
        "INSERT INTO items (uid, source, kind, source_id, item_json, item_hash, cancelled, first_seen_at, last_seen_at, event_gen,"
        " event_state, applied_cancelled, dirty, task_state, id_uncertain, updated_at) VALUES (?,?,?,?,?,?,0,?,?,0,'none',0,1,'none',0,?)",
        (item.uid, item.source.value, item.kind.value, item.source_id, canonical_json(item.to_dict()), item.content_hash(),
         T0.isoformat(), T0.isoformat(), T0.isoformat()),
    )
    con.commit()
    con.close()
    return item.uid


def test_schema_version_is_3():
    assert SCHEMA_VERSION == 3  # v3: items.task_attempt (Eduvidual resubmission)


def test_v1_database_file_is_migrated_and_keeps_its_rows(tmp_path):
    db = tmp_path / "state.sqlite3"
    uid = _build_v1_database(db)
    with StateStore(db) as s:
        assert s.schema_version == 3
        assert s.get(uid) is not None and s.get_meta("k") == "v"  # nothing else was touched
        assert s.report_opened_fingerprint(date(2026, 10, 14)) == "alt-fp"  # old row semantics survive
        assert s.report_opened_fingerprints(date(2026, 10, 13)) == {"gestern-fp"}
        s.mark_report_opened(date(2026, 10, 14), "zweiter-fp", T0)  # what v1 could not hold
        assert s.report_opened_fingerprints(date(2026, 10, 14)) == {"alt-fp", "zweiter-fp"}
    with StateStore(db) as s:  # reopening is a no-op
        assert s.schema_version == 3 and s.report_opened_fingerprints(date(2026, 10, 14)) == {"alt-fp", "zweiter-fp"}
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "report_opened" in tables and not any("v2" in t or "old" in t for t in tables)  # no leftovers of the migration


def test_v1_migration_survives_two_processes_opening_the_file_at_once(tmp_path):
    db = tmp_path / "state.sqlite3"
    _build_v1_database(db)
    first, second = StateStore(db), StateStore(db)  # the second one finds a current schema and must not migrate again
    try:
        assert first.schema_version == second.schema_version == 3
        assert second.report_opened_fingerprint(date(2026, 10, 14)) == "alt-fp"
    finally:
        first.close()
        second.close()


def test_a_fresh_database_has_the_v2_layout_with_a_composite_key(tmp_path):
    StateStore(tmp_path / "s.sqlite3").close()
    con = sqlite3.connect(tmp_path / "s.sqlite3")
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name = 'report_opened'").fetchone()[0]
    con.close()
    assert re.search(r"UNIQUE\s*\(\s*day\s*,\s*fingerprint\s*\)", ddl)


def test_copy_to_memory_keeps_the_report_fingerprints(store):
    store.mark_report_opened(DAY, "A", T0)
    store.mark_report_opened(DAY, "B", T0)
    copy = store.copy_to_memory()
    try:
        assert copy.report_opened_fingerprints(DAY) == {"A", "B"}
    finally:
        copy.close()
