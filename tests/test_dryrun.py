"""Tests for DryRunGateway: reads delegate, writes never reach the inner gateway, overlay stays consistent."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from schulsync.dryrun import DryRunGateway, PlannedWrite
from schulsync.errors import (
    ConflictError,
    GoogleAuthError,
    NetworkError,
    TargetUnavailableError,
)
from schulsync.google_bodies import (
    event_insert_body,
    event_patch_body,
    task_insert_body,
    task_patch_body,
)
from schulsync.google_types import TASK_MARKER_PREFIX, make_event_id
from schulsync.models import DesiredEvent, DesiredTask
from schulsync.timeutil import VIENNA
from tests.support.fake_google import FakeGoogle

UID = "eduvidual:assignment:100"
UID2 = "webuntis:exam:200"
EID, EID2 = make_event_id(UID), make_event_id(UID2)
WRITE_NAMES = {"insert_event", "patch_event", "insert_task", "patch_task", "delete_task"}


def props(uid: str) -> dict[str, str]:
    return {"schulsync": "1", "schulsync_uid": uid, "schulsync_source": "x", "schulsync_kind": "assignment"}


def dev(uid=UID, **kw) -> DesiredEvent:
    base = dict(
        summary="Abgabe: Projektarbeit",
        managed_text="m",
        start=datetime(2026, 10, 16, 23, 59, tzinfo=VIENNA),
        end=datetime(2026, 10, 17, 0, 0, tzinfo=VIENNA),
        location=None,
        popup_minutes=1440,
        private_props=props(uid),
    )
    base.update(kw)
    return DesiredEvent(**base)


def ins_body(uid=UID, eid=EID, **kw):
    return event_insert_body(dev(uid, **kw), event_id=eid, description="Beschreibung")


def task_body(title="Bearbeiten: Projektarbeit", uid=UID, due=date(2026, 10, 13)):
    return task_insert_body(DesiredTask(title, "m", due), notes=f"Notiz\n{TASK_MARKER_PREFIX}{uid}")


class ReadOnlyProxy:
    """Wraps a gateway and fails the test if any write method is even called."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        assert name not in WRITE_NAMES, f"dry run reached inner.{name}()"
        return getattr(self._inner, name)


@pytest.fixture
def populated() -> FakeGoogle:
    """Google state before the preview: one live event, one deleted event, an open and a completed task."""
    g = FakeGoogle()
    g.insert_event(ins_body())
    g.insert_event(ins_body(UID2, EID2))
    g.user_delete_event(EID2)
    g.insert_task(task_body("Bearbeiten: A", "u:1:a"))
    g.insert_task(task_body("Lernen: B", "u:1:b"))
    g.user_complete_task("task-0002")
    g.reset_write_log()
    g.reset_counters()
    return g


def snapshot(g: FakeGoogle):
    return g.events, g.deleted_event_ids, g.tasks, list(g.write_log)


# --------------------------------------------------------------------------- never writes


def test_full_simulated_run_never_reaches_inner(populated):
    before = snapshot(populated)
    d = DryRunGateway(ReadOnlyProxy(populated))
    d.check_targets()
    d.insert_event(ins_body("eduvidual:assignment:300", make_event_id("eduvidual:assignment:300")))
    d.patch_event(EID, event_patch_body(dev(summary="Abgesagt: x", popup_minutes=None, cancelled=True), description="d"))
    t = d.insert_task(task_body("Bearbeiten: neu", "u:3:c"))
    d.patch_task(t["id"], task_patch_body(DesiredTask("Bearbeiten: neu2", "m", date(2026, 10, 14)), notes="n", include_due=True))
    d.patch_task("task-0001", {"title": "Geändert"})
    d.delete_task("task-0001")
    d.delete_task("not-there")
    assert snapshot(populated) == before
    assert populated.write_log == [] and populated.misrouted_writes() == []
    assert not WRITE_NAMES & set(populated.calls)  # not even attempted
    assert [w.op for w in d.planned_writes] == [
        "insert_event", "patch_event", "insert_task", "patch_task", "patch_task", "delete_task", "delete_task",
    ]


