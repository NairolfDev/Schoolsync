"""Lernplan: zusaetzliche Google-Tasks (keine Kalendertermine) vor Pruefungen, abgeleitet aus den Pruefungs-Eintraegen."""

from __future__ import annotations

from datetime import timedelta

from schulsync.config import Config, RulesConfig
from schulsync.models import ItemStatus
from tests.support import samples as S
from tests.support.engine_harness import NOW, Rig


def cfg(x: int = 2, start: int = 14) -> Config:
    return Config(rules=RulesConfig(window_days=60, study_every_days=x, study_start_days=start))


def exam_in(days: int, eid: str = "30412", **kw):
    d = (NOW + timedelta(days=days)).replace(hour=8)
    return S.exam(eid=eid, start=d, end=d.replace(hour=9, minute=40), **kw)


def study(rig: Rig, title: str = "Schularbeit Angewandte Mathematik") -> dict[int, dict]:
    """open study tasks of one exam by d"""
    pre = f"Lernen: {title} (noch "
    return {int(t["title"][len(pre):].split()[0]): t for t in rig.google.open_tasks if t["title"].startswith(pre)}


def test_off_by_default():
    rig = Rig(cfg=Config())
    rig.sync(exam_in(20))
    assert len(rig.google.open_tasks) == 1 and len(rig.google.events) == 1


def test_sessions_dates_titles_notes_and_no_events():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))  # exam 3.11.2026
    got = study(rig)
    assert sorted(got) == [2, 4, 6, 8, 10, 12, 14]  # 7 = exam_lead_days: the normal "Lernen:" task covers it
    assert {d: t["due"][:10] for d, t in got.items()} == {
        14: "2026-10-20", 12: "2026-10-22", 10: "2026-10-24", 8: "2026-10-26", 6: "2026-10-28", 4: "2026-10-30", 2: "2026-11-01"
    }
    assert got[6]["title"] == "Lernen: Schularbeit Angewandte Mathematik (noch 6 Tage)"
    notes = got[6]["notes"]
    assert "03.11.2026" in notes and "08:00" in notes and "Lernplan: noch 6 Tage bis zur Prüfung" in notes
    assert "schulsync-id: webuntis:study:exam:30412#lern6" in notes
    assert len(rig.google.events) == 1  # only the exam itself
    assert len(rig.google.open_tasks) == 1 + 7  # + the normal task at exam_lead_days


def test_one_day_wording_and_today_is_created():
    rig = Rig(cfg=cfg(x=2, start=5))
    rig.sync(exam_in(5))  # d = 5 (dated today), 3, 1
    got = study(rig)
    assert sorted(got) == [1, 3, 5] and got[5]["due"].startswith("2026-10-14")
    assert got[1]["title"].endswith("(noch 1 Tag)")


def test_past_sessions_are_not_created_and_not_counted():
    rig = Rig(cfg=cfg())
    eng = rig.sync(exam_in(5))  # exam 19.10.: d=14..6 would be dated before today
    assert sorted(study(rig)) == [2, 4]
    c = eng.counts_for(S.exam().source)
    assert c.tasks.skipped == 0 and c.calendar.skipped == 0


def test_exam_move_updates_same_tasks_and_rerun_is_quiet():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    n = len(rig.google.open_tasks)
    rig.sync(exam_in(23))
    assert len(rig.google.open_tasks) == n
    got = study(rig)
    assert got[14]["due"].startswith("2026-10-23") and "06.11.2026" in got[14]["notes"]
    w = len(rig.google.write_log)
    rig.sync(exam_in(23))
    assert len(rig.google.write_log) == w


def test_cancelled_exam_removes_open_sessions_but_keeps_done_ones():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    done = study(rig)[12]
    rig.google.user_complete_task(done["id"])
    rig.sync(exam_in(20))  # noticed as done
    rig.sync(exam_in(20, status=ItemStatus.CANCELLED))
    assert study(rig) == {}
    assert rig.google.task(done["id"])["status"] == "completed"


def test_completed_session_stays_done_after_exam_move():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    done = study(rig)[12]
    rig.google.user_complete_task(done["id"])
    rig.sync(exam_in(20))
    rig.sync(exam_in(23))
    rec = rig.google.task(done["id"])
    assert rec["status"] == "completed" and rec["due"].startswith("2026-10-22")


def test_two_exams_do_not_mix():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20), exam_in(25, eid="2", title="Test Physik"))
    assert sorted(study(rig)) == [2, 4, 6, 8, 10, 12, 14]
    assert sorted(study(rig, "Test Physik")) == [2, 4, 6, 8, 10, 12, 14]
    assert study(rig, "Test Physik")[14]["due"].startswith("2026-10-25")
    assert study(rig)[14]["due"].startswith("2026-10-20")


