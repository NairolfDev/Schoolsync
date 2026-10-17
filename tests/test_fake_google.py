"""Tests of the FakeGoogle test double itself, so engine tests can trust it."""

from __future__ import annotations

import copy
from datetime import date, datetime

import pytest

from schulsync.errors import (
    ConflictError,
    NotFoundError,
    OutcomeUnknownError,
    TargetUnavailableError,
    TransientError,
)
from schulsync.google_bodies import (
    event_insert_body,
    event_patch_body,
    task_insert_body,
    task_patch_body,
)
from schulsync.google_types import GoogleGateway, TASK_MARKER_PREFIX, TargetCheck, make_event_id
from schulsync.models import DesiredEvent, DesiredTask
from schulsync.timeutil import VIENNA
from tests.support.fake_google import FakeGoogle

UID = "webuntis:exam:9001"
PROPS = {"schulsync": "1", "schulsync_uid": UID, "schulsync_source": "webuntis", "schulsync_kind": "exam"}
EID = make_event_id(UID)


def desired(**kw) -> DesiredEvent:
    base = dict(
        summary="Schularbeit: Mathematik",
        managed_text="x",
        start=datetime(2026, 10, 20, 8, 0, tzinfo=VIENNA),
        end=datetime(2026, 10, 20, 9, 40, tzinfo=VIENNA),
        location="Raum 2B",
        popup_minutes=1440,
        private_props=PROPS,
    )
    base.update(kw)
    return DesiredEvent(**base)


def ins(g: FakeGoogle, d: DesiredEvent | None = None, eid: str = EID) -> dict:
    return g.insert_event(event_insert_body(d or desired(), event_id=eid, description="Beschreibung"))


def new_task(g: FakeGoogle, title="Lernen: Mathematik", due=date(2026, 10, 13), uid=UID) -> dict:
    return g.insert_task(task_insert_body(DesiredTask(title, "m", due), notes=f"Notiz\n{TASK_MARKER_PREFIX}{uid}"))


def test_fake_satisfies_gateway_protocol():
    g: GoogleGateway = FakeGoogle()
    assert isinstance(g.check_targets(), TargetCheck)


# --------------------------------------------------------------------------- targets


def test_check_targets_ok():
    t = FakeGoogle().check_targets()
    assert (t.calendar_id, t.calendar_summary, t.tasklist_id, t.tasklist_title) == ("cal-schule", "Schule", "list-schule", "Schule")


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"calendar_exists": False}, "Kalender"),
        ({"calendar_is_primary": True}, "Hauptkalender"),
        ({"tasklist_exists": False}, "Aufgabenliste"),
    ],
)
def test_check_targets_fails_with_german_message(kwargs, fragment):
    with pytest.raises(TargetUnavailableError) as ei:
        FakeGoogle(**kwargs).check_targets()
    assert fragment in ei.value.message


def test_missing_calendar_blocks_event_ops_and_never_falls_back():
    g = FakeGoogle(calendar_exists=False)
    with pytest.raises(TargetUnavailableError):
        ins(g)
    assert g.write_log == [] and g.primary_events == {} and g.misrouted_writes() == []


def test_missing_tasklist_blocks_task_ops():
    g = FakeGoogle(tasklist_exists=False)
    for call in (g.list_tasks, lambda: new_task(g), lambda: g.get_task("x"), lambda: g.delete_task("x")):
        with pytest.raises(TargetUnavailableError):
            call()
    assert g.write_log == []


def test_write_to_primary_is_detectable_when_check_is_ignored():
    g = FakeGoogle(calendar_is_primary=True)
    with pytest.raises(TargetUnavailableError):
        g.check_targets()
    ins(g)  # code under test ignored the failing check
    assert len(g.misrouted_writes()) == 1 and EID in g.primary_events
    g.reset_write_log()
    assert g.misrouted_writes() == []