def test_write_errors_of_inner_cannot_leak_in():
    g = FakeGoogle()
    for op in WRITE_NAMES:
        g.fail_always(op, GoogleAuthError("abgelaufen"))
    d = DryRunGateway(g)
    d.insert_event(ins_body())  # would raise on a real gateway
    d.insert_task(task_body())
    assert len(d.planned_writes) == 2 and g.write_log == []


def test_planned_writes_shape_and_snapshot_semantics():
    d = DryRunGateway(None)
    body = ins_body()
    d.insert_event(body)
    body["summary"] = "mutated afterwards"
    t = d.insert_task(task_body())
    d.delete_task(t["id"])
    first, second, third = d.planned_writes
    assert isinstance(first, PlannedWrite)
    op, id_, b = first
    assert (op, id_) == ("insert_event", EID) and b["summary"] == "Abgabe: Projektarbeit"
    assert (second.op, second.id) == ("insert_task", None) and second.body["title"] == "Bearbeiten: Projektarbeit"
    assert (third.op, third.id, third.body) == ("delete_task", t["id"], None)


# --------------------------------------------------------------------------- overlay consistency: events


def test_insert_event_visible_to_later_reads_but_not_in_inner(populated):
    d = DryRunGateway(populated)
    uid = "eduvidual:assignment:300"
    eid = make_event_id(uid)
    out = d.insert_event(ins_body(uid, eid))
    assert out["id"] == eid and out["status"] == "confirmed"
    assert d.get_event(eid)["summary"] == "Abgabe: Projektarbeit"
    assert [e["id"] for e in d.find_events_by_uid(uid)] == [eid]
    assert populated.get_event(eid) is None


def test_second_insert_of_same_id_conflicts_like_the_real_api(populated):
    d = DryRunGateway(populated)
    uid = "eduvidual:assignment:300"
    d.insert_event(ins_body(uid, make_event_id(uid)))
    with pytest.raises(ConflictError):
        d.insert_event(ins_body(uid, make_event_id(uid)))


def test_insert_over_existing_google_event_or_tombstone_conflicts(populated):
    d = DryRunGateway(populated)
    with pytest.raises(ConflictError):
        d.insert_event(ins_body())  # live in Google
    with pytest.raises(ConflictError):
        d.insert_event(ins_body(UID2, EID2))  # tombstone in Google
    assert d.planned_writes == []  # a rejected insert is not a planned write


def test_patch_event_applies_to_overlay_only(populated):
    d = DryRunGateway(populated)
    out = d.patch_event(EID, event_patch_body(dev(summary="Abgesagt: Abgabe", popup_minutes=None, location="Raum 1"), description="neu"))
    assert out["summary"] == "Abgesagt: Abgabe" and out["reminders"] == {"useDefault": False, "overrides": []}
    seen = d.get_event(EID)
    assert seen["description"] == "neu" and seen["location"] == "Raum 1"
    assert populated.get_event(EID)["summary"] == "Abgabe: Projektarbeit"
    # a later patch builds on the simulated state (null clears)
    d.patch_event(EID, {"location": None})
    assert "location" not in d.get_event(EID)


def test_patch_event_nested_private_keys_merge(populated):
    d = DryRunGateway(populated)
    d.patch_event(EID, {"extendedProperties": {"private": {"extra": "1"}}})
    priv = d.get_event(EID)["extendedProperties"]["private"]
    assert priv["extra"] == "1" and priv["schulsync_uid"] == UID


def test_find_events_overlay_replaces_patched_inner_event_and_keeps_tombstones(populated):
    d = DryRunGateway(populated)
    d.patch_event(EID, {"summary": "Neu"})
    found = d.find_events_by_uid(UID)
    assert [(e["id"], e["summary"]) for e in found] == [(EID, "Neu")]
    assert d.find_events_by_uid(UID2) == [{"id": EID2, "status": "cancelled"}]


