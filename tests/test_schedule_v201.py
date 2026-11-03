from datetime import datetime, timezone
import xml.etree.ElementTree as ET

from schulsync import autostart

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_normal_schedule_has_only_two_daily_triggers():
    xml = autostart.build_task_xml(user_sid_or_name="PC\\User", command="app.exe", arguments="run --auto",
                                   working_dir="C:\\Data", run_times=("07:15", "17:45"))
    root = ET.fromstring(xml)
    triggers = root.findall("t:Triggers/*", NS)
    assert len(triggers) == 2
    assert [t.find("t:StartBoundary", NS).text[-8:] for t in triggers] == ["07:15:00", "17:45:00"]
    assert not root.findall(".//t:Repetition", NS)
    assert not root.findall(".//t:LogonTrigger", NS)


def test_retry_task_has_one_due_time_without_minute_polling():
    due = datetime(2026, 11, 3, 12, 25, tzinfo=timezone.utc)
    xml = autostart.build_retry_task_xml(user_sid_or_name="PC\\User", command="app.exe", arguments="run --auto",
                                         working_dir="C:\\Data", due=due)
    root = ET.fromstring(xml)
    assert root.find("t:Triggers/t:TimeTrigger/t:StartBoundary", NS).text == due.isoformat()
    assert root.find("t:Triggers/t:TimeTrigger/t:EndBoundary", NS) is not None
    assert not root.findall(".//t:Repetition", NS)
    assert root.find("t:Settings/t:StartWhenAvailable", NS).text == "true"


def test_retry_is_armed_while_running_and_cleared_on_success(tmp_path):
    from datetime import timedelta
    from schulsync.models import RunMode
    from tests.support.engine_harness import Env
    env = Env(tmp_path)
    calls = []
    out = env.run(RunMode.MANUAL, retry_schedule=calls.append)
    assert out.exit_code == 0
    assert calls[0] == env.now.astimezone(timezone.utc) + timedelta(hours=1)
    assert calls[-1] is None


def test_failed_run_arms_retry_at_failure_time(tmp_path):
    from datetime import timedelta
    from schulsync.errors import AuthError
    from schulsync.models import RunMode, Source
    from tests.support.engine_harness import Env
    env = Env(tmp_path)
    env.adapters[Source.WEBUNTIS].raise_next(AuthError("login"))
    calls = []
    out = env.run(RunMode.MANUAL, retry_schedule=calls.append)
    assert out.exit_code != 0
    assert calls[-1].astimezone(timezone.utc) == env.now.astimezone(timezone.utc) + timedelta(hours=1)


def test_fallback_watcher_starts_only_at_due_times(tmp_path, monkeypatch):
    from schulsync.config import Paths
    paths = Paths(tmp_path)
    monkeypatch.setattr(autostart, "shortcut_path", lambda: tmp_path / "startup.lnk")
    autostart._fallback_watch(paths, "app.exe", "run --auto", ("07:15", "17:45"))
    script = (tmp_path / "autostart-watch.ps1").read_text(encoding="utf-8-sig")
    assert "if ($runDue)" in script
    assert "next_retry" in script and "in_progress" in script
    assert (tmp_path / "autostart-times.json").exists()


def test_retry_schedule_created_only_if_main_task_exists(tmp_path, monkeypatch):
    from schulsync.config import Paths
    paths = Paths(tmp_path)
    calls = []
    monkeypatch.setattr(autostart, "_query_task", lambda name=autostart.TASK_NAME: autostart._Result(0, "ok"))
    monkeypatch.setattr(autostart, "_try_task", lambda xml, name: calls.append((xml, name)))
    monkeypatch.setattr(autostart, "current_user", lambda: "PC\\User")
    monkeypatch.setattr(autostart, "default_command", lambda: ("app.exe", "run --auto"))
    due = datetime(2026, 11, 3, 12, 25, tzinfo=timezone.utc)
    autostart.schedule_retry(paths, due)
    assert calls[0][1] == "SchulSync-Retry"
    assert due.isoformat() in calls[0][0]
    monkeypatch.setattr(autostart, "_query_task", lambda name=autostart.TASK_NAME: autostart._Result(1, "cannot find the file"))
    autostart.schedule_retry(paths, due)
    assert len(calls) == 1


def test_success_removes_existing_retry_task(tmp_path, monkeypatch):
    from schulsync.config import Paths
    calls = []
    monkeypatch.setattr(autostart, "_query_task", lambda name: autostart._Result(0, "ok"))
    monkeypatch.setattr(autostart, "_schtasks", lambda *args: calls.append(args) or autostart._Result(0, "ok"))
    autostart.schedule_retry(Paths(tmp_path), None)
    assert calls == [("/Delete", "/TN", "SchulSync-Retry", "/F")]


