"""Eduvidual-Aufgaben sind nur Google-Tasks (Datum = Datum von Frist minus 6 Stunden), keine Kalendertermine. Bereits angelegte Termine werden entfernt."""

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
    assert r.assignment_lead_hours == 6 and r.assignment_calendar is False


def test_an_assignment_creates_only_a_task_dated_six_hours_before_the_deadline():
    rig = Rig(cfg=NEW)
    eng = rig.sync(_a())
    assert rig.google.events == {} and len(rig.google.open_tasks) == 1
    (task,) = rig.google.open_tasks
    assert task["due"].startswith("2026-10-21")  # deadline 21.10. 23:59 minus 6 hours = 17:59 the same day
    assert "23:59" in task["notes"] and task["title"].startswith("Bearbeiten: ")
    assert eng.counts_for(_a().source).calendar.skipped == 0  # not a "skipped entry", simply no calendar for this kind


def test_homework_and_exams_keep_their_calendar_entries():
    rig = Rig(cfg=NEW)
    rig.sync(S.homework(), S.exam())
    assert len(rig.google.events) == 2 and len(rig.google.open_tasks) == 2


def test_an_existing_calendar_entry_of_an_assignment_is_deleted_once_and_the_task_moves_to_six_hours_before():
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
    assert task["due"].startswith("2026-10-21")
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


def test_the_task_date_is_the_date_of_deadline_minus_six_hours():
    from schulsync import rules

    cfg = RulesConfig()
    d = lambda *a: S.at(*a)
    assert rules.task_start_date(S.assignment(due=d(2026, 10, 22, 18, 0)), cfg).isoformat() == "2026-10-22"  # 12:00 same day
    assert rules.task_start_date(S.assignment(due=d(2026, 10, 22, 5, 0)), cfg).isoformat() == "2026-10-21"  # 23:00 the day before
    assert rules.task_start_date(S.assignment(due=d(2026, 10, 22, 6, 0)), cfg).isoformat() == "2026-10-22"  # exactly 00:00
    from datetime import date
    assert rules.task_start_date(S.assignment(due=date(2026, 10, 22)), cfg).isoformat() == "2026-10-22"  # date-only counts as 23:59
    legacy = RulesConfig(**S.LEGACY_RULES)
    assert rules.task_start_date(S.assignment(due=d(2026, 10, 22, 18, 0)), legacy).isoformat() == "2026-10-19"  # lead days still work
    # the 25.10.2026 clock change: 6 REAL hours before 25.10. 05:00 (CET, the night has 25 hours) is 00:00 CEST on the 25th
    assert rules.task_start_date(S.assignment(due=d(2026, 10, 25, 5, 0)), cfg).isoformat() == "2026-10-25"


def test_a_task_the_user_ticked_off_and_then_reopened_follows_the_school_again():
    rig = Rig(cfg=NEW)
    a = _a()
    rig.sync(a)
    (task,) = rig.google.open_tasks
    rig.google.user_complete_task(task["id"])
    rig.sync(replace(a, title="Laborprotokoll 2 (Text geändert)"))  # a change makes the run look: it notices the tick
    assert rig.st(a).task_state == TaskState.DONE
    rig.google.user_reopen_task(task["id"])
    moved = replace(a, when=replace(a.when, start=S.at(2026, 10, 30, 12, 0)))
    rig.sync(moved)  # the school moved the deadline: the re-opened task follows it
    (t,) = rig.google.open_tasks
    assert t["id"] == task["id"] and t["due"].startswith("2026-10-30") and len(rig.google.tasks) == 1
    assert rig.st(a).task_state == TaskState.OPEN


def test_a_task_that_stays_ticked_off_is_never_touched_even_when_the_deadline_moves():
    rig = Rig(cfg=NEW)
    a = _a()
    rig.sync(a)
    (task,) = rig.google.open_tasks
    rig.google.user_complete_task(task["id"])
    rig.sync(replace(a, when=replace(a.when, start=S.at(2026, 10, 30, 12, 0))))
    assert rig.google.open_tasks == [] and len(rig.google.tasks) == 1 and rig.st(a).task_state == TaskState.DONE


def test_the_v5_migration_rechecks_every_assignment_task(tmp_path):
    rig = Rig(cfg=OLD)
    a = _a()
    rig.sync(a)
    db = tmp_path / "s.sqlite3"
    s2 = StateStore(db)
    st = rig.st(a)
    st.task_state = TaskState.DONE
    s2.upsert(st)
    s2.close()
    con = sqlite3.connect(db)
    con.execute("UPDATE items SET dirty = 0, task_base_deadline = '2026-10-21T23:59:00+02:00'")
    con.execute("PRAGMA user_version = 4")
    con.commit()
    con.close()
    with StateStore(db) as s3:
        got = s3.get(a.uid)
        assert got.dirty and got.task_base_deadline == "unknown"
