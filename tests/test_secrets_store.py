"""Tests for schulsync.secrets_store. DPAPI tests run against the real Windows API (no mocks)."""

from __future__ import annotations

import base64
import json
import sys

import pytest

from schulsync import redact
from schulsync.errors import ConfigError
from schulsync.secrets_store import DpapiSecretStore, MemorySecretStore, SecretStore, register_loaded_secrets

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI exists only on Windows")


@pytest.fixture(autouse=True)
def _clean_redact():
    redact.clear_registered_secrets()
    yield
    redact.clear_registered_secrets()


# --------------------------------------------------------------------------- memory store


def test_memory_store_roundtrip_and_protocol():
    store: SecretStore = MemorySecretStore()
    assert store.get("a") is None
    store.set("b", "2")
    store.set("a", "1")
    assert store.get("a") == "1"
    assert store.names() == ["a", "b"]
    store.delete("a")
    store.delete("gibt-es-nicht")  # no error
    assert store.names() == ["b"]
    with pytest.raises(ValueError):
        store.set("", "x")


# --------------------------------------------------------------------------- DPAPI store


@windows_only
def test_dpapi_roundtrip_incl_unicode_empty_and_large(tmp_path):
    store = DpapiSecretStore(tmp_path / "secrets.dat")
    values = {
        "eduvidual_password": "hunter2-dummy",
        "webuntis_password": "Pässwörd-ß-€-日本-dummy",
        "empty": "",
        "google_client": json.dumps({"installed": {"client_secret": "GOCSPX-DummyDummyDummy1234"}}),
        "large": "x" * 200_000,
    }
    for k, v in values.items():
        store.set(k, v)
    fresh = DpapiSecretStore(tmp_path / "secrets.dat")  # new object: nothing cached
    for k, v in values.items():
        assert fresh.get(k) == v, k
    assert fresh.names() == sorted(values)
    assert fresh.get("missing") is None


@windows_only
def test_dpapi_never_writes_plaintext(tmp_path):
    path = tmp_path / "secrets.dat"
    store = DpapiSecretStore(path)
    secret = "hunter2-dummy-unique-9f3a"
    store.set("eduvidual_password", secret)
    store.set("other", secret)
    raw = path.read_bytes()
    assert secret.encode() not in raw
    assert secret.encode("utf-16-le") not in raw
    assert base64.b64encode(secret.encode()) not in raw
    # file format: {name: base64(blob)}, blobs differ per entry (DPAPI uses a random salt)
    data = json.loads(raw)
    assert set(data) == {"eduvidual_password", "other"}
    assert data["eduvidual_password"] != data["other"]
    for blob in data.values():
        assert secret.encode() not in base64.b64decode(blob)
    assert not list(tmp_path.glob("*.tmp"))  # atomic write leaves no temp file


@windows_only
def test_dpapi_overwrite_delete_and_isolation_between_entries(tmp_path):
    store = DpapiSecretStore(tmp_path / "secrets.dat")
    store.set("a", "one-dummy")
    store.set("b", "two-dummy")
    store.set("a", "three-dummy")
    assert store.get("a") == "three-dummy" and store.get("b") == "two-dummy"
    store.delete("a")
    store.delete("a")
    assert store.names() == ["b"]
    assert store.get("a") is None


@windows_only
def test_dpapi_creates_missing_parent_directory(tmp_path):
    store = DpapiSecretStore(tmp_path / "neu" / "unter" / "secrets.dat")
    store.set("a", "dummy-wert")
    assert store.get("a") == "dummy-wert"


@windows_only
def test_dpapi_missing_file_is_empty(tmp_path):
    store = DpapiSecretStore(tmp_path / "secrets.dat")
    assert store.names() == [] and store.get("a") is None
    store.delete("a")  # no error, no file created
    assert not (tmp_path / "secrets.dat").exists()