def test_runner_uses_user_configured_run_times_for_gate(tmp_path):
    from schulsync.config import Config, ScheduleConfig
    from schulsync.models import RunMode, Source
    from tests.support.engine_harness import Env
    env = Env(tmp_path, cfg=Config(schedule=ScheduleConfig(run_times=("07:00", "12:00"))))
    env.now = env.now.replace(hour=6, minute=30)
    assert env.run(RunMode.AUTO).exit_code == 0
    env.now = env.now.replace(hour=7, minute=15)
    assert env.run(RunMode.AUTO).exit_code == 0
    assert env.fetch_calls(Source.WEBUNTIS) == 2


def test_google_login_check_has_separate_hourly_task():
    xml = autostart.build_google_check_task_xml(user_sid_or_name="PC\\User", command="app.exe",
                                                arguments="google-auth-check", working_dir="C:\\Data")
    root = ET.fromstring(xml)
    assert root.find("t:Triggers/t:CalendarTrigger/t:Repetition/t:Interval", NS).text == "PT1H"
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "google-auth-check"


def test_google_login_check_gets_own_hidden_launcher(tmp_path, monkeypatch):
    from schulsync.config import Paths
    paths = Paths(tmp_path)
    captured = []
    monkeypatch.setattr(autostart.frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(autostart, "default_command", lambda: ("app.exe", "run --auto"))
    monkeypatch.setattr(autostart, "current_user", lambda: "PC\\User")
    monkeypatch.setattr(autostart, "_try_task", lambda xml, name: captured.append((xml, name)))
    autostart._install_google_check(paths)
    assert captured[0][1] == "SchulSync-Google-Login"
    assert "autostart-google-login.vbs" in captured[0][0]
    assert "google-auth-check --auto" in (tmp_path / "autostart-google-login.vbs").read_text(encoding="utf-16")


def test_due_retry_rearms_when_another_process_holds_lock(tmp_path):
    from datetime import timedelta
    from schulsync import retry_state
    from schulsync.lock import ProcessLock
    from schulsync.models import RunMode, Source
    from tests.support.engine_harness import Env

    env = Env(tmp_path)
    state = retry_state.record(env.paths, {'webuntis:network'}, env.now - timedelta(hours=1))
    lock = ProcessLock(env.paths.lock)
    assert lock.acquire(blocking=False)
    scheduled = []
    try:
        result = env.run(RunMode.AUTO, retry_schedule=scheduled.append)
    finally:
        lock.release()
    assert result.silent and env.fetch_calls(Source.WEBUNTIS) == 0
    assert scheduled == [env.now.astimezone(timezone.utc) + timedelta(minutes=1)]
    assert retry_state.read(env.paths)['next_retry'] == state['next_retry']


def test_fallback_retries_due_start_when_lock_was_busy(tmp_path, monkeypatch):
    from schulsync.config import Paths
    monkeypatch.setattr(autostart, 'shortcut_path', lambda: tmp_path / 'startup.lnk')
    autostart._fallback_watch(Paths(tmp_path), 'app.exe', 'run --auto')
    script = (tmp_path / 'autostart-watch.ps1').read_text(encoding='utf-8-sig')
    assert '$retryAgainAt' in script and '$now.AddSeconds(60)' in script


def test_early_auto_trigger_restores_later_retry_schedule(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace
    from schulsync import cli, retry_state
    from schulsync.config import Paths
    from schulsync.secrets_store import MemorySecretStore

    paths = Paths(tmp_path)
    paths.ensure()
    now = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
    retry_state.record(paths, {'webuntis:network'}, now)
    scheduled = []
    autostart = SimpleNamespace(schedule_retry=lambda p, due: scheduled.append((p, due)))
    env = cli.CliEnv(paths=paths, secrets=MemorySecretStore({}), clock=lambda: now,
                     autostart=autostart, say=lambda *_: None)
    assert cli.main(['run', '--auto'], env=env) == 0
    assert scheduled == [(paths, now + timedelta(hours=1))]


def test_fallback_keeps_due_daily_run_during_long_login_or_sleep(tmp_path, monkeypatch):
    import re
    import subprocess
    from schulsync.config import Paths
    monkeypatch.setattr(autostart, 'shortcut_path', lambda: tmp_path / 'startup.lnk')
    autostart._fallback_watch(Paths(tmp_path), 'app.exe', 'run --auto')
    script = (tmp_path / 'autostart-watch.ps1').read_text(encoding='utf-8-sig')
    function = re.search(r'(?ms)^function Test-DailyDue\b.*?^}', script).group(0)
    check = function + "\n" + '''
if (-not (Test-DailyDue ([datetime]'2026-11-03T06:00:00') ([datetime]'2026-11-03T06:10:00') @('06:05','18:00'))) { exit 1 }
if (-not (Test-DailyDue ([datetime]'2026-11-03T23:55:00') ([datetime]'2026-11-04T00:10:00') @('23:59','18:00'))) { exit 2 }
if (Test-DailyDue ([datetime]'2026-11-03T06:06:00') ([datetime]'2026-11-03T06:10:00') @('06:05','18:00')) { exit 3 }
'''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', check], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
