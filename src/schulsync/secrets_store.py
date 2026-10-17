"""Protected storage for passwords and OAuth material.

`DpapiSecretStore` encrypts every entry on its own with Windows DPAPI (user scope) and keeps the
blobs in ``paths.secrets`` as JSON ``{name: base64(blob)}``. Plaintext never touches the disk.

DPAPI facts (Microsoft docs, CryptProtectData / CryptUnprotectData, dpapi.h, crypt32.dll):
- Without ``CRYPTPROTECT_LOCAL_MACHINE`` (we never set it) the data can typically only be decrypted
  by the same user on the same computer; a MAC guards against tampering, so a modified or foreign
  blob fails instead of returning garbage.
- ``pOptionalEntropy`` must be identical for protect and unprotect; we use a fixed app-specific value
  (it is not a secret, it only separates SchulSync blobs from other DPAPI users of the same account).
- ``CRYPTPROTECT_UI_FORBIDDEN`` (0x1) makes the call fail instead of ever showing a prompt.
- The output ``pbData`` buffer must be released with ``LocalFree``.

File protection: the file is created with default permissions inside the user's profile
(``%LOCALAPPDATA%\\SchulSync``), which Windows already restricts to the user, SYSTEM and administrators.
No extra ACL work is done; the content is encrypted anyway.

Concurrency (read-modify-write of one file): writers are serialised by an in-process lock plus an OS file lock on
``<file>.lock`` (a separate file from the program lock: ``run`` holds that one while it may refresh the Google token).
Every write goes through a temp file with a UNIQUE name and is moved into place with ``os.replace``, which Windows
refuses (PermissionError) while another handle has the target open; that is retried a few times with a short, bounded
backoff, for readers as well as for the writer. Reads take no lock: the file is always replaced atomically.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, TypeVar

from .errors import ConfigError, TransientError
from .lock import ProcessLock
from .redact import SENSITIVE_KEY_NAMES, register_secret

_ENTROPY = b"SchulSync/secrets/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

_HINT_SETUP = "Bitte 'schulsync setup' erneut ausführen, um die Zugangsdaten neu zu speichern."

# ponytail: one in-process lock for every store object (there is exactly one secrets file per program); per-path locks if
# that ever changes.
_WRITE_LOCK = threading.RLock()
_LOCK_WAIT_SECONDS = 30.0  # how long a writer waits for another writer (process) before giving up
_FILE_ATTEMPTS = 8  # tries for a file operation that Windows refuses while another handle is open
_FILE_BACKOFF = 0.025  # first pause; doubles up to _FILE_BACKOFF_MAX (about 1.6 s in total)
_FILE_BACKOFF_MAX = 0.4
T = TypeVar("T")


def _retry_busy(op: Callable[..., T], *args: Any) -> T:
    """Run a file operation; on PermissionError (sharing violation) retry with a short, bounded backoff."""
    delay = _FILE_BACKOFF
    for attempt in range(_FILE_ATTEMPTS):
        try:
            return op(*args)
        except PermissionError:
            if attempt == _FILE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, _FILE_BACKOFF_MAX)
    raise AssertionError("unreachable")  # pragma: no cover


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def names(self) -> list[str]: ...


class MemorySecretStore:
    """In-memory store for tests and previews."""

    def __init__(self, initial: dict[str, str] | None = None):
        self._data: dict[str, str] = dict(initial or {})

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def set(self, name: str, value: str) -> None:
        _check_name(name)
        self._data[name] = value

    def delete(self, name: str) -> None:
        self._data.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._data)


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("Der Name des Geheimnisses darf nicht leer sein.")


# --------------------------------------------------------------------------- DPAPI via ctypes

_dpapi = None  # (CryptProtectData, CryptUnprotectData, LocalFree, DATA_BLOB) once loaded


def _load_dpapi():
    global _dpapi
    if _dpapi is not None:
        return _dpapi
    if sys.platform != "win32":
        raise ConfigError(
            "Der geschützte Geheimnisspeicher (Windows-DPAPI) ist nur unter Windows verfügbar.",
            hint="SchulSync ist für Windows gedacht.",
        )
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_p = ctypes.POINTER(DATA_BLOB)
    protect = crypt32.CryptProtectData
    protect.argtypes = [blob_p, wintypes.LPCWSTR, blob_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_p]
    protect.restype = wintypes.BOOL
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_p]
    unprotect.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    _dpapi = (protect, unprotect, local_free, DATA_BLOB)
    return _dpapi


def _make_blob(DATA_BLOB, data: bytes):
    import ctypes

    buf = ctypes.create_string_buffer(data, max(len(data), 1))
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf  # keep `buf` alive as long as `blob` is used


def _dpapi_call(which: str, data: bytes) -> bytes:
    """Run CryptProtectData ('protect') or CryptUnprotectData ('unprotect'). Raises OSError on failure."""
    import ctypes

    protect, unprotect, local_free, DATA_BLOB = _load_dpapi()
    src, src_buf = _make_blob(DATA_BLOB, data)
    ent, ent_buf = _make_blob(DATA_BLOB, _ENTROPY)
    out = DATA_BLOB()
    if which == "protect":
        ok = protect(ctypes.byref(src), None, ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    else:
        ok = unprotect(ctypes.byref(src), None, ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    ctypes.memset(src_buf, 0, len(src_buf))  # wipe our plaintext copy (input of 'protect')
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.memset(out.pbData, 0, out.cbData)  # output of 'unprotect' is plaintext: wipe before freeing
        local_free(out.pbData)


class DpapiSecretStore:
    """One DPAPI-encrypted entry per secret in a JSON file. Reads the file on every call (no cache)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # -- file handling -------------------------------------------------------------------------

    def _corrupt(self, why: str) -> ConfigError:
        return ConfigError(
            f"Die Datei mit den gespeicherten Zugangsdaten ist beschädigt ({why}).",
            hint=_HINT_SETUP,
        )

    def _read(self, *, recover: bool = False) -> dict[str, str]:
        """Parse the file. `recover=True` (write paths): move a malformed file aside and start empty."""
        try:
            raw = _retry_busy(self.path.read_bytes)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ConfigError(
                "Die Datei mit den gespeicherten Zugangsdaten kann nicht gelesen werden.",
                hint="Zugriffsrechte und Speicherort prüfen.",
                detail=type(exc).__name__,
            ) from exc
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
                raise ValueError("structure")
        except ValueError:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            if not recover:
                raise self._corrupt("kein gültiges Format") from None
            _retry_busy(os.replace, self.path, self.path.with_name(self.path.name + ".corrupt"))
            return {}
        return data

    def _write(self, data: dict[str, str]) -> None:
        """Atomic replace through a uniquely named temp file; the caller holds the writer lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        try:
            with open(tmp, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            _retry_busy(os.replace, tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigError(
                "Die Zugangsdaten konnten nicht gespeichert werden (Datei gesperrt oder nicht beschreibbar).",
                hint="Kurz warten und erneut versuchen; Zugriffsrechte und Speicherort der Datei prüfen.",
                detail=type(exc).__name__,
            ) from exc

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _writer(self) -> Iterator[None]:
        """Serialise read-modify-write: threads of this process first, then other processes."""
        with _WRITE_LOCK:
            lock = ProcessLock(self.lock_path)
            try:
                got = lock.acquire(blocking=True, timeout=_LOCK_WAIT_SECONDS)
            except OSError as exc:
                raise ConfigError(
                    "Die Sperrdatei des geschützten Speichers konnte nicht angelegt werden.",
                    hint="Zugriffsrechte und Speicherort prüfen.",
                    detail=type(exc).__name__,
                ) from exc
            if not got:
                raise TransientError(
                    "Der geschützte Speicher wird gerade von einem anderen SchulSync-Vorgang verwendet.",
                    hint="Kurz warten und erneut versuchen.",
                )
            try:
                yield
            finally:
                lock.release()

    # -- public API ----------------------------------------------------------------------------

    def get(self, name: str) -> str | None:
        entry = self._read().get(name)
        if entry is None:
            return None
        try:
            blob = base64.b64decode(entry, validate=True)
            return _dpapi_call("unprotect", blob).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise self._undecryptable(name) from None
        except OSError:
            raise self._undecryptable(name) from None

    def _undecryptable(self, name: str) -> ConfigError:
        return ConfigError(
            f"Der gespeicherte Eintrag '{name}' kann nicht entschlüsselt werden "
            "(anderer Windows-Benutzer, anderer Computer oder beschädigt).",
            hint=_HINT_SETUP,
        )

    def set(self, name: str, value: str) -> None:
        _check_name(name)
        try:
            blob = _dpapi_call("protect", value.encode("utf-8"))  # outside the lock: it does not touch the file
        except OSError as exc:
            raise ConfigError(
                "Die Zugangsdaten konnten nicht geschützt gespeichert werden (Windows-DPAPI).",
                hint="Läuft SchulSync in einem normalen Windows-Benutzerkonto?",
                detail=f"winerror={getattr(exc, 'winerror', None)}",
            ) from exc
        with self._writer():
            data = self._read(recover=True)
            data[name] = base64.b64encode(blob).decode("ascii")
            self._write(data)

    def delete(self, name: str) -> None:
        with self._writer():
            data = self._read(recover=True)
            if name in data:
                del data[name]
                self._write(data)

    def names(self) -> list[str]:
        return sorted(self._read())


# --------------------------------------------------------------------------- redaction hookup


def _sensitive_json_leaves(obj: Any, key: str = "") -> Iterable[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _sensitive_json_leaves(v, str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from _sensitive_json_leaves(v, key)
    elif isinstance(obj, str) and key.lower() in SENSITIVE_KEY_NAMES:
        yield obj


def register_loaded_secrets(store: SecretStore, names: Iterable[str]) -> list[str]:
    """Register every present secret with `redact.register_secret`; returns the names that were found.

    JSON secrets (OAuth client file, token file) are registered as a whole AND by their sensitive
    leaf values (``client_secret``, ``refresh_token`` ...), because logs would show the leaf, not
    the whole document.
    """
    found: list[str] = []
    for name in names:
        value = store.get(name)
        if not value:
            continue
        found.append(name)
        register_secret(value)
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            for leaf in _sensitive_json_leaves(parsed):
                register_secret(leaf)
    return found
