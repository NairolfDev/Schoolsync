from datetime import datetime, timezone, timedelta

from schulsync import auth_monitor
from schulsync.config import Config, GoogleConfig, Paths
from schulsync.errors import GoogleAuthError, NetworkError
from schulsync.secrets_store import MemorySecretStore

CFG = Config(google=GoogleConfig(calendar_id='calendar', tasklist_id='tasks'))


def test_expired_grant_prompts_once_and_saves_verified_credentials(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    secrets = MemorySecretStore({'google_client': 'client', 'google_token': 'old'})
    monkeypatch.setattr(auth_monitor, 'load_credentials', lambda *a, **k: (_ for _ in ()).throw(GoogleAuthError('expired', detail='invalid_grant')))
    saved = []
    monkeypatch.setattr(auth_monitor, 'save_credentials', lambda creds, store: saved.append(creds))
    monkeypatch.setattr(auth_monitor, '_verify', lambda creds, cfg: None)
    prompts = []
    def authorize(client):
        prompts.append(client)
        return object()
    assert auth_monitor.run(paths, secrets, CFG, authorize) == 0
    assert prompts == ['client'] and len(saved) == 1


def test_network_error_does_not_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_monitor, 'load_credentials', lambda *a, **k: (_ for _ in ()).throw(NetworkError('offline')))
    assert auth_monitor.run(Paths(tmp_path), MemorySecretStore({}), Config(), lambda _: (_ for _ in ()).throw(AssertionError())) == 0


def test_cancelled_prompt_is_throttled(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    secrets = MemorySecretStore({'google_client': 'client', 'google_token': 'old'})
    monkeypatch.setattr(auth_monitor, 'load_credentials', lambda *a, **k: (_ for _ in ()).throw(GoogleAuthError('expired', detail='invalid_grant')))
    prompts = []
    def cancel(client):
        prompts.append(client)
        raise GoogleAuthError('cancelled')
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert auth_monitor.run(paths, secrets, CFG, cancel, now=now) == 1
    assert auth_monitor.run(paths, secrets, CFG, cancel, now=now + timedelta(hours=1)) == 0
    assert len(prompts) == 1


def test_invalid_client_does_not_trigger_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_monitor, 'load_credentials', lambda *a, **k: (_ for _ in ()).throw(GoogleAuthError('client', detail='invalid_client')))
    assert auth_monitor.run(Paths(tmp_path), MemorySecretStore({'google_client': 'client'}), Config(), lambda _: (_ for _ in ()).throw(AssertionError())) == 1


def test_wrong_account_is_never_saved(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    secrets = MemorySecretStore({'google_client': 'client', 'google_token': 'old'})
    monkeypatch.setattr(auth_monitor, 'load_credentials', lambda *a, **k: (_ for _ in ()).throw(GoogleAuthError('expired', detail='invalid_grant')))
    monkeypatch.setattr(auth_monitor, '_verify', lambda *a: (_ for _ in ()).throw(GoogleAuthError('wrong account')))
    monkeypatch.setattr(auth_monitor, 'save_credentials', lambda *a: (_ for _ in ()).throw(AssertionError('must not save')))
    assert auth_monitor.run(paths, secrets, CFG, lambda _: object()) == 1


def test_missing_configured_targets_never_starts_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_monitor, 'load_credentials', lambda *a, **k: (_ for _ in ()).throw(GoogleAuthError('expired', detail='invalid_grant')))
    assert auth_monitor.run(Paths(tmp_path), MemorySecretStore({'google_client': 'client'}), Config(), lambda _: (_ for _ in ()).throw(AssertionError())) == 1


def test_cli_auth_check_skips_if_synchronization_lock_is_held(tmp_path, monkeypatch):
    from schulsync import cli
    from schulsync.lock import ProcessLock
    paths = Paths(tmp_path)
    paths.ensure()
    lock = ProcessLock(paths.lock)
    assert lock.acquire(blocking=False)
    try:
        monkeypatch.setattr(auth_monitor, 'run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not run')))
        env = cli.CliEnv(paths=paths, secrets=MemorySecretStore({}), say=lambda *_: None)
        assert cli.main(['google-auth-check', '--auto'], env=env) == 0
    finally:
        lock.release()


def test_chrome_browser_is_preferred_when_installed(tmp_path, monkeypatch):
    from schulsync import google_auth
    chrome = tmp_path / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b'')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('ProgramFiles', str(tmp_path / 'missing'))
    monkeypatch.setenv('ProgramFiles(x86)', str(tmp_path / 'missing2'))
    assert google_auth._chrome_executable() == str(chrome)


def test_chrome_falls_back_to_default_browser_when_not_installed(tmp_path, monkeypatch):
    from schulsync import google_auth
    for key in ('LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)'):
        monkeypatch.setenv(key, str(tmp_path))
    assert google_auth._chrome_executable() is None