def test_normal_writes_never_touch_primary():
    g = FakeGoogle()
    ins(g)
    g.patch_event(EID, event_patch_body(desired(summary="neu"), description="d"))
    assert g.misrouted_writes() == [] and g.primary_events == {}


# --------------------------------------------------------------------------- events


def test_insert_get_roundtrip_and_defaults():
    g = FakeGoogle()
    out = ins(g)
    assert out["id"] == EID and out["status"] == "confirmed"
    got = g.get_event(EID)
    assert got["summary"] == "Schularbeit: Mathematik"
    assert got["start"] == {"dateTime": "2026-10-20T08:00:00+02:00", "timeZone": "Europe/Vienna"}
    assert got["extendedProperties"]["private"]["schulsync_uid"] == UID
    assert g.get_event("unknownid1") is None


def test_returned_dicts_are_copies():
    g = FakeGoogle()
    ins(g)
    g.get_event(EID)["summary"] = "hacked"
    assert g.events[EID]["summary"] == "Schularbeit: Mathematik"


def test_insert_same_id_conflicts():
    g = FakeGoogle()
    ins(g)
    with pytest.raises(ConflictError):
        ins(g)
    assert len(g.write_log) == 1  # rejected insert is not a write
    assert g.calls["insert_event"] == 2


def test_insert_with_deleted_id_conflicts_too():
    g = FakeGoogle()
    ins(g)
    g.user_delete_event(EID)
    with pytest.raises(ConflictError):
        ins(g)


@pytest.mark.parametrize("bad", ["abc", "ss-xyz-123", "SSABCDEF", "z" * 8, "a" * 1025])
def test_invalid_custom_event_id_rejected(bad):
    with pytest.raises(ValueError):
        ins(FakeGoogle(), eid=bad)


def test_invalid_event_bodies_rejected():
    g = FakeGoogle()
    good = event_insert_body(desired(), event_id=EID, description="")
    for mutate in (
        lambda b: b["start"].update(date="2026-10-20"),  # both date and dateTime
        lambda b: b.update(end={"date": "2026-10-20"}),  # mixed representations
        lambda b: b["end"].update(dateTime="2026-10-20T07:00:00+02:00"),  # end before start
        lambda b: b["reminders"]["overrides"].append({"method": "sms", "minutes": 1}),
        lambda b: b["reminders"].update(overrides=[{"method": "popup", "minutes": 1}] * 6),
        lambda b: b["start"].update(dateTime="2026-10-20T08:00:00"),  # no offset ...
    ):
        body = copy.deepcopy(good)
        mutate(body)
        if body["start"].get("dateTime") == "2026-10-20T08:00:00":
            body["start"].pop("timeZone")  # ... and no timeZone
        with pytest.raises(ValueError):
            g.insert_event(body)
    assert g.write_log == []


def test_all_day_exclusive_end_enforced():
    g = FakeGoogle()
    body = event_insert_body(desired(start=date(2026, 10, 20), end=date(2026, 10, 21)), event_id=EID, description="")
    g.insert_event(body)
    body["id"] = "ss" + "1" * 8
    body["end"] = {"date": "2026-10-20"}
    with pytest.raises(ValueError):
        g.insert_event(body)


def test_private_property_limits_are_silent():
    g = FakeGoogle()
    body = event_insert_body(desired(private_props={"ok": "v"}), event_id=EID, description="")
    body["extendedProperties"]["private"].update({"k" * 45: "dropped", "long": "v" * 2000})
    out = g.insert_event(body)
    priv = out["extendedProperties"]["private"]
    assert "k" * 45 not in priv and len(priv["long"]) == 1024 and priv["ok"] == "v"


