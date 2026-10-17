"""A REAL killed process between a Google write and the local database commit (ANFORDERUNGEN 7 and 10: 'Prozessabbruch nach
einem Google-Schreibvorgang').

The acceptance tests simulate the abort with an exception inside the same interpreter. Here a child process runs the real
runner, engine and SQLite file, and is ended with `os._exit` (no `finally`, no `atexit`, no flush: like a killed task or a power
cut) in the very moment the fake Google has accepted a create and before the engine can record it. The "server" keeps what it
accepted (the fake is pickled to a file first); the next start, in this process, must neither create a second entry nor leave
the database or the process lock in a state that blocks it.

The pickle file is written by the child of this very test into its own temp directory and read back by the same test: it is
never foreign data (a plain stand-in for "what the Google server holds", the fakes have no other serialisation).

What stays fake: Google (an in-memory fake that survives through a pickle file), the school platforms and the clock.
What is real: the second process, its death, the SQLite file with its journal, the process lock (released by the OS).
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from schulsync.config import Paths
from schulsync.models import OverallStatus
from tests.support.acceptance_harness import ASSIGNMENT, World, at, event_for, open_tasks_for, task_for, events_for
from tests.support.fake_google import FakeGoogle

REPO = Path(__file__).resolve().parents[1]
DAY = at(2026, 10, 14, 8, 0)

CHILD = '''
import os, pickle, sys
from pathlib import Path

from schulsync.config import Paths
from tests.support.acceptance_harness import World, at
from tests.support.fake_google import FakeGoogle

home, google_file, dying_op = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]


class Dying(FakeGoogle):
    """The server accepts the write, then the process is gone before it hears the answer."""


def make_dying(op):
    def method(self, *args, **kwargs):
        result = getattr(FakeGoogle, op)(self, *args, **kwargs)
        plain = FakeGoogle()
        plain.__dict__.update(self.__dict__)
        google_file.write_bytes(pickle.dumps(plain))  # what Google now holds
        os._exit(9)
    return method


setattr(Dying, dying_op, make_dying(dying_op))
world = World(Paths(home), google=Dying()).load_standard()
world.webuntis.set_items([])
world.start(at=at(2026, 10, 14, 8, 0))
os._exit(0)  # not reached: the write above ends the process
'''


@pytest.mark.parametrize("dying_op", ["insert_event", "insert_task"])
def test_a_process_killed_right_after_google_accepted_a_create_leaves_no_duplicate_at_the_next_start(tmp_path, dying_op):
    home, google_file = tmp_path / "home", tmp_path / "google.pickle"
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]), "PYTHONIOENCODING": "utf-8"}

    child = subprocess.run([sys.executable, str(script), str(home), str(google_file), dying_op], env=env, capture_output=True, timeout=120)

    assert child.returncode == 9, child.stderr.decode("utf-8", "replace")  # it really died inside the write
    google: FakeGoogle = pickle.loads(google_file.read_bytes())
    held = len(google.events) if dying_op == "insert_event" else len(google.tasks)
    assert held == 1  # Google accepted exactly the one write the child never heard back about

    world = World(Paths(home), google=google).load_standard()
    world.webuntis.set_items([])
    out = world.start(at=DAY)  # the lock of the dead process is free, the database file of the dead process opens

    assert out.exit_code == 0 and out.report.status == OverallStatus.SUCCESS, out.console_text
    assert len(events_for(world.google, ASSIGNMENT.uid)) == 1 and len(world.google.events) == 1
    assert len(open_tasks_for(world.google, ASSIGNMENT.uid)) == 1 and len(world.google.tasks) == 1
    assert event_for(world.google, ASSIGNMENT.uid)["summary"].startswith("Abgabe:")
    assert task_for(world.google, ASSIGNMENT.uid)["title"].startswith("Bearbeiten:")
    with world.store() as st:
        state = st.get(ASSIGNMENT.uid)
        assert state.event_id and state.task_id and not state.dirty
        assert st.pending_count() == 0 and st.open_intents() == []

    before = len(world.google.write_log)
    again = world.start(at=at(2026, 10, 15, 8, 0))  # and the next day nothing is written twice either
    assert again.exit_code == 0 and len(world.google.events) == 1 and len(world.google.tasks) == 1
    assert len(world.google.write_log) == before
