"""H3: DpapiSecretStore under concurrency (threads and processes), os.replace retries, writer lock.

DPAPI tests run against the real Windows API like tests/test_secrets_store.py; the process test spawns real children."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from schulsync import redact, secrets_store
from schulsync.errors import ConfigError, TransientError
from schulsync.lock import ProcessLock
from schulsync.secrets_store import DpapiSecretStore

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI exists only on Windows")
SRC = str(Path(__file__).resolve().parent.parent / "src")


@pytest.fixture(autouse=True)
def _clean_redact():
    redact.clear_registered_secrets()
    yield
    redact.clear_registered_secrets()


@pytest.fixture
def fast_retry(monkeypatch):
    """No real waiting in the retry tests; returns the list of requested pauses."""
    pauses: list[float] = []
    monkeypatch.setattr(secrets_store.time, "sleep", pauses.append)
    return pauses


def _tmp_leftovers(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.glob("*.tmp"))


# --------------------------------------------------------------------------- threads


@windows_only
def test_four_threads_writing_different_names_lose_nothing_and_raise_nothing(tmp_path):
    path = tmp_path / "secrets.dat"
    errors: list[BaseException] = []
    per_thread = 15

    def writer(tag: str) -> None:
        store = DpapiSecretStore(path)  # one instance per thread: the lock must not live inside the instance
        try:
            for i in range(per_thread):
                store.set(f"{tag}_{i}", f"{tag}-dummy-wert-{i}")
        except BaseException as exc:  # noqa: BLE001 - the test wants to see ANY escape
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []
    store = DpapiSecretStore(path)
    assert len(store.names()) == 4 * per_thread
    for n in range(4):
        for i in range(per_thread):
            assert store.get(f"t{n}_{i}") == f"t{n}-dummy-wert-{i}"
    assert _tmp_leftovers(path) == []


@windows_only
def test_readers_running_during_writes_never_see_a_raw_error(tmp_path):
    path = tmp_path / "secrets.dat"
    DpapiSecretStore(path).set("fest", "dummy-fest")
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        store = DpapiSecretStore(path)
        while not stop.is_set():
            try:
                assert store.get("fest") == "dummy-fest"
                store.names()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    r = threading.Thread(target=reader)
    r.start()
    w = DpapiSecretStore(path)
    for i in range(25):
        w.set(f"neu_{i}", f"dummy-{i}")
    stop.set()
    r.join(30)
    assert errors == []


@windows_only
def test_temp_names_are_unique_per_write(tmp_path, monkeypatch):
    path = tmp_path / "secrets.dat"
    seen: list[str] = []
    real = os.replace

    def spy(src, dst, *a, **k):
        seen.append(Path(src).name)
        return real(src, dst, *a, **k)

    monkeypatch.setattr(secrets_store.os, "replace", spy)
    store = DpapiSecretStore(path)
    for i in range(5):
        store.set(f"n{i}", "dummy-wert")
    assert len(seen) == 5 and len(set(seen)) == 5
    assert all(n.endswith(".tmp") and n != "secrets.dat.tmp" for n in seen)  # no fixed temp name any more


# --------------------------------------------------------------------------- os.replace retry


@windows_only
def test_replace_is_retried_on_permission_error_with_a_bounded_backoff(tmp_path, monkeypatch, fast_retry):
    path = tmp_path / "secrets.dat"
    real = os.replace
    failures = {"left": 3}

    def flaky(src, dst, *a, **k):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError(13, "Zugriff verweigert (Dateifreigabe)")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(secrets_store.os, "replace", flaky)
    store = DpapiSecretStore(path)
    store.set("a", "dummy-a")
    assert store.get("a") == "dummy-a"
    assert len(fast_retry) == 3 and fast_retry == sorted(fast_retry) and max(fast_retry) <= 1.0
    assert _tmp_leftovers(path) == []


@windows_only
def test_a_permanently_locked_target_gives_a_german_config_error_not_a_raw_exception(tmp_path, monkeypatch, fast_retry):
    path = tmp_path / "secrets.dat"
    store = DpapiSecretStore(path)
    store.set("alt", "dummy-alt")
    monkeypatch.setattr(secrets_store.os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "gesperrt")))
    with pytest.raises(ConfigError) as ei:
        store.set("neu", "dummy-neu")
    assert "gespeichert" in ei.value.message and ei.value.hint
    assert len(fast_retry) <= 10  # bounded
    monkeypatch.undo()
    assert store.get("alt") == "dummy-alt" and store.get("neu") is None  # the old file is intact
    assert _tmp_leftovers(path) == []  # the failed write cleaned up after itself


@windows_only
def test_a_reader_blocked_by_a_replace_retries_instead_of_failing(tmp_path, monkeypatch, fast_retry):
    path = tmp_path / "secrets.dat"
    store = DpapiSecretStore(path)
    store.set("a", "dummy-a")
    real = Path.read_bytes
    failures = {"left": 2}

    def flaky(self):
        if self.name == "secrets.dat" and failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError(13, "gerade ersetzt")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert store.get("a") == "dummy-a"
    assert len(fast_retry) == 2


# --------------------------------------------------------------------------- writer lock across processes


@windows_only
def test_set_waits_for_the_cross_process_writer_lock_and_times_out_with_a_transient_error(tmp_path, monkeypatch):
    path = tmp_path / "secrets.dat"
    store = DpapiSecretStore(path)
    monkeypatch.setattr(secrets_store, "_LOCK_WAIT_SECONDS", 0.3)
    with ProcessLock(store.lock_path):  # "another process" holds the writer lock
        with pytest.raises(TransientError) as ei:
            store.set("a", "dummy-a")
        assert "anderen SchulSync" in ei.value.message
        assert store.names() == []  # reading never needs the writer lock
    store.set("a", "dummy-a")  # free again
    assert store.get("a") == "dummy-a"


@windows_only
def test_the_writer_lock_is_not_the_program_lock(tmp_path):
    """`run` holds the program lock (paths.lock) while it may refresh the Google token: the secrets writer lock must
    be another file, otherwise the token refresh of a running sync would block on its own process."""
    store = DpapiSecretStore(tmp_path / "secrets.dat")
    assert store.lock_path != tmp_path / "schulsync.lock" and store.lock_path.parent == tmp_path
    with ProcessLock(tmp_path / "schulsync.lock"):
        store.set("a", "dummy-a")


_WRITER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from schulsync.secrets_store import DpapiSecretStore

    store = DpapiSecretStore(Path(sys.argv[1]))
    tag = sys.argv[2]
    for i in range(int(sys.argv[3])):
        store.set(f"{tag}_{i}", f"{tag}-dummy-wert-{i}")
    """
)