def test_patch_merges_none_clears_and_keeps_foreign_private_keys():
    g = FakeGoogle()
    ins(g)
    g.patch_event(EID, {"extendedProperties": {"private": {"mine": "1"}}})  # a foreign key
    out = g.patch_event(EID, event_patch_body(desired(location=None, summary="Neu"), description="d2"))
    assert out["summary"] == "Neu" and out["description"] == "d2"
    assert "location" not in out  # null cleared the field
    priv = out["extendedProperties"]["private"]
    assert priv["mine"] == "1" and priv["schulsync_uid"] == UID  # nested key merge
    g.patch_event(EID, {"extendedProperties": {"private": {"mine": None}}})
    assert "mine" not in g.events[EID]["extendedProperties"]["private"]


def test_patch_replaces_reminder_arrays():
    g = FakeGoogle()
    ins(g)
    out = g.patch_event(EID, event_patch_body(desired(popup_minutes=None, cancelled=True), description=""))
    assert out["reminders"] == {"useDefault": False, "overrides": []}


def test_patch_switching_all_day_and_timed_stays_valid():
    g = FakeGoogle()
    ins(g)
    out = g.patch_event(EID, event_patch_body(desired(start=date(2026, 10, 20), end=date(2026, 10, 21)), description=""))
    assert out["start"] == {"date": "2026-10-20", "timeZone": "Europe/Vienna"}  # timeZone is left alone, harmless
    assert "dateTime" not in out["start"]
    out = g.patch_event(EID, event_patch_body(desired(), description=""))
    assert "date" not in out["start"] and out["start"]["dateTime"] == "2026-10-20T08:00:00+02:00"


def test_patch_without_nulling_would_be_rejected():
    g = FakeGoogle()
    ins(g, desired(start=date(2026, 10, 20), end=date(2026, 10, 21)))
    with pytest.raises(ValueError):  # naive patch (no null) leaves date AND dateTime
        g.patch_event(EID, {"start": {"dateTime": "2026-10-20T08:00:00+02:00"}, "end": {"dateTime": "2026-10-20T09:00:00+02:00"}})


def test_patch_unknown_or_deleted_event_is_404_and_never_revives():
    g = FakeGoogle()
    with pytest.raises(NotFoundError):
        g.patch_event("unknownid1", {"summary": "x"})
    ins(g)
    g.user_delete_event(EID)
    with pytest.raises(NotFoundError):
        g.patch_event(EID, {"summary": "x"})
    assert g.events == {}


def test_deleted_event_reads_as_tombstone():
    g = FakeGoogle()
    ins(g)
    g.user_delete_event(EID)
    assert g.get_event(EID) == {"id": EID, "status": "cancelled"}
    assert g.deleted_event_ids == {EID}


def test_deleted_event_gone_variant():
    g = FakeGoogle(deleted_event_get_returns_none=True)
    ins(g)
    g.user_delete_event(EID)
    assert g.get_event(EID) is None


def test_find_events_by_uid_live_and_tombstone_without_ext_props():
    g = FakeGoogle()
    ins(g)
    other = "ss" + "9" * 8
    g.insert_event(event_insert_body(desired(private_props={**PROPS, "schulsync_uid": "webuntis:exam:1"}), event_id=other, description=""))
    found = g.find_events_by_uid(UID)
    assert [e["id"] for e in found] == [EID] and found[0]["extendedProperties"]["private"]["schulsync_uid"] == UID
    g.user_delete_event(EID)
    found = g.find_events_by_uid(UID)
    assert found == [{"id": EID, "status": "cancelled"}]  # no extendedProperties on tombstones
    assert g.find_events_by_uid("nope") == []


def test_user_edits_on_events():
    g = FakeGoogle()
    ins(g)
    g.user_edit_event_description(EID, "meine Notiz")
    g.user_edit_event_summary(EID, "Mein Titel")
    got = g.get_event(EID)
    assert got["description"] == "meine Notiz" and got["summary"] == "Mein Titel"
    assert len(g.write_log) == 1  # user actions are not logged writes
    with pytest.raises(KeyError):
        g.user_delete_event("unknownid1")


# --------------------------------------------------------------------------- tasks