@windows_only
@pytest.mark.parametrize(
    "bad_entry",
    [
        "!!!kein-base64!!!",
        base64.b64encode(b"das ist kein DPAPI-Blob").decode(),  # e.g. blob of another user/machine
        "",
    ],
)
def test_undecryptable_entry_gives_german_config_error(tmp_path, bad_entry):
    path = tmp_path / "secrets.dat"
    store = DpapiSecretStore(path)
    store.set("good", "gut-dummy")
    data = json.loads(path.read_text())
    data["bad"] = bad_entry
    path.write_text(json.dumps(data))
    assert store.get("good") == "gut-dummy"  # other entries stay usable
    with pytest.raises(ConfigError) as exc:
        store.get("bad")
    assert "bad" in exc.value.message and "entschlüsselt" in exc.value.message
    assert "schulsync setup" in (exc.value.hint or "")
    store.set("bad", "neu-dummy")  # setup can repair the entry
    assert store.get("bad") == "neu-dummy"


@windows_only
def test_tampered_blob_is_rejected_not_returned_as_garbage(tmp_path):
    path = tmp_path / "secrets.dat"
    store = DpapiSecretStore(path)
    store.set("a", "hunter2-dummy")
    data = json.loads(path.read_text())
    blob = bytearray(base64.b64decode(data["a"]))
    blob[-5] ^= 0xFF  # DPAPI blobs carry a MAC
    data["a"] = base64.b64encode(bytes(blob)).decode()
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError):
        store.get("a")


@windows_only
@pytest.mark.parametrize("content", [b"{kaputt", b"[1, 2]", b'{"a": 5}', b"\xff\xfe\x00"])
def test_corrupt_file_read_raises_config_error_and_set_recovers(tmp_path, content):
    path = tmp_path / "secrets.dat"
    path.write_bytes(content)
    store = DpapiSecretStore(path)
    with pytest.raises(ConfigError) as exc:
        store.get("a")
    assert "schulsync setup" in (exc.value.hint or "")
    with pytest.raises(ConfigError):
        store.names()
    store.set("a", "neu-dummy")  # file is moved aside, not silently destroyed
    assert store.get("a") == "neu-dummy"
    assert (tmp_path / "secrets.dat.corrupt").read_bytes() == content


def test_dpapi_on_non_windows_gives_config_error(tmp_path, monkeypatch):
    from schulsync import secrets_store

    monkeypatch.setattr(secrets_store.sys, "platform", "linux")
    monkeypatch.setattr(secrets_store, "_dpapi", None)
    with pytest.raises(ConfigError):
        DpapiSecretStore(tmp_path / "s.dat").set("a", "wert-dummy")


# --------------------------------------------------------------------------- redaction hookup


def test_register_loaded_secrets_registers_values_and_json_leaves():
    store = MemorySecretStore(
        {
            "eduvidual_password": "hunter2-dummy",
            "google_client": json.dumps(
                {"installed": {"client_id": "123-abc.apps.example", "client_secret": "GOCSPX-Dummy1234567"}}
            ),
            "google_token": json.dumps({"token": "ya29.dummyAccess123", "refresh_token": "1//dummyRefresh456"}),
            "empty": "",
        }
    )
    found = register_loaded_secrets(store, ["eduvidual_password", "google_client", "google_token", "empty", "missing"])
    assert found == ["eduvidual_password", "google_client", "google_token"]
    text = "pw hunter2-dummy cs GOCSPX-Dummy1234567 at ya29.dummyAccess123 rt 1//dummyRefresh456 id 123-abc.apps.example"
    out = redact.redact(text)
    for secret in ("hunter2-dummy", "GOCSPX-Dummy1234567", "ya29.dummyAccess123", "1//dummyRefresh456"):
        assert secret not in out
    assert "123-abc.apps.example" in out  # client_id is not secret and stays readable


@windows_only
def test_register_loaded_secrets_with_dpapi_store(tmp_path):
    store = DpapiSecretStore(tmp_path / "secrets.dat")
    store.set("webuntis_password", "untis-pw-dummy")
    assert register_loaded_secrets(store, ["webuntis_password", "eduvidual_password"]) == ["webuntis_password"]
    assert redact.redact("login untis-pw-dummy") == "login ***"
