"""Logging for the 'schulsync' logger: one UTF-8 file per Vienna day, optional console, redaction.

- Files: ``logs/schulsync-YYYY-MM-DD.log``. The day is the Europe/Vienna date of each *record*, so a
  process that runs across midnight switches files by itself. (`logging.FileHandler` with
  ``delay=True`` is used instead of RotatingFileHandler: rotation is by day and pruning is done by
  :func:`prune_logs`.)
- Every handler carries a :class:`~schulsync.redact.RedactingFilter` (message, args, exceptions).
- ``propagate`` is switched off for the 'schulsync' logger: handlers of the root logger would not
  have the filter and could leak unredacted text.
- Idempotent: handlers installed by an earlier call are removed (and closed) first.
- Timestamps in the log lines are ISO-8601 with the Vienna offset.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import Paths
from .redact import RedactingFilter
from .timeutil import VIENNA, now_vienna, today_vienna

LOGGER_NAME = "schulsync"
# schulsync-<day>.log, and bericht-<day>.txt: the plain-text copy the runner leaves next to the logs when the HTML report
# could not be written (runner._plain_text_copy); both age out together.
_FILE_RE = re.compile(r"^(?:schulsync-(\d{4}-\d{2}-\d{2})\.log|bericht-(\d{4}-\d{2}-\d{2})\.txt)$")
_MARK = "_schulsync_handler"


def log_file_for(paths: Paths, day: datetime | None = None) -> Path:
    d = today_vienna(day)
    return paths.logs / f"schulsync-{d.isoformat()}.log"


class _Formatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, VIENNA).isoformat(timespec="seconds")


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _DailyFileHandler(logging.FileHandler):
    """FileHandler whose target file follows the Vienna date of each record."""

    def __init__(self, paths: Paths):
        self._paths = paths
        super().__init__(log_file_for(paths), mode="a", encoding="utf-8", delay=True)

    def emit(self, record: logging.LogRecord) -> None:
        target = os.fspath(self._paths.logs / f"schulsync-{datetime.fromtimestamp(record.created, VIENNA).date().isoformat()}.log")
        if target != self.baseFilename:
            if self.stream is not None:  # emit() runs under the handler lock
                self.stream.close()
                self.stream = None
            self.baseFilename = target
        super().emit(record)


class _ConsoleHandler(logging.StreamHandler):
    """Console output that never fails on characters the console encoding cannot represent."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        enc = getattr(self.stream, "encoding", None) or "utf-8"
        return text.encode(enc, "replace").decode(enc, "replace")


def _remove_our_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if getattr(h, _MARK, False):
            logger.removeHandler(h)
            h.close()


def shutdown_logging() -> None:
    """Close and detach the handlers installed by :func:`setup_logging` (tests, clean exit)."""
    _remove_our_handlers(logging.getLogger(LOGGER_NAME))


def setup_logging(
    paths: Paths,
    *,
    level: int = logging.INFO,
    console: bool = False,
    keep_days: int = 30,
    now: datetime | None = None,
) -> logging.Logger:
    """Configure and return the 'schulsync' logger. Safe to call repeatedly."""
    paths.logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    _remove_our_handlers(logger)
    logger.setLevel(level)
    logger.propagate = False

    handlers: list[logging.Handler] = [_DailyFileHandler(paths)]
    # pythonw.exe (autostart) has no stderr: a console handler would be pointless there.
    if console and sys.stderr is not None:
        handlers.append(_ConsoleHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(_Formatter(_FORMAT))
        h.addFilter(RedactingFilter())
        setattr(h, _MARK, True)
        logger.addHandler(h)
    # Our handlers run first: their RedactingFilter rewrites the record in place (msg redacted, args
    # dropped), so foreign handlers that may be attached to this logger (pytest's capture handlers
    # are attached to non-propagating loggers; a future GUI log view) never see unredacted text or
    # unformattable args. Stable sort: the relative order of all other handlers is kept.
    logger.handlers.sort(key=lambda x: not getattr(x, _MARK, False))

    prune_logs(paths, keep_days, now)
    return logger


def prune_logs(paths: Paths, keep_days: int, now: datetime | None = None) -> list[Path]:
    """Delete ``schulsync-YYYY-MM-DD.log`` (and the runner's plain-text report copies ``bericht-YYYY-MM-DD.txt``) whose date
    is more than `keep_days` days before today.

    Only files matching those exact patterns are considered. Files that cannot be deleted (e.g. open in
    an editor on Windows) are skipped. Returns the deleted paths.
    """
    cutoff = today_vienna(now if now is not None else now_vienna()) - timedelta(days=keep_days)
    deleted: list[Path] = []
    try:
        candidates = sorted(paths.logs.iterdir())
    except OSError:
        return deleted
    for f in candidates:
        m = _FILE_RE.match(f.name)
        if not m or not f.is_file():
            continue
        try:
            day = datetime.strptime(m.group(1) or m.group(2), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                f.unlink()
                deleted.append(f)
            except OSError:
                continue
    return deleted
