"""Tests for schulsync.logging_setup."""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pytest

from schulsync import redact
from schulsync.logging_setup import LOGGER_NAME, log_file_for, prune_logs, setup_logging, shutdown_logging
from schulsync.redact import RedactingFilter
from schulsync.timeutil import VIENNA, today_vienna


@pytest.fixture(autouse=True)
def _clean():
    redact.clear_registered_secrets()
    yield
    shutdown_logging()
    redact.clear_registered_secrets()


def _ours(log: logging.Logger) -> list[logging.Handler]:
    """Handlers installed by setup_logging (pytest may attach its own capture handlers to the logger)."""
    return [h for h in log.handlers if getattr(h, "_schulsync_handler", False)]


def _read(paths, now: datetime | None = None) -> str:
    """Content of the one log file written for the real current Vienna day (`now` only drives pruning)."""
    files = list(paths.logs.glob("schulsync-*.log"))
    assert len(files) == 1, files
    return files[0].read_text(encoding="utf-8")


def test_writes_utf8_file_named_by_vienna_day(paths, now):
    log = setup_logging(paths, console=False, keep_days=30, now=now)
    assert log is logging.getLogger(LOGGER_NAME)
    logging.getLogger("schulsync.sync").info("Prüfung für 3AHITN: Größe 5 – ok")
    shutdown_logging()
    f = paths.logs / f"schulsync-{today_vienna().isoformat()}.log"
    assert f == log_file_for(paths) and f.exists()
    text = f.read_text(encoding="utf-8")
    assert "Prüfung für 3AHITN: Größe 5 – ok" in text
    assert "INFO" in text and "schulsync.sync" in text
    assert text.startswith(f"{today_vienna().isoformat()}T")  # ISO timestamp with offset follows
    assert ("+01:00" in text.splitlines()[0]) or ("+02:00" in text.splitlines()[0])


def test_file_is_created_lazily(paths):
    setup_logging(paths, console=False, keep_days=30)
    assert list(paths.logs.glob("schulsync-*.log")) == []


def test_redacts_message_args_and_exceptions_in_file(paths, now):
    redact.register_secret("hunter2-dummy")
    log = setup_logging(paths, console=False, keep_days=30, now=now)
    child = logging.getLogger("schulsync.adapters.eduvidual")  # child logger: filter sits on handlers
    child.info("Anmeldung mit hunter2-dummy")
    child.info("Anmeldung mit %s und %s", "hunter2-dummy", {"password": "anders-dummy"})
    child.warning("URL https://x.example/server.php?wstoken=0123456789abcdef&fmt=json")
    try:
        raise RuntimeError("Antwort: sesskey=Qw3rTy9Zx1 / hunter2-dummy")
    except RuntimeError:
        child.exception("Abruf fehlgeschlagen")
    log.error("Header Cookie: MoodleSessioneduvidual=a1b2c3d4e5f6")
    shutdown_logging()
    text = _read(paths, now)
    for secret in ("hunter2-dummy", "anders-dummy", "0123456789abcdef", "Qw3rTy9Zx1", "a1b2c3d4e5f6"):
        assert secret not in text, secret
    assert "Traceback" in text and "RuntimeError" in text and "Abruf fehlgeschlagen" in text
    assert text.count("***") >= 6


def test_every_handler_has_redacting_filter_and_idempotent(paths, now):
    log = setup_logging(paths, console=True, keep_days=30, now=now)
    assert len(_ours(log)) == 2
    for h in _ours(log):
        assert any(isinstance(f, RedactingFilter) for f in h.filters)
    log2 = setup_logging(paths, console=True, keep_days=30, now=now)
    assert log2 is log and len(_ours(log)) == 2
    setup_logging(paths, console=False, keep_days=30, now=now)
    assert len(_ours(log)) == 1
    logging.getLogger("schulsync.x").info("einmal")
    shutdown_logging()
    assert _read(paths, now).count("einmal") == 1  # not duplicated
    assert _ours(log) == []


def test_foreign_handlers_are_left_alone(paths):
    log = logging.getLogger(LOGGER_NAME)
    foreign = logging.NullHandler()
    log.addHandler(foreign)
    try:
        setup_logging(paths, console=False, keep_days=30)
        setup_logging(paths, console=False, keep_days=30)
        shutdown_logging()
        assert foreign in log.handlers
    finally:
        log.removeHandler(foreign)


def test_level_and_no_propagation(paths, now):
    log = setup_logging(paths, level=logging.WARNING, console=False, keep_days=30, now=now)
    assert log.level == logging.WARNING and log.propagate is False
    logging.getLogger("schulsync.t").info("leise")
    logging.getLogger("schulsync.t").warning("laut")
    shutdown_logging()
    text = _read(paths, now)
    assert "laut" in text and "leise" not in text


