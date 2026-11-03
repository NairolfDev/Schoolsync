import json

import pytest

from schulsync.errors import ConfigError
from schulsync.linux_secrets import SecretServiceStore


class Backend:
    def __init__(self): self.values = {}
    def get_password(self, service, user): return self.values.get((service, user))
    def set_password(self, service, user, value): self.values[(service, user)] = value


def test_secret_service_store_keeps_multiple_secrets_only_in_backend(tmp_path):
    backend = Backend()
    store = SecretServiceStore(tmp_path / "secrets.dat", backend=backend)
    store.set("google_token", "sensitive-token")
    store.set("webuntis_password", "secret-password")
    assert store.get("google_token") == "sensitive-token"
    assert store.names() == ["google_token", "webuntis_password"]
    store.delete("google_token")
    assert store.get("google_token") is None
    assert not (tmp_path / "secrets.dat").exists()
    assert "secret-password" in next(iter(backend.values.values()))


def test_secret_service_invalid_data_fails_without_fallback(tmp_path):
    backend = Backend()
    store = SecretServiceStore(tmp_path / "secrets.dat", backend=backend)
    backend.set_password(store.service, "secrets", "not-json")
    with pytest.raises(ConfigError): store.get("google_token")


def test_secret_service_errors_never_expose_secrets(tmp_path):
    class Broken(Backend):
        def set_password(self, *args): raise RuntimeError("secret-password")
    with pytest.raises(ConfigError) as error:
        SecretServiceStore(tmp_path / "secrets.dat", backend=Broken()).set("key", "secret-password")
    assert "secret-password" not in str(error.value)