def test_changing_every_days_removes_obsolete_future_open_tasks():
    rig = Rig(cfg=cfg(2))
    rig.sync(exam_in(20))
    before = {t["id"] for t in rig.google.open_tasks}
    rig.cfg = cfg(3)
    rig.sync(exam_in(20))
    after = {t["id"] for t in rig.google.open_tasks}
    assert not before <= after
    assert sorted(study(rig)) == [2, 5, 8, 11, 14]


def test_dst_change_due_date():
    rig = Rig(cfg=cfg())
    rig.sync(S.exam(start=S.at(2026, 10, 26, 8, 0), end=S.at(2026, 10, 26, 9, 40)))
    assert study(rig)[2]["due"].startswith("2026-10-24")


def test_incomplete_exam_category_derives_nothing():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    n = len(rig.google.open_tasks)
    rig.sync()  # exams absent: no cancellation by absence
    assert len(rig.google.open_tasks) == n


def test_disabling_plan_removes_future_sessions_but_preserves_done_and_past():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    tasks = study(rig)
    done = tasks[12]
    past = tasks[14]
    rig.google.user_complete_task(done["id"])
    rig.cfg = cfg(0)
    rig.sync(exam_in(20), now=NOW + timedelta(days=8))
    assert rig.google.task(done["id"])["status"] == "completed"
    assert rig.google.task(past["id"]).get("deleted") is not True
    assert rig.google.task(tasks[2]["id"]).get("deleted") is True


def test_incomplete_exam_fetch_does_not_remove_obsolete_sessions():
    from schulsync.errors import NetworkError
    from schulsync.models import Category, Source

    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    before = {t["id"] for t in rig.google.open_tasks}
    rig.cfg = cfg(0)
    rig.adapters[Source.WEBUNTIS].fail_category(Category.EXAMS, NetworkError("offline"))
    rig.sync(exam_in(20))
    assert before == {t["id"] for t in rig.google.open_tasks}


def test_removed_marker_prevents_automatic_cleanup():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    rec = study(rig)[2]
    rig.google.patch_task(rec["id"], {"notes": "Personal note"})
    rig.cfg = cfg(0)
    rig.sync(exam_in(20))
    assert rig.google.task(rec["id"]).get("deleted") is not True


def test_withdrawal_after_disabling_plan_removes_future_sessions():
    from schulsync.models import Source

    rig = Rig(cfg=cfg())
    exam = exam_in(20)
    rig.sync(exam)
    rig.cfg = cfg(0)
    rig.adapters[Source.WEBUNTIS].set_withdrawn({exam.uid: "Prüfung abgesagt"})
    rig.sync()
    assert study(rig) == {}


def test_cleanup_keeps_task_with_user_changed_past_due_date():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    task = study(rig)[2]
    rig.google.patch_task(task["id"], {"due": "2026-10-01T00:00:00.000Z"})
    rig.cfg = cfg(0)
    rig.sync(exam_in(20))
    assert rig.google.task(task["id"]).get("deleted") is not True


def test_reenabled_plan_recreates_future_sessions():
    rig = Rig(cfg=cfg())
    rig.sync(exam_in(20))
    rig.cfg = cfg(0)
    rig.sync(exam_in(20))
    assert study(rig) == {}
    rig.cfg = cfg()
    rig.sync(exam_in(20))
    assert sorted(study(rig)) == [2, 4, 6, 8, 10, 12, 14]


def test_moved_exam_with_unstable_id_reuses_existing_study_tasks():
    from dataclasses import replace
    rig = Rig(cfg=cfg())
    rig.sync(replace(exam_in(20, eid="old"), id_stable=False))
    before = {d: task["id"] for d, task in study(rig).items()}
    rig.sync(replace(exam_in(23, eid="new"), id_stable=False))
    assert len(rig.google.open_tasks) == 8
    assert {d: task["id"] for d, task in study(rig).items()} == before
    assert study(rig)[14]["due"].startswith("2026-10-23")


def test_unstable_exam_id_change_and_cancellation_removes_existing_sessions():
    from dataclasses import replace
    rig = Rig(cfg=cfg())
    rig.sync(replace(exam_in(20, eid="old"), id_stable=False))
    rig.sync(replace(exam_in(23, eid="new", status=ItemStatus.CANCELLED), id_stable=False))
    assert study(rig) == {}


def test_unstable_exam_adoption_does_not_relabel_or_delete_task_without_old_marker():
    from dataclasses import replace
    rig = Rig(cfg=cfg())
    rig.sync(replace(exam_in(20, eid="old"), id_stable=False))
    task = study(rig)[2]
    rig.google.patch_task(task["id"], {"notes": "user notes only"})
    rig.sync(replace(exam_in(23, eid="new"), id_stable=False))
    assert rig.google.task(task["id"])["notes"] == "user notes only"
    rig.cfg = cfg(0)
    rig.sync(replace(exam_in(23, eid="new"), id_stable=False))
    assert rig.google.task(task["id"]).get("deleted") is not True
    assert rig.google.task(task["id"])["notes"] == "user notes only"
