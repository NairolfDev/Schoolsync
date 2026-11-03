from datetime import datetime, timezone
from pathlib import Path

from schulsync import autostart_linux
from schulsync.config import Paths


def test_linux_install_creates_two_daily_timers_and_separate_google_check(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls = []
    monkeypatch.setattr(autostart_linux, "_systemctl", lambda *args: calls.append(args) or (0, ""))
    paths = Paths(tmp_path / "profile with spaces")
    result = autostart_linux.install(paths)
    assert result.ok
    folder = tmp_path / "config" / "systemd" / "user"
    timer = (folder / "schulsync.timer").read_text()
    assert timer.count("OnCalendar=") == 2
    assert "06:00:00" in timer and "18:00:00" in timer and "Persistent=true" in timer
    assert "run" in (folder / "schulsync.service").read_text()
    assert "google-auth-check" in (folder / "schulsync-google-login.service").read_text()
    assert "OnCalendar=hourly" in (folder / "schulsync-google-login.timer").read_text()
    assert ("enable", "--now", "schulsync.timer", "schulsync-google-login.timer") in calls
    assert ("restart", "schulsync.timer", "schulsync-google-login.timer") in calls


def test_linux_retry_timer_has_one_exact_time_and_is_removed_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls = []
    monkeypatch.setattr(autostart_linux, "_systemctl", lambda *args: calls.append(args) or (0, ""))
    paths = Paths(tmp_path / "profile")
    due = datetime(2026, 11, 3, 12, 25, 30, 500, tzinfo=timezone.utc)
    autostart_linux.schedule_retry(paths, due)
    folder = tmp_path / "config" / "systemd" / "user"
    timer = (folder / "schulsync-retry.timer").read_text()
    assert "OnCalendar=2026-11-03 12:25:31 UTC" in timer
    assert "Persistent=true" in timer
    assert ("enable", "--now", "schulsync-retry.timer") in calls
    assert ("restart", "schulsync-retry.timer") in calls
    autostart_linux.schedule_retry(paths, None)
    assert not (folder / "schulsync-retry.timer").exists()
    assert ("disable", "--now", "schulsync-retry.timer") in calls


def test_linux_reports_systemd_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(autostart_linux, "_systemctl", lambda *args: (1, "kein user service"))
    result = autostart_linux.install(Paths(tmp_path / "profile"))
    assert not result.ok and "systemd" in result.details.lower()


def test_unit_escapes_specifiers_and_quotes():
    text = autostart_linux._quote('/tmp/a %N/$HOME "test"')
    assert "%%N" in text and "$$HOME" in text and '\\"test\\"' in text


def test_appimage_timer_uses_stable_appimage_path(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "SchulSync.AppImage"))
    service = autostart_linux._service(Paths(tmp_path), "run --auto")
    assert autostart_linux._quote(str((tmp_path / "SchulSync.AppImage").resolve())) in service
    assert "Environment=APPIMAGE_EXTRACT_AND_RUN=1" in service
