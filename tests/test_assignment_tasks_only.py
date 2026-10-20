"""Eduvidual-Aufgaben sind nur Google-Tasks (Datum = Frist minus 1 Tag), keine Kalendertermine. Bereits angelegte Termine werden entfernt."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from schulsync.config import Config, RulesConfig
from schulsync.models import EventState, TaskState
from schulsync.state import StateStore
from tests.support import samples as S
from tests.support.engine_harness import Rig

NEW = Config()  # the new defaults: tasks only, 1 day
OLD = Config(rules=RulesConfig(**S.LEGACY_RULES))


def _a(**kw):
    return S.assignment(cmid="1", due=S.at(2026, 10, 21, 23, 59), **kw)


def test_defaults_are_tasks_only_one_day_before():
    r = RulesConfig()
    assert r.assignment_lead_days == 1 and r.assignment_calendar is False


def test_an_assignment_creates_only_a_task_dated_one_day_before_the_deadline():
    rig = Rig(cfg=NEW)
    eng = rig.sync(_a())
    assert rig.google.events == {} and len(rig.google.open_tasks) == 1
    (task,) = rig.google.open_tasks
    assert task["due"].startswith("2026-10-20")  # deadline 21.10. 23:59 minus 1 calendar day
    assert "23:59" in task["notes"] and task["title"].startswith("Bearbeiten: ")
    assert eng.counts_for(_a().source).calendar.skipped == 0  # not a "skipped entry", simply no calendar for this kind


def test_homework_and_exams_keep_their_calendar_entries():
    rig = Rig(cfg=NEW)
    rig.sync(S.homework(), S.exam())
    assert len(rig.google.events) == 2 and len(rig.google.open_tasks) == 2


def test_an_existing_calendar_entry_of_an_assignment_is_deleted_once_and_the_task_moves_to_one_day_before():
    rig = Rig(cfg=OLD)  # before the change: event + task 3 days before
    a = _a()
    rig.sync(a)
    assert len(rig.google.events) == 1
    (task,) = rig.google.open_tasks
    assert task["due"].startswith("2026-10-18")
    # upgrade: schema v4 marks assignments for a re-check; the new rules apply
    st = rig.st(a)
    st.dirty, st.task_base_deadline = True, "unknown"  # what the v4 migration does
    rig.store.upsert(st)
    rig.cfg = NEW
    rig.sync(a)
    assert rig.google.events == {} or all(e.get("status") == "cancelled" for e in rig.google.events.values())
    assert [w for w in rig.google.write_log if w[0] == "delete_event"] != []
    (task,) = rig.google.open_tasks
    assert task["due"].startswith("2026-10-20")
    assert rig.st(a).event_state == EventState.NONE and rig.st(a).task_state == TaskState.OPEN
    n = len(rig.google.write_log)
    rig.sync(a)  # stable afterwards: no more writes, no event comes back
    assert len(rig.google.write_log) == n and rig.google.events == {}


def test_a_foreign_event_with_our_id_is_never_deleted():
    rig = Rig(cfg=OLD)
    a = _a()
    rig.sync(a)
    eid = rig.st(a).event_id
    rec = rig.google._store().live[eid]
    rec["extendedProperties"]["private"]["schulsync_uid"] = "somebody:else"  # not ours any more
    st = rig.st(a)
    st.dirty = True
    rig.store.upsert(st)
    rig.cfg = NEW
    rig.sync(a)
    assert eid in rig.google._store().live and [w for w in rig.google.write_log if w[0] == "delete_event"] == []


def test_the_v4_migration_marks_assignments_for_a_recheck(tmp_path):
    rig = Rig(cfg=OLD)
    a = _a()
    rig.sync(a)
    st = rig.st(a)
    st.dirty = False
    rig.store.upsert(st)
    db = tmp_path / "s.sqlite3"
    s2 = StateStore(db)
    s2.upsert(rig.st(a))
    s2.close()
    con = sqlite3.connect(db)
    con.execute("UPDATE items SET dirty = 0, task_base_deadline = '2026-10-21T23:59:00+02:00'")
    con.execute("PRAGMA user_version = 3")
    con.commit()
    con.close()
    with StateStore(db) as s3:  # opening runs migration v4
        got = s3.get(a.uid)
        assert got.dirty and got.task_base_deadline == "unknown"
