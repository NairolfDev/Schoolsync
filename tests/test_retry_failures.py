"""Run-level retry behaviour: each error series is reported after its third occurrence."""
from schulsync import retry_state
from schulsync.config import Config, ReportConfig, RetryConfig
from schulsync.errors import AuthError, TransientError
from schulsync.models import RunMode, Source
from tests.support.engine_harness import Env


def test_different_errors_have_independent_series(tmp_path):
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1)), failures_before_report=None)
    shown = []
    def run(error):
        env.adapters[Source.WEBUNTIS].raise_next(error)
        result = env.run(RunMode.MANUAL, notify=lambda *a: shown.append(a) or True)
        env.advance(hours=1)
        return result
    run(TransientError('network'))
    run(AuthError('auth'))
    run(TransientError('network'))
    assert retry_state.read(env.paths)['counts'] == {'webuntis:transient': 1}
    assert not env.opened and not shown
    run(TransientError('network'))
    assert not env.opened
    run(TransientError('network'))
    assert len(env.opened) == len(shown) == 1


def test_preview_does_not_modify_retry_state(tmp_path):
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1)), failures_before_report=None)
    env.adapters[Source.WEBUNTIS].raise_next(TransientError('network'), times=2)
    env.run(RunMode.MANUAL)
    before = retry_state.read(env.paths)
    env.run(RunMode.PREVIEW)
    assert retry_state.read(env.paths) == before and not env.opened


def test_success_clears_series(tmp_path):
    env = Env(tmp_path, cfg=Config(retry=RetryConfig(max_attempts=1)), failures_before_report=None)
    env.adapters[Source.WEBUNTIS].raise_next(TransientError('network'))
    env.run(RunMode.MANUAL)
    assert retry_state.read(env.paths)['counts'] == {'webuntis:transient': 1}
    assert env.run(RunMode.MANUAL).exit_code == 0
    state = retry_state.read(env.paths)
    assert state['counts'] == {} and state['next_retry'] is None and state['last_success']


def test_notify_disabled_opens_report_after_third_error(tmp_path):
    cfg = Config(retry=RetryConfig(max_attempts=1), report=ReportConfig(notify=False))
    env = Env(tmp_path, cfg=cfg, failures_before_report=None)
    notifications = []
    env.adapters[Source.WEBUNTIS].raise_next(TransientError('network'), times=3)
    for _ in range(3):
        env.run(RunMode.MANUAL, notify=lambda *a: notifications.append(a) or True)
    assert len(env.opened) == 1 and not notifications