def test_task_insert_assigns_ids_and_roundtrips():
    g = FakeGoogle()
    a, b = new_task(g), new_task(g, uid="webuntis:exam:2")
    assert a["id"] != b["id"] and a["status"] == "needsAction"
    assert a["title"] == "Lernen: Mathematik" and a["due"] == "2026-10-13T00:00:00.000Z"
    assert TASK_MARKER_PREFIX + UID in a["notes"]
    assert g.get_task(a["id"])["notes"] == a["notes"]
    assert g.get_task("nope") is None
    assert [t["id"] for t in g.list_tasks()] == [a["id"], b["id"]]


def test_task_due_time_portion_is_discarded():
    g = FakeGoogle()
    t = g.insert_task({"title": "t", "due": "2026-10-13T22:30:00.000Z"})
    assert t["due"] == "2026-10-13T00:00:00.000Z"


def test_task_limits_enforced():
    g = FakeGoogle()
    with pytest.raises(ValueError):
        g.insert_task({"title": "x" * 1025})
    with pytest.raises(ValueError):
        g.insert_task({"title": "x", "notes": "n" * 8193})
    assert g.tasks == []


def test_task_patch_changes_title_notes_due_only():
    g = FakeGoogle()
    t = new_task(g)
    out = g.patch_task(t["id"], task_patch_body(DesiredTask("Neu", "m", date(2026, 10, 15)), notes="N2", include_due=True))
    assert (out["title"], out["notes"], out["due"], out["status"]) == ("Neu", "N2", "2026-10-15T00:00:00.000Z", "needsAction")
    out = g.patch_task(t["id"], task_patch_body(DesiredTask("Neu2", "m", date(2026, 10, 30)), notes="N3", include_due=False))
    assert out["due"] == "2026-10-15T00:00:00.000Z"  # due untouched
    with pytest.raises(NotFoundError):
        g.patch_task("unknown", {"title": "x"})


def test_task_patch_none_clears_notes_and_due():
    g = FakeGoogle()
    t = new_task(g)
    out = g.patch_task(t["id"], {"notes": None, "due": None})
    assert "notes" not in out and "due" not in out


def test_user_complete_reopen_and_hidden():
    g = FakeGoogle()
    t = new_task(g)
    g.user_complete_task(t["id"])
    done = g.get_task(t["id"])
    assert done["status"] == "completed" and "completed" in done
    g.user_clear_completed()
    assert g.get_task(t["id"])["hidden"] is True
    assert [x["id"] for x in g.list_tasks()] == [t["id"]]  # still listed (all flags)
    g.user_reopen_task(t["id"])
    back = g.get_task(t["id"])
    assert back["status"] == "needsAction" and "completed" not in back and "hidden" not in back


def test_delete_task_marks_deleted_and_is_idempotent():
    g = FakeGoogle()
    t = new_task(g)
    g.delete_task(t["id"])
    first = g.get_task(t["id"])
    assert first["deleted"] is True
    assert [x["id"] for x in g.list_tasks()] == [t["id"]]  # deleted ones are included
    g.delete_task(t["id"])  # no-op
    assert g.get_task(t["id"])["updated"] == first["updated"]
    g.delete_task("never-existed")  # no error
    assert g.open_tasks == []


def test_user_delete_task_flag_and_purge():
    g = FakeGoogle()
    a, b = new_task(g), new_task(g, uid="x:y:z")
    g.user_delete_task(a["id"])
    g.user_delete_task(b["id"], purge=True)
    assert g.get_task(a["id"])["deleted"] is True
    assert g.get_task(b["id"]) is None and [t["id"] for t in g.list_tasks()] == [a["id"]]


def test_user_edit_notes_and_move_due():
    g = FakeGoogle()
    t = new_task(g)
    g.user_edit_task_notes(t["id"], "eigene Notiz")
    g.user_move_task_due(t["id"], date(2026, 11, 2))
    got = g.get_task(t["id"])
    assert got["notes"] == "eigene Notiz" and got["due"] == "2026-11-02T00:00:00.000Z"
    assert len(g.write_log) == 1


