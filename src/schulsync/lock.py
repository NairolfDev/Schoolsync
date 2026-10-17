"""Single-instance process lock based on an OS file lock.

The lock belongs to the open file handle, so the operating system releases it when the process
ends for any reason (crash, kill, power loss): there are no stale locks. The lock file itself is
never deleted (deleting would race with a process that just opened it) and may contain the PID of
the holder for diagnostics only; the PID is never used to decide anything.

Windows: ``msvcrt.locking(fd, LK_NBLCK, 1)`` (Python docs, msvcrt) locks one byte. We lock a byte far
beyond the PID text so other processes can still read the PID (Windows locks are mandatory for the
locked region only). Other platforms: ``fcntl.flock(LOCK_EX | LOCK_NB)``. The non-Windows branch is
not exercised on this development machine.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path

from .errors import SchulSyncError

_LOCK_OFFSET = 1 << 30  # Windows only: byte that is locked (lock beyond EOF is allowed)
_POLL_SECONDS = 0.1
# errno values that mean "somebody else holds the lock"; any other OSError is a real problem (a file system that cannot
# lock) and must not be mistaken for a second running instance, or every automatic start would end silently.
_CONTENTION = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


class LockHeldError(SchulSyncError):
    """Another SchulSync process holds the lock."""

    code = "lock_held"


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _CONTENTION:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _CONTENTION:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class ProcessLock:
    """Exclusive, non-reentrant-across-objects lock. Two ProcessLock objects on the same path
    exclude each other even inside one process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, blocking: bool = False, timeout: float | None = None) -> bool:
        """Try to take the lock. `blocking=True` polls until acquired (or `timeout` seconds passed)."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                locked = _try_lock(fd)
            except BaseException:
                os.close(fd)  # a lock error is the caller's problem (OSError), but the handle must not leak
                raise
            if locked:
                self._fd = fd
                self._write_pid(fd)
                return True
            os.close(fd)
            if not blocking or (deadline is not None and time.monotonic() >= deadline):
                return False
            time.sleep(_POLL_SECONDS)

    @staticmethod
    def _write_pid(fd: int) -> None:
        try:  # diagnostics only; failure must never matter
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass  # closing the handle releases the lock anyway
        finally:
            os.close(fd)

    def holder_pid(self) -> int | None:
        """PID written by the current/last holder (diagnostics only; may be stale)."""
        try:
            return int(self.path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def __enter__(self) -> "ProcessLock":
        if not self.acquire(blocking=False):
            raise LockHeldError(
                "SchulSync läuft bereits.",
                hint="Warten, bis der andere Lauf beendet ist, und dann erneut versuchen.",
            )
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
