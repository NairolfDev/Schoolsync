"""Linux entry point and default paths must not use Windows services."""
from argparse import Namespace
from types import SimpleNamespace

from schulsync import autostart_linux, cli, config
from schulsync.linux_secrets import SecretServiceStore


def test_linux_defaults_use_xdg_and_secret_service(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.delenv("SCHULSYNC_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    paths = config.Paths.default()
    assert paths.home == tmp_path / "schulsync"
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux"))
    env = cli.CliEnv(paths=paths)
    assert env.autostart is autostart_linux
    assert isinstance(cli._Session(Namespace(), env).secrets, SecretServiceStore)


def test_linux_entry_passes_arguments_directly_to_main(monkeypatch):
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux", argv=["schulsync", "status"]))
    monkeypatch.setattr(cli.frozen, "remove_old_exe", lambda: (_ for _ in ()).throw(AssertionError("Windows path used")))
    monkeypatch.setattr(cli, "main", lambda argv: 42 if argv == ["status"] else 9)
    assert cli.entry() == 42


def test_linux_rejects_windows_installer_and_update(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux"))
    out = []
    from schulsync.secrets_store import MemorySecretStore
    env = cli.CliEnv(say=out.append, paths=config.Paths(tmp_path), secrets=MemorySecretStore())
    for command in ("install", "uninstall", "update"):
        assert cli.main([command], env=env) != 0
    assert any("Linux" in line for line in out)