@windows_only
def test_three_processes_writing_at_once_lose_no_entry(tmp_path):
    path = tmp_path / "secrets.dat"
    env = dict(os.environ, PYTHONPATH=SRC)
    per_proc = 10
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(path), f"p{n}", str(per_proc)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for n in range(3)
    ]
    results = [(p.communicate(timeout=240), p.returncode) for p in procs]
    for (out, err), code in results:
        assert code == 0 and err.strip() == "", err
    store = DpapiSecretStore(path)
    assert len(store.names()) == 3 * per_proc
    for n in range(3):
        for i in range(per_proc):
            assert store.get(f"p{n}_{i}") == f"p{n}-dummy-wert-{i}"
    json.loads(path.read_text(encoding="utf-8"))  # the file is intact JSON
    assert _tmp_leftovers(path) == []


@windows_only
def test_delete_is_serialised_with_set(tmp_path):
    path = tmp_path / "secrets.dat"
    store = DpapiSecretStore(path)
    errors: list[BaseException] = []

    def churn(tag: str) -> None:
        s = DpapiSecretStore(path)
        try:
            for i in range(8):
                s.set(f"{tag}_{i}", "dummy")
                s.delete(f"{tag}_{i}")
            s.set(f"{tag}_keep", f"{tag}-bleibt")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(f"c{n}",)) for n in range(3)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert errors == [] and store.names() == ["c0_keep", "c1_keep", "c2_keep"]
