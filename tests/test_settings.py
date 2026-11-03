"""settings.py und `schulsync settings`: validieren vor dem Schreiben, Kommentare bleiben, Menü mit Skript-Antworten."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from schulsync import cli, settings
from schulsync.cli import CliEnv, main
from schulsync.config import load_config
from schulsync.logging_setup import shutdown_logging
from schulsync.secrets_store import MemorySecretStore
from tests.test_cli import Io

TODAY = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch):
    monkeypatch.delenv("SCHULSYNC_HOME", raising=False)
    yield
    shutdown_logging()


@pytest.fixture
def io() -> Io:
    return Io()


@pytest.fixture
def cfg_path(paths) -> Path:
    cli._write_default_config(paths.config)
    return paths.config


def run(paths, io, *argv) -> int:
    from tests.test_cli import Clock
    from datetime import timezone
    env = CliEnv(say=io.say, ask=io.ask, ask_secret=io.ask_secret, paths=paths, secrets=MemorySecretStore(),
                 clock=lambda: datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
    return main(["settings", *argv], env=env)


def test_list_shows_value_default_and_help_without_prompts(paths, io, cfg_path):
    assert run(paths, io, "--list") == 0
    assert "rules.study_every_days" in io.out and "Standard: 0" in io.out and "Erlaubt: ganze Zahl von 0 bis 30" in io.out
    assert io.prompts == []


def test_set_valid_values_writes_and_keeps_comments(paths, io, cfg_path):
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + "\n# mein Kommentar\n", encoding="utf-8")
    before = cfg_path.read_text(encoding="utf-8")
    assert run(paths, io, "--set", "rules.study_every_days=4", "--set", "report.notify=nein",
               "--set", "webuntis.categories=homework,exams", "--set", "eduvidual.courses=123, Mathe") == 0
    cfg = load_config(cfg_path)
    assert cfg.rules.study_every_days == 4 and cfg.report.notify is False
    assert cfg.webuntis.categories == ("homework", "exams") and cfg.eduvidual.courses == ("123", "Mathe")
    text = cfg_path.read_text(encoding="utf-8")
    assert text.startswith("# SchulSync-Konfiguration") and "# mein Kommentar" in text
    assert text != before
    assert "Gilt ab dem nächsten Lauf" in io.out and "Beispiel: Prüfung am 31.10.: Lern-Tasks am 17.10., 21.10." in io.out


@pytest.mark.parametrize("arg", [
    "rules.study_every_days=31", "rules.study_every_days=abc", "rules.window_days=0", "report.open_on=immer",
    "webuntis.categories=homework,foo", "webuntis.categories=", "report.notify=vielleicht", "retry.max_attempts=3",
    "keine_gleichung", "rules.nope=1",
])
def test_set_invalid_leaves_the_file_unchanged_and_exits_2(paths, io, cfg_path, arg):
    before = cfg_path.read_bytes()
    assert run(paths, io, "--set", "rules.window_days=40", "--set", arg) == 2
    assert cfg_path.read_bytes() == before  # auch der gültige Teil wird nicht geschrieben
    assert "Nicht geändert" in io.out


def test_cross_field_error_from_parse_config_blocks_the_write(paths, cfg_path, monkeypatch):
    before = cfg_path.read_bytes()
    st = settings.BY_KEY["rules.window_days"]
    monkeypatch.setitem(settings.BY_KEY, "rules.window_days", settings.Setting(st.key, st.label, st.help, "int", 1, 999))
    with pytest.raises(settings.ConfigError):  # Tabelle erlaubt 500, parse_config nicht
        settings.validated_write(cfg_path, {"rules.window_days": 500})
    assert cfg_path.read_bytes() == before


def test_reset_restores_the_default(paths, io, cfg_path):
    assert run(paths, io, "--set", "rules.exam_lead_days=2") == 0
    assert load_config(cfg_path).rules.exam_lead_days == 2
    assert run(paths, io, "--reset", "rules.exam_lead_days") == 0
    assert load_config(cfg_path).rules.exam_lead_days == 7
    assert run(paths, io, "--reset", "rules.unknown") == 2


def test_no_config_says_run_setup_first(paths, io):
    assert not paths.config.exists()
    assert run(paths, io, "--list") == 1
    assert "schulsync setup" in io.out
    assert not paths.config.exists()


def test_interactive_menu_with_scripted_answers(paths, io, cfg_path):
    keys = [s.key for s in settings.SETTINGS]
    n = lambda k: str(keys.index(k) + 1)  # noqa: E731
    io.answers.extend([
        "x", "99",                                   # ungültige Menüwahl
        n("rules.window_days"), "500",               # ungültig: Datei bleibt
        n("rules.window_days"), "",                  # leer = behalten
        n("rules.study_start_days"), "10",
        n("report.open_on"), "never",
        "q",
    ])
    assert run(paths, io, ) == 0
    cfg = load_config(cfg_path)
    assert cfg.rules.window_days == 31 and cfg.rules.study_start_days == 10 and cfg.report.open_on == "never"
    out = io.out
    assert "Nicht geändert" in out and "ganze Zahl von 1 bis 120" in out
    assert "Gilt ab dem nächsten Lauf" in out and "Lernplan" in out
    assert "Erlaubt:" in out


def test_interactive_menu_without_change(paths, io, cfg_path):
    io.answers.append("q")
    assert run(paths, io) == 0
    assert "Nichts geändert" in io.out


@pytest.mark.parametrize("key,text,expected", [
    ("rules.assignment_calendar", "Ja", True), ("rules.assignment_calendar", "aus", False),
    ("report.open_on", "warnings", "warnings"),
    ("webuntis.categories", "exams, cancellations", ["exams", "cancellations"]),
    ("eduvidual.courses", "-", []), ("eduvidual.courses", "5,Physik", ["5", "Physik"]),
    ("rules.reminder_minutes", " 60 ", 60),
])
def test_parse_value(key, text, expected):
    assert settings.parse_value(settings.BY_KEY[key], text) == expected


def test_study_preview_text():
    assert settings.study_preview(4, 14, date(2026, 10, 30)) == (
        "Prüfung am 30.10.: Lern-Tasks am 16.10., 20.10., 24.10., 28.10. (4 zusätzliche).")
    assert "..." in settings.study_preview(1, 14, date(2026, 10, 30))
    assert "kein Lernplan" in settings.study_preview(0, 14, date(2026, 10, 30))


def test_every_setting_maps_to_an_existing_config_key_with_matching_type_and_default_in_range():
    assert settings.all_keys_exist() == []
    for st in settings.SETTINGS:
        d = settings.default_value(st)
        if st.kind == settings.INT:
            assert st.lo <= d <= st.hi, st.key
            assert isinstance(d, int) and not isinstance(d, bool)
        if st.kind == settings.BOOL:
            assert isinstance(d, bool)
        if st.kind in (settings.CHOICE, settings.MULTI):
            assert set([d] if isinstance(d, str) else d) <= set(st.choices)
    assert len(settings.BY_KEY) == len(settings.SETTINGS)


def test_study_preview_excludes_normal_exam_task():
    text = settings.study_preview(1, 14, date(2026, 10, 30))
    assert "(13 zusätzliche)" in text