def test_reads_pass_through_untouched_inner_data(populated):
    d = DryRunGateway(populated)
    assert d.get_event(EID) == populated.get_event(EID)
    assert d.get_event(EID2) == {"id": EID2, "status": "cancelled"}
    assert d.get_event("unknownid1") is None
    assert d.list_tasks() == populated.list_tasks()
    assert d.get_task("task-0002") == populated.get_task("task-0002")
    assert not d.reads_unavailable and d.read_errors == []


def test_returned_dicts_are_copies(populated):
    d = DryRunGateway(populated)
    out = d.insert_event(ins_body("eduvidual:assignment:300", make_event_id("eduvidual:assignment:300")))
    out["summary"] = "tampered"
    assert d.get_event(make_event_id("eduvidual:assignment:300"))["summary"] == "Abgabe: Projektarbeit"


# --------------------------------------------------------------------------- overlay consistency: tasks


def test_insert_task_visible_in_list_and_get_with_fake_id(populated):
    d = DryRunGateway(populated)
    t = d.insert_task(task_body("Lernen: neu", "u:3:c"))
    assert t["id"].startswith("dryrun-task-") and t["status"] == "needsAction" and t["due"] == "2026-10-13T00:00:00.000Z"
    ids = [x["id"] for x in d.list_tasks()]
    assert ids == ["task-0001", "task-0002", t["id"]]
    assert d.get_task(t["id"])["title"] == "Lernen: neu"
    assert populated.get_task(t["id"]) is None and len(populated.tasks) == 2


def test_two_inserted_tasks_get_distinct_ids():
    d = DryRunGateway(None)
    assert d.insert_task(task_body())["id"] != d.insert_task(task_body())["id"]


def test_patch_real_task_shows_in_list_in_place(populated):
    d = DryRunGateway(populated)
    d.patch_task("task-0001", task_patch_body(DesiredTask("Bearbeiten: A neu", "m", date(2026, 10, 20)), notes="N", include_due=True))
    listed = d.list_tasks()
    assert [t["id"] for t in listed] == ["task-0001", "task-0002"]
    assert (listed[0]["title"], listed[0]["notes"], listed[0]["due"]) == ("Bearbeiten: A neu", "N", "2026-10-20T00:00:00.000Z")
    assert populated.get_task("task-0001")["title"] == "Bearbeiten: A"


def test_patch_simulated_task(populated):
    d = DryRunGateway(populated)
    t = d.insert_task(task_body())
    out = d.patch_task(t["id"], {"title": "x", "notes": None})
    assert out["title"] == "x" and "notes" not in out
    assert d.get_task(t["id"])["title"] == "x"


def test_patch_never_changes_status_unless_asked(populated):
    d = DryRunGateway(populated)
    d.patch_task("task-0002", task_patch_body(DesiredTask("t", "m", date(2026, 10, 1)), notes="n", include_due=False))
    assert d.get_task("task-0002")["status"] == "completed"


def test_delete_task_marks_deleted_in_overlay_like_google(populated):
    d = DryRunGateway(populated)
    d.delete_task("task-0001")
    assert d.get_task("task-0001")["deleted"] is True
    assert {t["id"]: t.get("deleted") for t in d.list_tasks()} == {"task-0001": True, "task-0002": None}
    assert not populated.get_task("task-0001").get("deleted")
    d.delete_task("task-0001")  # idempotent
    d.delete_task("unknown")  # no error
    assert d.get_task("unknown") is None


def test_delete_simulated_task(populated):
    d = DryRunGateway(populated)
    t = d.insert_task(task_body())
    d.delete_task(t["id"])
    assert d.get_task(t["id"])["deleted"] is True


# --------------------------------------------------------------------------- parity with the FakeGoogle (same script, same visible state)


def _project(events: dict, tasks: list):
    ev = {k: (v["summary"], v["description"], v.get("location"), v["reminders"], v["start"], v["end"]) for k, v in events.items()}
    tk = [(t["title"], t.get("notes"), t.get("due"), t["status"], bool(t.get("deleted"))) for t in tasks]
    return ev, tk


