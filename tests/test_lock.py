"""Tests for schulsync.lock, including a real second process holding the OS lock."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from schulsync.lock import LockHeldError, ProcessLock

# A venv's python.exe on Windows is a launcher that starts the real interpreter as a child process;
# terminating the launcher would leave the child (the real lock holder) running. The base interpreter
# is enough here: the holder script only needs the standard library and PYTHONPATH=src.
PYTHON = getattr(sys, "_base_executable", sys.executable)
SRC = str(Path(__file__).resolve().parent.parent / "src")

_HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from schulsync.lock import ProcessLock
    lock = ProcessLock(Path(sys.argv[1]))
    print("LOCKED" if lock.acquire() else "BUSY", flush=True)
    time.sleep(120)
    """
)


def _spawn_holder(path: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=SRC)
    proc = subprocess.Popen(
        [PYTHON, "-c", _HOLDER, str(path)], stdout=subprocess.PIPE, text=True, env=env
    )
    assert proc.stdout.readline().strip() == "LOCKED"
    return proc


def test_second_lock_object_in_same_process_fails(tmp_path):
    path = tmp_path / "x.lock"
    a, b = ProcessLock(path), ProcessLock(path)
    assert a.acquire() is True
    assert a.held
    assert b.acquire() is False
    assert not b.held
    a.release()
    assert b.acquire() is True
    b.release()


def test_release_then_reacquire_and_idempotent_calls(tmp_path):
    lock = ProcessLock(tmp_path / "x.lock")
    assert lock.acquire() and lock.acquire()  # already held by this object: still True
    lock.release()
    lock.release()  # no error
    assert not lock.held
    assert lock.acquire()
    lock.release()


def test_context_manager_and_lock_held_error(tmp_path):
    path = tmp_path / "x.lock"
    with ProcessLock(path) as outer:
        assert outer.held
        with pytest.raises(LockHeldError) as exc:
            with ProcessLock(path):
                pytest.fail("must not be entered")
        assert "läuft bereits" in exc.value.message
    assert ProcessLock(path).acquire()  # released by __exit__, even though the other object is GC'd later


def test_context_manager_releases_on_exception(tmp_path):
    path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        with ProcessLock(path):
            raise RuntimeError("boom")
    other = ProcessLock(path)
    assert other.acquire()
    other.release()


def test_creates_missing_directory_and_writes_pid_for_diagnostics(tmp_path):
    path = tmp_path / "neu" / "x.lock"
    lock = ProcessLock(path)
    assert lock.acquire()
    assert lock.holder_pid() == os.getpid()  # readable by others while locked
    assert path.read_text().strip() == str(os.getpid())
    lock.release()


def test_blocking_acquire_times_out_then_succeeds_after_release(tmp_path):
    path = tmp_path / "x.lock"
    a, b = ProcessLock(path), ProcessLock(path)
    assert a.acquire()
    start = time.monotonic()
    assert b.acquire(blocking=True, timeout=0.3) is False
    assert time.monotonic() - start >= 0.3
    a.release()
    assert b.acquire(blocking=True, timeout=1.0) is True
    b.release()


def test_subprocess_holding_lock_blocks_us_and_death_frees_it(tmp_path):
    path = tmp_path / "x.lock"
    proc = _spawn_holder(path)
    try:
        mine = ProcessLock(path)
        assert mine.acquire() is False
        assert mine.holder_pid() == proc.pid
        proc.terminate()  # hard stop on Windows (TerminateProcess): no cleanup code runs in the child
        proc.wait(timeout=10)
        deadline = time.monotonic() + 5
        while not mine.acquire():  # the OS closes the handle on process death
            assert time.monotonic() < deadline, "lock still held after the holder died"
            time.sleep(0.05)
        mine.release()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()


def test_subprocess_cannot_lock_while_we_hold_it(tmp_path):
    path = tmp_path / "x.lock"
    mine = ProcessLock(path)
    assert mine.acquire()
    env = dict(os.environ, PYTHONPATH=SRC)
    out = subprocess.run(
        [PYTHON, "-c", _HOLDER.replace("time.sleep(120)", ""), str(path)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert out.stdout.strip() == "BUSY", out.stderr
    mine.release()
