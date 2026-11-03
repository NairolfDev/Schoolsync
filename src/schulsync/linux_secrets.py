"""Encrypted Linux Secret Service storage; never falls back to plaintext files."""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path

from .errors import ConfigError
from .lock import ProcessLock

_LOCK = threading.RLock()
_HINT = "Linux Secret Service entsperren und Python-Paket 'keyring' installieren."


class SecretServiceStore:
    """All credentials for one data directory in one Secret Service item."""

    def __init__(self, path: Path, *, backend=None):
        self.path = Path(path)
        self.service = "SchulSync/" + hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()
        self._backend = backend

    def _service(self):
        if self._backend is None:
            try:
                from keyring.backends.SecretService import Keyring
                self._backend = Keyring()
            except Exception as exc:  # no plaintext fallback, even with a misconfigured keyring
                raise ConfigError("Linux Secret Service ist nicht verfügbar.", hint=_HINT, detail=type(exc).__name__) from None
        return self._backend

    def _read(self) -> dict[str, str]:
        try:
            raw = self._service().get_password(self.service, "secrets")
            if raw is None:
                return {}
            data = json.loads(raw)
            if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
                raise ValueError("invalid secret format")
            return data
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError("Zugangsdaten im Linux Secret Service sind nicht lesbar.", hint=_HINT, detail=type(exc).__name__) from None

    @contextmanager
    def _writer(self):
        with _LOCK:
            lock = ProcessLock(self.path.with_name(self.path.name + ".lock"))
            if not lock.acquire(blocking=True, timeout=30):
                raise ConfigError("Zugangsdaten sind gerade gesperrt.", hint="Bitte später erneut versuchen.")
            try:
                yield
            finally:
                lock.release()

    def _write(self, data: dict[str, str]) -> None:
        try:
            self._service().set_password(self.service, "secrets", json.dumps(data, ensure_ascii=False))
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError("Zugangsdaten konnten nicht geschützt gespeichert werden.", hint=_HINT, detail=type(exc).__name__) from None

    def get(self, name: str) -> str | None:
        return self._read().get(name)

    def set(self, name: str, value: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("Der Name des Geheimnisses darf nicht leer sein.")
        with self._writer():
            data = self._read()
            data[name] = value
            self._write(data)

    def delete(self, name: str) -> None:
        with self._writer():
            data = self._read()
            if name in data:
                del data[name]
                self._write(data)

    def names(self) -> list[str]:
        return sorted(self._read())