def _script(g):
    g.insert_event(ins_body("eduvidual:assignment:300", make_event_id("eduvidual:assignment:300"), location="Raum 7"))
    g.patch_event(EID, event_patch_body(dev(summary="Neu", location=None, start=date(2026, 10, 16), end=date(2026, 10, 17)), description="d2"))
    t = g.insert_task(task_body("Lernen: neu", "u:3:c"))
    g.patch_task(t["id"], {"title": "Lernen: neu 2", "due": "2026-10-15T00:00:00.000Z"})
    g.patch_task("task-0001", {"notes": None})
    g.delete_task("task-0001")
    return t["id"]


def test_overlay_matches_what_a_real_run_would_leave_behind():
    def fresh() -> FakeGoogle:
        g = FakeGoogle()
        g.insert_event(ins_body())
        g.insert_task(task_body("Bearbeiten: A", "u:1:a"))
        return g

    real = fresh()
    _script(real)
    sim_inner = fresh()
    sim = DryRunGateway(sim_inner)
    sim_task_id = _script(sim)

    real_events, real_tasks = _project(real.events, real.tasks)
    sim_events = {
        e["id"]: e
        for e in (sim.get_event(EID), sim.get_event(make_event_id("eduvidual:assignment:300")))
    }
    sim_ev, sim_tk = _project(sim_events, sim.list_tasks())
    assert sim_ev == real_events
    assert sim_tk == real_tasks
    assert sim_task_id.startswith("dryrun-task-")


# --------------------------------------------------------------------------- inner None / failing reads


def test_inner_none_reads_empty_and_flag_set():
    d = DryRunGateway(None)
    assert d.reads_unavailable
    assert d.get_event(EID) is None and d.find_events_by_uid(UID) == []
    assert d.list_tasks() == [] and d.get_task("x") is None
    assert d.check_targets().calendar_summary == "Schule"


def test_inner_none_writes_still_simulated_and_consistent():
    d = DryRunGateway(None)
    d.insert_event(ins_body())
    d.insert_task(task_body())
    assert d.get_event(EID) is not None and len(d.list_tasks()) == 1
    # patch without a known base: still answers (preview must not crash), echoing the patch
    out = d.patch_event("unknownid1", {"summary": "x"})
    assert out == {"id": "unknownid1", "summary": "x"}
    assert d.patch_task("nope", {"title": "t"}) == {"id": "nope", "title": "t"}


def test_failing_reads_degrade_and_are_remembered():
    g = FakeGoogle()
    g.fail_always("get_event", NetworkError("Keine Verbindung"))
    g.fail_always("list_tasks", GoogleAuthError("Anmeldung abgelaufen"))
    g.fail_always("find_events_by_uid", NetworkError("Keine Verbindung"))
    g.fail_always("get_task", NetworkError("Keine Verbindung"))
    d = DryRunGateway(g)
    assert not d.reads_unavailable
    assert d.get_event(EID) is None
    assert d.list_tasks() == []
    assert d.find_events_by_uid(UID) == []
    assert d.get_task("x") is None
    assert d.reads_unavailable
    codes = [e.code for e in d.read_errors]
    assert codes == ["network", "google_auth"]  # de-duplicated, German messages kept
    assert all(e.message for e in d.read_errors)
    d.insert_event(ins_body())  # writes keep working
    assert g.write_log == []


def test_unexpected_exceptions_are_not_swallowed():
    g = FakeGoogle()
    g.fail_always("get_event", RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        DryRunGateway(g).get_event(EID)


# --------------------------------------------------------------------------- targets


def test_check_targets_delegates_and_may_raise():
    assert DryRunGateway(FakeGoogle()).check_targets().calendar_id == "cal-schule"
    with pytest.raises(TargetUnavailableError):
        DryRunGateway(FakeGoogle(calendar_is_primary=True)).check_targets()
    with pytest.raises(TargetUnavailableError):
        DryRunGateway(FakeGoogle(tasklist_exists=False)).check_targets()


def test_nothing_misrouted_to_primary_calendar():
    g = FakeGoogle(calendar_is_primary=True)  # a broken setup: the preview must still not write anywhere
    d = DryRunGateway(g)
    d.insert_event(ins_body())
    assert g.misrouted_writes() == [] and g.primary_events == {} and g.write_log == []
