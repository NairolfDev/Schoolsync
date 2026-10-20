"""Engine level tests of the hardening work.

H2  target identity: the ids of calendar and task list are remembered in the state meta at the first successful target
    check; if the configured ids change, the Google link state of every item is reset (nothing is "user deleted").
H5  restore: an explicit German result per uid (restored / nothing to restore / not restored: cancelled or expired).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from schulsync.config import Config, GoogleConfig, RulesConfig
from schulsync.google_types import make_event_id
from schulsync.models import Action, EventState, ItemStatus, ItemTime, Severity, TaskState
from schulsync.sync import SyncEngine
from tests.support import samples as S
from tests.support.engine_harness import EDU, NOW, WU, Rig
from tests.support.fake_google import FakeGoogle


def cfg_with(calendar_id: str = "cal-1", tasklist_id: str = "list-1") -> Config:
    return Config(google=GoogleConfig(calendar_id=calendar_id, tasklist_id=tasklist_id), rules=RulesConfig(**S.LEGACY_RULES))


def eid(item, gen: int = 0) -> str:
    return make_event_id(item.uid, gen)


def codes(eng: SyncEngine) -> list[str]:
    return [n.code for n in eng.notices]


def engine_for(rig: Rig, cfg: Config, google: FakeGoogle) -> SyncEngine:
    return SyncEngine(rig.store, cfg, lambda: google)


# ====================================================================================== H2 target identity


class TestTargetIdentity:
    def test_the_ids_are_remembered_at_the_first_successful_target_check(self):
        rig = Rig(cfg=cfg_with())
        assert rig.store.get_meta("target.calendar_id") is None
        rig.sync(S.assignment(), write=False)
        assert rig.store.get_meta("target.calendar_id") is None  # no Google contact yet: nothing is known to work
        rig.write_again()
        assert rig.store.get_meta("target.calendar_id") == "cal-1" and rig.store.get_meta("target.tasklist_id") == "list-1"

    def test_a_failed_target_check_remembers_nothing(self):
        rig = Rig(cfg=cfg_with(), calendar_exists=False)
        rig.sync(S.assignment())
        assert rig.store.get_meta("target.calendar_id") is None

    def test_the_same_ids_never_reset_anything(self):
        rig = Rig(cfg=cfg_with())
        a, x = S.assignment(), S.exam()
        rig.sync(a, x)
        before = {s.uid: s for s in rig.store.all_items()}
        eng = rig.engine()
        assert eng.reset_targets_if_changed(NOW) is False
        rig.google.reset_write_log()
        eng2 = rig.sync(a, x)  # another full run with the same targets
        assert rig.google.write_log == [] and "targets_changed" not in codes(eng) + codes(eng2)
        assert {s.uid: s for s in rig.store.all_items()} == before
        assert rig.st(a).event_gen == 0 and rig.st(a).event_state == EventState.ACTIVE

    def test_unconfigured_ids_are_ignored_like_before(self):
        rig = Rig(cfg=Config(rules=RulesConfig(**S.LEGACY_RULES)))  # the engine tests of old: ids empty in the configuration
        rig.sync(S.assignment())
        assert rig.store.get_meta("target.calendar_id") is None
        assert rig.engine().reset_targets_if_changed(NOW) is False

    def test_a_database_from_before_this_change_learns_the_ids_at_the_first_run_without_a_reset(self):
        """No stored ids but items already linked to Google: the old targets are unknowable, so nothing is reset; the
        configuration the user has been running with is taken as the ids (even if nothing has to be written)."""
        rig = Rig(cfg=Config(rules=RulesConfig(**S.LEGACY_RULES)))
        a = S.assignment()
        rig.sync(a)
        linked = rig.st(a)
        assert rig.store.get_meta("target.calendar_id") is None
        eng = SyncEngine(rig.store, cfg_with(), lambda: rig.google)
        assert eng.reset_targets_if_changed(NOW) is False
        assert rig.st(a) == linked and "targets_changed" not in codes(eng)
        assert rig.store.get_meta("target.calendar_id") == "cal-1" and rig.store.get_meta("target.tasklist_id") == "list-1"
        # ... and a change after that is detected
        later = SyncEngine(rig.store, cfg_with("cal-2", "list-2"), lambda: FakeGoogle())
        assert later.reset_targets_if_changed(NOW) is True

    def test_changed_ids_reset_the_google_link_of_every_item_and_recreate_everything_in_the_new_targets(self):
        old = Rig(cfg=cfg_with("cal-1", "list-1"))
        a, x = S.assignment(), S.exam()
        old.sync(a, x)
        assert len(old.google.events) == 2 and len(old.google.open_tasks) == 2
        old_writes = list(old.google.write_log)

        # the user re-created the calendar and the list, ran 'schulsync setup' and got new ids; Google is a fresh world
        new_google = FakeGoogle()
        new_cfg = cfg_with("cal-2", "list-2")
        eng = engine_for(old, new_cfg, new_google)
        assert eng.reset_targets_if_changed(NOW) is True
        for item in (a, x):
            st = old.st(item)
            assert st.event_id is None and st.event_state == EventState.NONE and st.event_gen == 1
            assert st.event_applied_hash is None and st.applied_cancelled is False
            assert st.task_id is None and st.task_state == TaskState.NONE and st.task_applied_hash is None
            assert st.task_base_deadline is None and st.task_due is None and st.task_removed_reason is None
            assert st.dirty is True
        (n,) = [n for n in eng.notices if n.code == "targets_changed"]
        assert n.severity == Severity.WARNING and "Kalender" in n.message and "neuen Zielen" in n.message
        assert old.store.get_meta("target.calendar_id") == "cal-2" and old.store.get_meta("target.tasklist_id") == "list-2"

        eng.write_phase([EDU, WU], NOW)
        assert set(new_google.events) == {eid(a, 1), eid(x, 1)} and len(new_google.open_tasks) == 2
        assert old.google.write_log == old_writes  # the old targets are never touched again
        assert all(c.action == Action.CREATE for c in eng.changes)  # created, not "restored": nobody deleted anything
        assert old.st(a).event_state == EventState.ACTIVE and old.st(a).task_state == TaskState.OPEN

    def test_the_reset_happens_once_not_on_every_run(self):
        old = Rig(cfg=cfg_with("cal-1", "list-1"))
        a = S.assignment()
        old.sync(a)
        new_google = FakeGoogle()
        new_cfg = cfg_with("cal-2", "list-2")
        eng = engine_for(old, new_cfg, new_google)
        assert eng.reset_targets_if_changed(NOW) is True
        eng.write_phase([EDU, WU], NOW)
        again = engine_for(old, new_cfg, new_google)
        assert again.reset_targets_if_changed(NOW) is False
        assert old.st(a).event_gen == 1  # not bumped a second time

    def test_a_changed_tasklist_alone_is_a_change_too(self):
        old = Rig(cfg=cfg_with("cal-1", "list-1"))
        a = S.assignment()
        old.sync(a)
        eng = engine_for(old, cfg_with("cal-1", "list-9"), FakeGoogle())
        assert eng.reset_targets_if_changed(NOW) is True and old.st(a).event_gen == 1

    def test_an_item_that_is_fully_past_is_reset_but_simply_skipped_by_the_planner(self):
        old = Rig(cfg=cfg_with("cal-1", "list-1"))
        a = S.assignment()  # due 21.10. 23:59
        old.sync(a)
        later = NOW + timedelta(days=10)  # the deadline is over
        new_google = FakeGoogle()
        eng = engine_for(old, cfg_with("cal-2", "list-2"), new_google)
        assert eng.reset_targets_if_changed(later) is True
        assert old.st(a).dirty is True
        eng.write_phase([EDU, WU], later)
        assert new_google.events == {} and new_google.open_tasks == [] and new_google.write_log == []
        assert eng.counts_for(EDU).calendar.skipped_reasons == {"expired": 1}
        assert old.st(a).dirty is False  # settled, nothing pending forever

    def test_a_completed_task_stays_completed_it_is_not_recreated_as_an_open_copy(self):
        """ANFORDERUNGEN 6: 'Einmal erledigte Tasks bleiben erledigt ... keine neue offene Kopie'."""
        old = Rig(cfg=cfg_with("cal-1", "list-1"))
        a = S.assignment()
        old.sync(a)
        task_id = old.google.open_tasks[0]["id"]
        old.google.user_complete_task(task_id)
        old.sync(replace(a, title="Laborprotokoll 2 (neu)"))  # the engine notices that the user ticked it off
        assert old.st(a).task_state == TaskState.DONE
        new_google = FakeGoogle()
        eng = engine_for(old, cfg_with("cal-2", "list-2"), new_google)
        assert eng.reset_targets_if_changed(NOW) is True
        eng.write_phase([EDU, WU], NOW)
        assert old.st(a).task_state == TaskState.DONE and new_google.open_tasks == []
        assert len(new_google.events) == 1  # the calendar entry does move to the new calendar

    def test_user_deleted_markers_belong_to_the_old_targets_and_are_cleared(self):
        old = Rig(cfg=cfg_with("cal-1", "list-1"))
        a = S.assignment()
        old.sync(a)
        old.google.user_delete_event(eid(a))
        old.sync(replace(a, when=ItemTime(S.at(2026, 10, 28, 23, 59))))
        assert old.st(a).event_state == EventState.USER_DELETED
        new_google = FakeGoogle()
        eng = engine_for(old, cfg_with("cal-2", "list-2"), new_google)
        eng.reset_targets_if_changed(NOW)
        eng.write_phase([EDU, WU], NOW)
        assert old.st(a).event_state == EventState.ACTIVE and len(new_google.events) == 1

    def test_open_write_intents_of_the_old_targets_are_aborted(self):
        old = Rig(cfg=cfg_with("cal-1", "list-1"))
        a = S.assignment()
        old.sync(a, write=False)
        old.store.open_intent(a.uid, "calendar", "create", eid(a), "h")
        assert len(old.store.open_intents()) == 1
        eng = engine_for(old, cfg_with("cal-2", "list-2"), FakeGoogle())
        # nothing was ever checked against Google, so no ids are stored: no reset, the intent is untouched
        assert eng.reset_targets_if_changed(NOW) is False and len(old.store.open_intents()) == 1
        old.write_again()  # a successful check stores the ids (and reconciles the intent)
        old.store.open_intent(a.uid, "tasks", "create", "schulsync-id: x", "h;;")
        eng2 = engine_for(old, cfg_with("cal-3", "list-3"), FakeGoogle())
        assert eng2.reset_targets_if_changed(NOW) is True
        assert old.store.open_intents() == []

    def test_a_preview_engine_on_a_copy_leaves_the_real_store_alone(self):
        old = Rig(cfg=cfg_with("cal-1", "list-1"))
        a = S.assignment()
        old.sync(a)
        copy = old.store.copy_to_memory()
        try:
            eng = SyncEngine(copy, cfg_with("cal-2", "list-2"), lambda: FakeGoogle(), preview=True)
            assert eng.reset_targets_if_changed(NOW) is True
            assert copy.get(a.uid).event_gen == 1
        finally:
            copy.close()
        assert old.st(a).event_gen == 0 and old.store.get_meta("target.calendar_id") == "cal-1"


# ====================================================================================== H5 restore


def delete_event_then_move(rig: Rig, item, *when):
    """The user deletes the Google event; the next sync (a moved deadline) records it as USER_DELETED."""
    rig.google.user_delete_event(eid(item))
    return rig.sync(replace(item, when=ItemTime(S.at(*when))))


class TestRestoreResult:
    def test_a_deleted_entry_is_restored_with_a_german_per_uid_line(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        delete_event_then_move(rig, a, 2026, 10, 28, 23, 59)
        eng = rig.engine()
        res = eng.restore([a.uid], NOW)
        eng.write_phase([EDU], NOW)
        assert res.restored == [a.uid] and res.not_found == [] and res.blocked == {}
        (entry,) = res.entries
        assert (entry.uid, entry.outcome, entry.reason) == (a.uid, "restored", None)
        assert "wiederhergestellt" in entry.message.lower() and entry.title == a.title
        (n,) = [n for n in eng.notices if n.uid == a.uid and n.code == "restore_done"]
        assert n.severity == Severity.INFO

    def test_a_cancelled_item_is_not_restored_and_says_why_without_touching_the_state(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        delete_event_then_move(rig, a, 2026, 10, 28, 23, 59)
        rig.sync(replace(a, when=ItemTime(S.at(2026, 10, 28, 23, 59)), status=ItemStatus.CANCELLED))
        st = rig.st(a)
        assert st.event_state == EventState.USER_DELETED and st.cancelled
        gen = st.event_gen
        eng = rig.engine()
        res = eng.restore([a.uid], NOW)
        assert res.restored == [] and res.not_found == [] and res.blocked == {a.uid: "cancelled"} and res.sources == set()
        (entry,) = res.entries
        assert entry.outcome == "blocked" and entry.reason == "cancelled"
        assert "abgesagt" in entry.message and "Nicht wiederhergestellt" in entry.message
        assert rig.st(a).event_gen == gen and rig.st(a).event_state == EventState.USER_DELETED  # nothing bumped
        eng.write_phase([EDU], NOW)
        assert rig.google.events == {} and eng.changes == []
        (n,) = [n for n in eng.notices if n.code == "restore_blocked"]
        assert n.severity == Severity.WARNING and n.uid == a.uid

    def test_an_expired_item_is_not_restored_and_says_why(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        delete_event_then_move(rig, a, 2026, 10, 18, 23, 59)
        later = NOW + timedelta(days=7)  # 21.10.: the (moved) deadline of 18.10. is over
        eng = rig.engine()
        res = eng.restore(None, later)
        assert res.restored == [] and res.blocked == {a.uid: "expired"}
        assert "vorbei" in res.entries[0].message and rig.st(a).event_gen == 0

    def test_not_deleted_and_unknown_uids_are_reported_as_nothing_to_restore(self):
        rig = Rig()
        a = S.assignment()
        rig.sync(a)
        eng = rig.engine()
        res = eng.restore([a.uid, "eduvidual:assignment:assign:nope"], NOW)
        assert res.restored == [] and res.not_found == [a.uid, "eduvidual:assignment:assign:nope"]
        by_uid = {e.uid: e for e in res.entries}
        assert by_uid[a.uid].outcome == "nothing" and by_uid[a.uid].reason == "not_deleted"
        assert "nicht" in by_uid[a.uid].message and "gelöscht" in by_uid[a.uid].message
        assert by_uid["eduvidual:assignment:assign:nope"].reason == "unknown" and "unbekannt" in by_uid["eduvidual:assignment:assign:nope"].message.lower()
        assert rig.st(a).event_gen == 0

    def test_restore_all_mixes_restored_and_blocked_entries(self):
        rig = Rig()
        a, b = S.assignment(), S.assignment(cmid="40214", title="Laborprotokoll 3")
        rig.sync(a, b)
        delete_event_then_move(rig, a, 2026, 10, 28, 23, 59)
        rig.google.user_delete_event(eid(b))
        rig.sync(replace(a, when=ItemTime(S.at(2026, 10, 28, 23, 59))), replace(b, when=ItemTime(S.at(2026, 10, 29, 23, 59)), status=ItemStatus.CANCELLED))
        eng = rig.engine()
        res = eng.restore(None, NOW)
        assert res.restored == [a.uid] and res.blocked == {b.uid: "cancelled"} and res.sources == {EDU}
        assert [e.outcome for e in res.entries] == ["restored", "blocked"]  # uid order
        eng.write_phase([EDU], NOW)
        assert list(rig.google.events) == [eid(a, 1)]
