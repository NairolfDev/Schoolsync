"""1.1.0: Windows-Meldung bei Problemen, `uninstall`."""

from __future__ import annotations

import subprocess

import pytest

from schulsync import frozen, notify
from schulsync.errors import TransientError
from schulsync.models import Source
from tests.support import samples as S
from tests.support.engine_harness import Env

WU = Source.WEBUNTIS


# ---- toast

def test_toast_passes_text_by_environment_not_in_the_command(monkeypatch):
    monkeypatch.undo()  # the autouse fixture replaced notify.toast; this test needs the real one
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"], timeout=kw["timeout"], shell=kw.get("shell", False))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    monkeypatch.setattr(notify.os, "name", "nt")
    assert notify.toast("Titel <x>", "Text '\" $(rm)") is True
    assert "$(rm)" not in " ".join(seen["cmd"]) and "Titel" not in " ".join(seen["cmd"]) and seen["env"]["SCHULSYNC_TOAST_TEXT"] == "Text '\" $(rm)"
    assert seen["timeout"] <= 30 and not seen["shell"]


def test_toast_failure_is_false_not_an_exception(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(notify.os, "name", "nt")

    def boom(*a, **k):
        raise OSError("kein PowerShell")

    monkeypatch.setattr(notify.subprocess, "run", boom)
    assert notify.toast("a", "b") is False


def test_a_problem_shows_one_toast_per_problem_and_day_but_success_none(tmp_path):
    env = Env(tmp_path)
    shown: list[str] = []
    env.set_items(S.assignment(), S.exam())
    env.adapters[WU].raise_next(TransientError("Zeitüberschreitung."), times=4)
    out = env.run(notify=lambda title, text: shown.append(title) or True)
    assert out.report.needs_attention and shown == ["SchulSync: Problem beim Abgleich"]
    env.adapters[WU].raise_next(TransientError("Zeitüberschreitung."), times=4)
    env.run(notify=lambda title, text: shown.append(title) or True)  # same problem, same day
    assert len(shown) == 1


def test_a_successful_run_shows_no_toast(tmp_path):
    env = Env(tmp_path)
    shown: list[str] = []
    env.set_items(S.assignment())
    out = env.run(notify=lambda title, text: shown.append(title) or True)
    assert not out.report.needs_attention and shown == []


def test_a_failing_toast_never_breaks_the_run(tmp_path):
    env = Env(tmp_path)
    env.set_items(S.assignment())
    env.adapters[WU].raise_next(TransientError("x"), times=4)

    def boom(title, text):
        raise RuntimeError("Toast kaputt")

    out = env.run(notify=boom)
    assert out.report is not None and out.report.needs_attention


# ---- uninstall

def test_uninstall_refuses_foreign_folders(tmp_path):
    with pytest.raises(ValueError):
        frozen._delete_later([tmp_path / "Dokumente"])


def test_uninstall_removes_path_and_schedules_the_folder_deletion(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    removed, started = [], []
    monkeypatch.setattr(frozen, "remove_from_user_path", lambda p: removed.append(p) or True)
    monkeypatch.setattr(frozen, "_delete_later", lambda folders: started.append(list(folders)))
    out: list[str] = []
    frozen.uninstall(out.append, data_dir=None)
    assert removed == [tmp_path / "Programs" / "SchulSync"] and started == [[tmp_path / "Programs" / "SchulSync"]]
    assert "bleiben erhalten" in " ".join(out)
    started.clear()
    frozen.uninstall(out.append, data_dir=tmp_path / "SchulSync")
    assert started[0][1] == tmp_path / "SchulSync"
