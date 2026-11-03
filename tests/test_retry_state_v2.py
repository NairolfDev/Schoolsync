from datetime import datetime, timedelta, timezone
from schulsync.config import Paths
from schulsync import retry_state


def test_separate_errors_three_failures_and_reset(tmp_path):
    paths = Paths(tmp_path)
    now = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
    a, b = 'webuntis:auth', 'tasks:network'
    first = retry_state.record(paths, {a, b}, now)
    assert first['counts'] == {a: 1, b: 1}
    assert first['next_retry'] == (now + timedelta(hours=1)).isoformat()
    retry_state.record(paths, {a}, now + timedelta(hours=1))
    third = retry_state.record(paths, {a, b}, now + timedelta(hours=2))
    assert third['counts'] == {a: 3, b: 1}
    retry_state.mark_notified(paths, {a})
    assert retry_state.read(paths)['notified'] == [a]
    success = retry_state.record(paths, set(), now + timedelta(hours=3))
    assert success['counts'] == {} and success['next_retry'] is None
    assert success['last_success'] == (now + timedelta(hours=3)).isoformat()


def test_error_key_never_contains_unknown_external_data():
    assert retry_state.normalize_error('webuntis', 'auth') == 'webuntis:auth'
    assert retry_state.normalize_error('webuntis', 'password=secret') == 'webuntis:other'


def test_manual_runs_wait_until_third_failure_and_retries_due(tmp_path):
    from schulsync.config import Config, RetryConfig
    from schulsync.errors import TransientError
    from schulsync.models import RunMode, Source
    from tests.support.engine_harness import Env
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1)), failures_before_report=None)
    notifications = []
    def attempt(mode):
        env.adapters[Source.WEBUNTIS].raise_next(TransientError('timeout'))
        return env.run(mode, notify=lambda *args: notifications.append(args) or True)
    attempt(RunMode.MANUAL)
    assert not env.opened and not notifications
    env.advance(minutes=30)
    calls = env.fetch_calls(Source.WEBUNTIS)
    assert env.run(RunMode.AUTO).silent
    assert env.fetch_calls(Source.WEBUNTIS) == calls
    env.advance(minutes=30)
    attempt(RunMode.AUTO)
    env.advance(hours=1)
    attempt(RunMode.AUTO)
    assert len(env.opened) == 1 and len(notifications) == 1
    assert retry_state.read(env.paths)['counts']['webuntis:transient'] == 3


def test_notify_independent_from_browser_and_no_duplicate_alert(tmp_path):
    from schulsync.config import Config, RetryConfig, ReportConfig
    from schulsync.errors import AuthError
    from schulsync.models import RunMode, Source
    from tests.support.engine_harness import Env
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1), report=ReportConfig(open_on='never', notify=True)), failures_before_report=None)
    shown = []
    for i in range(4):
        env.adapters[Source.WEBUNTIS].raise_next(AuthError('rejected'))
        env.run(RunMode.MANUAL, notify=lambda *args: shown.append(args) or True)
        env.advance(hours=1)
    assert not env.opened and len(shown) == 1
    assert retry_state.read(env.paths)['counts']['webuntis:auth'] == 4


def test_unwritable_counter_suppresses_early_alert(tmp_path, monkeypatch):
    from schulsync.errors import TransientError
    from schulsync.models import RunMode, Source
    from tests.support.engine_harness import Env
    env = Env(tmp_path)
    env.adapters[Source.WEBUNTIS].raise_next(TransientError('failed'), times=4)
    monkeypatch.setattr(retry_state, 'record', lambda *args: (_ for _ in ()).throw(OSError('disk')))
    shown = []
    env.run(RunMode.MANUAL, notify=lambda *args: shown.append(args) or True)
    assert not env.opened and not shown


def test_killed_run_is_counted_and_retried_after_one_hour(tmp_path):
    import subprocess
    import sys
    import time
    from tests.support.engine_harness import Env, NOW
    from schulsync.models import RunMode
    from schulsync.config import Paths
    paths = Paths(tmp_path / 'SchulSync')
    code = '''
import sys, time
from pathlib import Path
from tests.support.engine_harness import Env
from schulsync.models import RunMode
env = Env(Path(sys.argv[1]))
env.adapters[next(iter(env.adapters))].fetch = lambda *args: time.sleep(300)
env.run(RunMode.MANUAL)
'''
    proc = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)])
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if retry_state.read(paths).get('in_progress'):
                break
            time.sleep(.05)
        assert retry_state.read(paths).get('in_progress')
    finally:
        proc.kill()
        proc.wait(timeout=10)
    env = Env(tmp_path)
    env.now = NOW
    assert env.run(RunMode.AUTO).silent
    env.advance(hours=1)
    out = env.run(RunMode.AUTO)
    assert out.report is not None and out.exit_code == 0
    assert retry_state.read(paths)['counts'] == {}
    assert retry_state.read(paths)['in_progress'] is None


