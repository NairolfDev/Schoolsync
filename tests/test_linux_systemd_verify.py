import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from schulsync import autostart_linux
from schulsync.config import Paths


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("systemd-analyze"), reason="Linux systemd-analyze required")
def test_generated_user_units_validate_with_systemd(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(autostart_linux, "_systemctl", lambda *args: (0, ""))
    paths = Paths(tmp_path / "profile with spaces")
    assert autostart_linux.install(paths).ok
    autostart_linux.schedule_retry(paths, datetime.now(timezone.utc) + timedelta(hours=1))
    directory = tmp_path / "config" / "systemd" / "user"
    files = sorted(str(p) for p in directory.iterdir() if p.suffix in (".service", ".timer"))
    done = subprocess.run(["systemd-analyze", "verify", *files], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
