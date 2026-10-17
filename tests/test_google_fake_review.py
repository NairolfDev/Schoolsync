"""Adversarial review tests for google_bodies / dryrun / FakeGoogle. Each test demonstrates one defect."""

from __future__ import annotations

import pytest

from schulsync import textblocks
from schulsync.dryrun import DryRunGateway
from schulsync.errors import NetworkError, NotFoundError
from schulsync.google_bodies import task_marker_uid
from tests.support.fake_google import FakeGoogle


@pytest.mark.parametrize("due", ["2026-10-13", "2026-10-13T00:00:00"])
def test_fake_rejects_task_due_that_is_not_a_full_rfc3339_timestamp(due):
    """Tasks `due` is an RFC 3339 timestamp (date, time AND offset/Z). Real Google answers 400 'Invalid value
    for: Invalid format' for a bare date (n8n issue 20274, make.com community thread). The fake accepts both,
    so an engine that sent `date.isoformat()` would pass every fake-based test and fail in production."""
    g = FakeGoogle()
    with pytest.raises(ValueError):
        g.insert_task({"title": "t", "due": due})
    t = g.insert_task({"title": "t"})
    with pytest.raises(ValueError):
        g.patch_task(t["id"], {"due": due})


def test_fake_rejects_event_date_not_in_yyyy_mm_dd_form():
    """Calendar all-day `date` is 'yyyy-mm-dd'. `date.fromisoformat` (3.11+) also accepts '20261016'."""
    g = FakeGoogle()
    with pytest.raises(ValueError):
        g.insert_event({"id": "ssabcdef1", "start": {"date": "20261016"}, "end": {"date": "20261017"}})


@pytest.mark.parametrize(
    "bad_event",
    [
        {"reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": "5"}]}},
        {"reminders": {"useDefault": False, "overrides": None}},
        {"extendedProperties": {"private": None}},
    ],
)
def test_fake_reports_malformed_bodies_as_valueerror_like_its_docstring_promises(bad_event):
    """Docstring: fidelity checks 'raise ValueError'. Malformed values leak TypeError/AttributeError instead,
    so a test (or engine wrapper) that treats ValueError as 'body rejected' misclassifies them."""
    g = FakeGoogle()
    body = {"id": "ssabcdef2", "start": {"date": "2026-10-16"}, "end": {"date": "2026-10-17"}, **bad_event}
    with pytest.raises(ValueError):
        g.insert_event(body)


def test_dryrun_unknown_event_patch_still_404s_after_an_unrelated_read_failed():
    """`_patch_base` only raises 404 while `read_errors` is empty. One unrelated failed read (list_tasks) makes
    every later patch of a genuinely unknown event (clean get_event -> None) 'succeed' with a bare record, while a
    real run raises NotFoundError."""
    g = FakeGoogle()
    g.fail_next("list_tasks", NetworkError("Keine Verbindung"))
    d = DryRunGateway(g)
    d.list_tasks()  # unrelated failure, recorded in read_errors
    assert d.get_event("ssunknown000") is None  # this read worked: Google definitely has no such event
    with pytest.raises(NotFoundError):
        d.patch_event("ssunknown000", {"summary": "x"})


@pytest.mark.parametrize("notes", ["schulsync-id:abc", "Notiz<br>schulsync-id: abc"])
def test_marker_parsers_agree(notes):
    """google_bodies.task_marker_uid and textblocks.find_task_marker read the same line format but disagree
    ('schulsync-id:abc' without space; marker after an HTML line break). The engine adopts an existing task by
    marker to avoid duplicates; a parser that misses a marker the other one sees leads to a duplicate task."""
    assert task_marker_uid({"notes": notes}) == textblocks.find_task_marker(notes)
