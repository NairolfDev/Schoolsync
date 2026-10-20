"""`schulsync update` (GitHub-Release prüfen, Prüfsumme, .exe austauschen) und die Installation der .exe. Ohne Netz und ohne Registry-Zugriff."""

from __future__ import annotations

import hashlib

import pytest

from schulsync import __version__, frozen, update
from schulsync.errors import NetworkError, SchemaError, SchulSyncError


class Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload, self.content = status, payload, content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError("x")


def release(tag="v9.0.0", exe="https://github.com/o/r/releases/download/v9.0.0/SchulSync-Setup.exe", sha=None):
    sha = sha or exe + ".sha256"
    return {"tag_name": tag, "assets": [
        {"name": "SchulSync-Setup.exe", "browser_download_url": exe},
        {"name": "SchulSync-Setup.exe.sha256", "browser_download_url": sha}]}


def fake_get(api, files):
    def get(url, **kw):
        if url.startswith("https://api.github.com/"):
            return api
        return files[url]
    return get


NEW = b"MZ-new-version"
URL = "https://github.com/o/r/releases/download/v9.0.0/SchulSync-Setup.exe"


def files(content=NEW, digest=None):
    return {URL: Resp(content=content), URL + ".sha256": Resp(content=((digest or hashlib.sha256(content).hexdigest()) + " *SchulSync-Setup.exe\n").encode())}


def test_version_comparison():
    assert update.parse_version("v1.10.0") > update.parse_version("1.9.9") > update.parse_version("v1.0")
    with pytest.raises(SchemaError):
        update.parse_version("neu")


def test_up_to_date_changes_nothing(tmp_path):
    out = []
    exe = tmp_path / "schulsync.exe"
    exe.write_bytes(b"old")
    code = update.run_update(out.append, get=fake_get(Resp(payload=release(tag="v" + __version__)), {}), current=exe, is_frozen=True)
    assert code == 0 and exe.read_bytes() == b"old" and "neuesten Stand" in " ".join(out)


def test_check_only_reports_a_new_version_without_installing(tmp_path):
    exe = tmp_path / "schulsync.exe"
    exe.write_bytes(b"old")
    out = []
    assert update.run_update(out.append, check_only=True, get=fake_get(Resp(payload=release()), {}), current=exe, is_frozen=True) == 10
    assert exe.read_bytes() == b"old"


def test_update_replaces_the_exe_after_the_checksum_matches(tmp_path):
    exe = tmp_path / "schulsync.exe"
    exe.write_bytes(b"old")
    out = []
    assert update.run_update(out.append, get=fake_get(Resp(payload=release()), files()), current=exe, is_frozen=True) == 0
    assert exe.read_bytes() == NEW and (tmp_path / "schulsync.exe.old").read_bytes() == b"old"
    assert not (tmp_path / "schulsync.exe.new").exists() and "aktualisiert" in " ".join(out)


def test_wrong_checksum_installs_nothing(tmp_path):
    exe = tmp_path / "schulsync.exe"
    exe.write_bytes(b"old")
    with pytest.raises(SchulSyncError, match="Prüfsumme"):
        update.run_update(lambda t: None, get=fake_get(Resp(payload=release()), files(digest="0" * 64)), current=exe, is_frozen=True)
    assert exe.read_bytes() == b"old"


def test_source_install_is_pointed_to_git_pull(tmp_path):
    out = []
    assert update.run_update(out.append, get=fake_get(Resp(payload=release()), files()), current=tmp_path / "x", is_frozen=False) == 1
    assert "git pull" in " ".join(out)


@pytest.mark.parametrize("bad", ["http://github.com/o/r/a.exe", "https://evil.example.com/a.exe", "https://github.com.evil.com/a.exe"])
def test_only_https_github_download_hosts_are_accepted(bad):
    with pytest.raises(SchemaError):
        update.latest_release(fake_get(Resp(payload=release(exe=bad, sha=URL + ".sha256")), {}))


def test_missing_release_or_asset_is_a_clear_error():
    with pytest.raises(SchulSyncError, match="noch keine"):
        update.latest_release(fake_get(Resp(status=404), {}))
    with pytest.raises(SchemaError):
        update.latest_release(fake_get(Resp(payload={"tag_name": "v9", "assets": []}), {}))


def test_network_failure_is_reported_not_raised_raw():
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("x")

    with pytest.raises(NetworkError):
        update.latest_release(boom)


def test_a_failed_swap_restores_the_old_exe(tmp_path, monkeypatch):
    exe = tmp_path / "schulsync.exe"
    exe.write_bytes(b"old")
    real = type(exe).replace

    def flaky(self, target):
        if self.name.endswith(".new"):
            raise OSError("gesperrt")
        return real(self, target)

    monkeypatch.setattr(type(exe), "replace", flaky)
    with pytest.raises(OSError):
        update.replace_exe(exe, NEW)
    assert exe.read_bytes() == b"old" and not (tmp_path / "schulsync.exe.new").exists()


def test_install_copies_the_exe_and_adds_the_path_once(tmp_path, monkeypatch):
    src = tmp_path / "Downloads" / "SchulSync-Setup.exe"
    src.parent.mkdir()
    src.write_bytes(b"exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(frozen, "exe_path", lambda: src)
    added = []
    monkeypatch.setattr(frozen, "add_to_user_path", lambda p: added.append(p) or True)
    out = []
    target = frozen.install(out.append)
    assert target == tmp_path / "Local" / "Programs" / "SchulSync" / "schulsync.exe" and target.read_bytes() == b"exe"
    assert added == [target.parent] and "NEUES PowerShell-Fenster" in " ".join(out)


def test_the_installed_exe_autostart_command_is_the_exe_itself(monkeypatch, tmp_path):
    from schulsync import autostart

    exe = tmp_path / "schulsync.exe"
    monkeypatch.setattr(frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(frozen, "exe_path", lambda: exe)
    assert autostart.default_command() == (str(exe), "run --auto")


def test_the_installed_exe_repairs_a_missing_path_entry_on_every_start(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    folder = tmp_path / "Programs" / "SchulSync"
    folder.mkdir(parents=True)
    added = []
    monkeypatch.setattr(frozen, "add_to_user_path", lambda p: added.append(p) or True)
    monkeypatch.setattr(frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(frozen, "exe_path", lambda: folder.resolve() / "schulsync.exe")
    frozen.ensure_path()
    assert added == [folder]
    added.clear()
    monkeypatch.setattr(frozen, "exe_path", lambda: tmp_path / "Downloads" / "SchulSync-Setup.exe")  # not installed: nothing
    frozen.ensure_path()
    assert added == []