def test_console_output_is_redacted_and_encoding_safe(paths, capsys):
    redact.register_secret("hunter2-dummy")
    setup_logging(paths, console=True, keep_days=30)
    logging.getLogger("schulsync.c").info("hallo hunter2-dummy äöü")
    err = capsys.readouterr().err
    assert "hallo *** äöü" in err


def test_file_follows_vienna_day_across_midnight(paths):
    setup_logging(paths, console=False, keep_days=30)
    log = logging.getLogger("schulsync.m")
    # 2026-10-14 22:30 UTC is already 15.10. 00:30 in Vienna (CEST): the record belongs to the 15th.
    rec_a = log.makeRecord("schulsync.m", logging.INFO, __file__, 1, "vor Mitternacht", (), None)
    rec_a.created = datetime(2026, 10, 14, 21, 30, tzinfo=VIENNA).timestamp()
    rec_b = log.makeRecord("schulsync.m", logging.INFO, __file__, 1, "nach Mitternacht", (), None)
    rec_b.created = datetime(2026, 10, 15, 0, 30, tzinfo=VIENNA).timestamp()
    log.handle(rec_a)
    log.handle(rec_b)
    shutdown_logging()
    a = (paths.logs / "schulsync-2026-10-14.log").read_text(encoding="utf-8")
    b = (paths.logs / "schulsync-2026-10-15.log").read_text(encoding="utf-8")
    assert "vor Mitternacht" in a and "nach Mitternacht" not in a
    assert "nach Mitternacht" in b and "vor Mitternacht" not in b


# --------------------------------------------------------------------------- pruning


def _touch(path):
    path.write_text("x", encoding="utf-8")
    return path


def test_prune_deletes_only_old_schulsync_logs(paths, now):  # now = 2026-10-14
    old = _touch(paths.logs / "schulsync-2026-09-01.log")
    edge_keep = _touch(paths.logs / "schulsync-2026-09-14.log")  # exactly 30 days old: kept
    edge_drop = _touch(paths.logs / "schulsync-2026-09-13.log")  # 31 days old: deleted
    today = _touch(paths.logs / "schulsync-2026-10-14.log")
    future = _touch(paths.logs / "schulsync-2026-12-01.log")
    foreign = [
        _touch(paths.logs / "other-2020-01-01.log"),
        _touch(paths.logs / "schulsync-2020-01-01.txt"),
        _touch(paths.logs / "schulsync-abc.log"),
        _touch(paths.logs / "notes.log"),
        _touch(paths.logs / "schulsync-2020-13-45.log"),  # matches the pattern, not a real date
    ]
    (paths.logs / "schulsync-2020-01-01.log.d").mkdir()  # directory with log-like name
    deleted = prune_logs(paths, 30, now)
    assert sorted(p.name for p in deleted) == ["schulsync-2026-09-01.log", "schulsync-2026-09-13.log"]
    assert not old.exists() and not edge_drop.exists()
    assert edge_keep.exists() and today.exists() and future.exists()
    assert all(p.exists() for p in foreign)
    assert (paths.logs / "schulsync-2020-01-01.log.d").is_dir()


def test_prune_without_logs_dir_is_fine(tmp_path, now):
    from schulsync.config import Paths

    assert prune_logs(Paths(tmp_path / "gibt-es-nicht"), 30, now) == []


def test_setup_prunes_old_logs(paths, now):
    old = _touch(paths.logs / "schulsync-2026-01-01.log")
    setup_logging(paths, console=False, keep_days=7, now=now)
    assert not old.exists()


@pytest.mark.skipif(os.name != "nt", reason="open-file deletion semantics differ on POSIX")
def test_prune_skips_file_that_is_open_elsewhere(paths, now):
    f = _touch(paths.logs / "schulsync-2026-01-01.log")
    with open(f, "rb"):  # Windows: deleting an open file fails with PermissionError -> skipped, no crash
        deleted = prune_logs(paths, 7, now)
    assert deleted == [] and f.exists()


def test_our_handlers_run_before_foreign_handlers_and_foreign_ones_see_redacted_records(paths):
    seen: list[str] = []

    class Spy(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    log = logging.getLogger(LOGGER_NAME)
    spy = Spy()
    log.addHandler(spy)  # attached BEFORE setup_logging, like pytest's capture handlers
    try:
        redact.register_secret("hunter2-dummy")
        setup_logging(paths, console=False, keep_days=30)
        assert getattr(log.handlers[0], "_schulsync_handler", False)
        log.info("passwort hunter2-dummy %s", "hunter2-dummy")
        assert seen == ["passwort *** ***"]
    finally:
        log.removeHandler(spy)