# --------------------------------------------------------------------------- observability


def test_write_log_records_accepted_writes_only():
    g = FakeGoogle()
    ins(g)
    t = new_task(g)
    g.patch_task(t["id"], {"title": "x"})
    g.delete_task(t["id"])
    g.get_event(EID), g.list_tasks(), g.get_task(t["id"]), g.find_events_by_uid(UID)
    assert [w[0] for w in g.write_log] == ["insert_event", "insert_task", "patch_task", "delete_task"]
    assert g.write_log[0][1] == EID and g.write_log[1][1] is None and g.write_log[3][2] is None
    assert g.writes("insert_task")[0][2]["title"] == "Lernen: Mathematik"
    assert g.calls["get_event"] == 1 and g.calls["list_tasks"] == 1
    g.reset_write_log()
    assert g.write_log == [] and g.calls["insert_event"] == 1
    g.reset_counters()
    assert not g.calls


def test_write_log_body_is_a_snapshot():
    g = FakeGoogle()
    body = event_insert_body(desired(), event_id=EID, description="a")
    g.insert_event(body)
    body["summary"] = "mutated later"
    assert g.write_log[0][2]["summary"] == "Schularbeit: Mathematik"


# --------------------------------------------------------------------------- fault injection


def test_fail_next_not_applied_leaves_no_trace():
    g = FakeGoogle()
    g.fail_next("insert_event", TransientError("Netz weg"))
    with pytest.raises(TransientError):
        ins(g)
    assert g.events == {} and g.write_log == []
    ins(g)  # second call works (times=1)
    assert EID in g.events


def test_fail_next_applied_is_a_lost_response():
    g = FakeGoogle()
    g.fail_next("insert_task", OutcomeUnknownError("Zeitüberschreitung"), applied=True)
    with pytest.raises(OutcomeUnknownError):
        new_task(g)
    assert len(g.tasks) == 1 and len(g.write_log) == 1  # stored although the caller saw an error
    g.fail_next("insert_event", OutcomeUnknownError("x"), applied=True)
    with pytest.raises(OutcomeUnknownError):
        ins(g)
    with pytest.raises(ConflictError):  # retry with same id hits the stored event
        ins(g)


def test_fail_next_times_and_order():
    g = FakeGoogle()
    g.fail_next("list_tasks", TransientError("a"), times=2)
    for _ in range(2):
        with pytest.raises(TransientError):
            g.list_tasks()
    assert g.list_tasks() == []
    assert g.calls["list_tasks"] == 3


def test_fail_always_and_clear():
    g = FakeGoogle()
    g.fail_always("get_task", TransientError("immer"))
    for _ in range(3):
        with pytest.raises(TransientError):
            g.get_task("x")
    g.fail_next("get_task", NotFoundError("einmal"))  # queued faults win over fail_always
    with pytest.raises(NotFoundError):
        g.get_task("x")
    g.clear_faults()
    assert g.get_task("x") is None


def test_fault_accepts_exception_class():
    g = FakeGoogle()
    g.fail_next("check_targets", TransientError)
    with pytest.raises(TransientError):
        g.check_targets()


def test_fault_argument_validation():
    g = FakeGoogle()
    with pytest.raises(ValueError):
        g.fail_next("no_such_op", TransientError("x"))
    with pytest.raises(ValueError):
        g.fail_next("get_event", TransientError("x"), applied=True)
    with pytest.raises(ValueError):
        g.fail_next("get_event", TransientError("x"), times=0)
    with pytest.raises(ValueError):
        g.fail_always("nope", TransientError("x"))


def test_applied_fault_with_server_side_conflict_still_raises_injected_error():
    g = FakeGoogle()
    ins(g)
    g.fail_next("insert_event", OutcomeUnknownError("x"), applied=True)
    with pytest.raises(OutcomeUnknownError):
        ins(g)
    assert len(g.write_log) == 1
