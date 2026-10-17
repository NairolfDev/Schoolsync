"""H4 in a REAL child process: the watchdog ends a hung `run` with the real `os._exit(3)`, the failure report is on disk
and the program lock (held by the hung 'run') is free again afterwards because the OS released it with the process.
Run with python.exe and with pythonw.exe (what the autostart really uses: no stdout/stderr at all).

The hung HTTP call is simulated by a `runner.run` that takes the lock like the real one and then sleeps. The child reports
what happened through a marker file, not through stdout (there is none under pythonw)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from schulsync.config import Paths
from schulsync.lock import ProcessLock

SRC = str(Path(__file__).resolve().parent.parent / "src")
PYTHONW = Path(sys.executable).with_name("pythonw.exe")

_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from schulsync import cli, runner
    from schulsync.config import Paths, set_config_value
    from schulsync.lock import ProcessLock
    from schulsync.secrets_store import MemorySecretStore

    paths = Paths(Path(sys.argv[1]))
    marker = Path(sys.argv[2])
    paths.ensure()
    cli._write_default_config(paths.config)
    set_config_value(paths.config, "retry", "run_timeout_seconds", 0.5)


    def note(text):
        with marker.open("a", encoding="utf-8") as fh:
            fh.write(text + "\\n")


    def hung(deps, mode, *, force=False):
        lock = ProcessLock(deps.paths.lock)
        assert lock.acquire()
        note("LOCKED")
        time.sleep(120)  # a call that never returns


    runner.run = hung
    env = cli.CliEnv(
        paths=paths,
        secrets=MemorySecretStore(),
        build_deps=lambda p, c, s: runner.Deps(paths=p, config=c, adapter_factories={}, gateway_factory=lambda: None),
        open_report=lambda path: note("OPENED " + path.name) or True,
        watchdog_grace=0.0,
        say=lambda text="": None,
    )
    sys.exit(cli.main(["run", "--auto"], env=env))
    """
)


@pytest.mark.parametrize(
    "exe",
    [
        pytest.param(Path(sys.executable), id="python"),
        pytest.param(PYTHONW, id="pythonw", marks=pytest.mark.skipif(not PYTHONW.exists(), reason="no pythonw.exe")),
    ],
)
def test_a_hung_run_is_ended_by_the_watchdog_with_exit_code_3_and_the_lock_is_released(tmp_path, exe):
    home, marker = tmp_path / "home", tmp_path / "marker.txt"
    started = time.monotonic()
    proc = subprocess.run(
        [str(exe), "-c", _CHILD, str(home), str(marker)],
        env=dict(os.environ, PYTHONPATH=SRC, PYTHONIOENCODING="utf-8"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert time.monotonic() - started < 100  # it did not sleep the 120 s of the hung call
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "LOCKED" and lines[1].startswith("OPENED bericht-") and len(lines) == 2  # the report was opened once
    reports = list((home / "reports").glob("bericht-*.html"))
    assert len(reports) == 1
    html = reports[0].read_text(encoding="utf-8")
    assert "Fehlgeschlagen" in html and "hart abgebrochen" in html and "timeout" in html
    log = "\n".join(f.read_text(encoding="utf-8") for f in (home / "logs").glob("*.log"))
    assert "Hartes Zeitlimit" in log
    lock = ProcessLock(Paths(home).lock)
    try:
        assert lock.acquire()  # the OS released the file lock when the process ended
    finally:
        lock.release()