def test_third_interrupted_run_reports_before_success(tmp_path):
    from schulsync.models import RunMode
    from tests.support.engine_harness import Env
    env = Env(tmp_path)
    shown = []
    for count in (1, 2, 3):
        retry_state.begin(env.paths, env.now)
        env.advance(hours=1)
        if count < 3:
            # Model another abrupt stop after the previous interruption was recovered.
            state = retry_state.read(env.paths)
            started = datetime.fromisoformat(state['in_progress'])
            retry_state.record(env.paths, {'program:unexpected'}, started)
        else:
            out = env.run(RunMode.AUTO, notify=lambda *args: shown.append(args) or True)
            assert out.exit_code == 1 and len(env.opened) == len(shown) == 1
            assert env.run(RunMode.AUTO).exit_code == 0
    assert retry_state.read(env.paths)['counts'] == {}


def test_next_retry_is_one_actual_hour_across_dst_end(tmp_path):
    from zoneinfo import ZoneInfo
    paths = Paths(tmp_path)
    before = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo('Europe/Vienna'), fold=0)
    data = retry_state.record(paths, {'webuntis:network'}, before)
    after = datetime.fromisoformat(data['next_retry'])
    assert after.astimezone(timezone.utc) - before.astimezone(timezone.utc) == timedelta(hours=1)


def test_login_network_error_preserves_its_kind(tmp_path):
    from schulsync.config import Config, RetryConfig
    from schulsync.errors import ErrorInfo
    from schulsync.models import RunMode, Source
    from tests.support.engine_harness import Env
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1)), failures_before_report=None)
    env.adapters[Source.WEBUNTIS].login_fails(ErrorInfo('network', 'network down'))
    out = env.run(RunMode.MANUAL)
    assert out.report.sources[Source.WEBUNTIS].errors[0].code == 'network'
    assert retry_state.read(env.paths)['counts'] == {'webuntis:network': 1}


def test_retry_report_does_not_claim_failed_writes_were_previously_taken_over(tmp_path):
    from schulsync.config import Config, RetryConfig
    from schulsync.errors import BadRequestError
    from schulsync import report
    from tests.support import samples
    from tests.support.engine_harness import Env
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1)), failures_before_report=None)
    exam = samples.exam()
    env.set_items(exam)
    env.google.fail_always('insert_event', BadRequestError)
    env.google.fail_always('insert_task', BadRequestError)
    assert env.run().exit_code == 1
    env.google.clear_faults()
    env.advance(hours=1)
    retry = env.run()
    assert retry.exit_code == 0
    assert any(c.uid == exam.uid and c.status.value == 'applied' for c in retry.report.changes)
    assert not any(n.code == report.NOTICE_TAKEN_OVER and n.uid == exam.uid for n in retry.report.notices)


def test_retry_report_marks_only_the_target_that_already_succeeded(tmp_path):
    from schulsync.config import Config, RetryConfig
    from schulsync.errors import BadRequestError
    from schulsync import report
    from tests.support import samples
    from tests.support.engine_harness import Env
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1)), failures_before_report=None)
    exam = samples.exam()
    env.set_items(exam)
    env.google.fail_always('insert_task', BadRequestError)
    assert env.run().exit_code == 1
    env.google.clear_faults()
    env.advance(hours=1)
    retry = env.run()
    notices = [n for n in retry.report.notices if n.code == report.NOTICE_TAKEN_OVER and n.uid == exam.uid]
    assert [n.message for n in notices] == ['Kalender']


def test_manual_retry_reports_the_previously_applied_event_status(tmp_path):
    from dataclasses import replace
    from schulsync.config import Config, RetryConfig
    from schulsync.errors import BadRequestError, NetworkError
    from schulsync.models import ItemStatus, RunMode, Source
    from schulsync import report
    from tests.support import samples
    from tests.support.engine_harness import Env
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1)), failures_before_report=None)
    exam = samples.exam()
    env.set_items(exam)
    env.adapters[Source.EDUVIDUAL].raise_next(NetworkError('network'))
    assert env.run().exit_code == 1
    env.set_items(replace(exam, status=ItemStatus.CANCELLED))
    env.google.fail_always('patch_event', BadRequestError)
    retry = env.run(RunMode.MANUAL)
    notices = [n for n in retry.report.notices if n.code == report.NOTICE_TAKEN_OVER and n.uid == exam.uid]
    assert len(notices) == 1
    assert 'Kalender (abgesagt)' not in notices[0].message
    assert 'Kalender' in notices[0].message
