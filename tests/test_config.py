"""Configuration (ANFORDERUNGEN 9: readable configuration for sources, target ids, lead times and retries).

`config.py` had no test module of its own: mutants that removed the https-only check, the range checks or the unknown-key
error survived the whole suite. Every rule of `_validate` and `_build` is pinned here, each with a value just inside and
just outside the allowed range, so a removed or loosened check fails exactly one test.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from schulsync.autostart import build_task_xml
from schulsync.cli import WATCHDOG_GRACE_SECONDS
from schulsync.config import MAX_RUN_TIMEOUT_SECONDS, Config, load_config, parse_config, set_config_value
from schulsync.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent


def bad(section: str, key: str, value: str) -> None:
    with pytest.raises(ConfigError):
        parse_config(f"[{section}]\n{key} = {value}\n")


def good(section: str, key: str, value: str) -> Config:
    return parse_config(f"[{section}]\n{key} = {value}\n")


# --------------------------------------------------------------------------- the example file


def test_the_example_file_is_exactly_the_default_configuration():
    """Drift between `config.example.toml` and `Config()` (a changed default, a pasted value) must not go unnoticed."""
    assert parse_config((ROOT / "config.example.toml").read_text(encoding="utf-8")) == Config()


def test_the_example_file_and_the_defaults_carry_no_credentials_or_ids():
    cfg = Config()
    assert cfg.eduvidual.username == "" and cfg.webuntis.username == ""
    assert cfg.google.calendar_id == "" and cfg.google.tasklist_id == ""
    raw = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    for section in raw.values():
        for key, value in section.items():
            assert not any(w in key.lower() for w in ("password", "passwort", "token", "secret", "key")), key
            if key in ("username", "calendar_id", "tasklist_id"):
                assert value == "", f"{key} must be empty in the example"


def test_an_empty_file_gives_the_defaults():
    assert parse_config("") == Config()


# --------------------------------------------------------------------------- https only (credentials are never sent over http)


@pytest.mark.parametrize("section", ["eduvidual", "webuntis"])
@pytest.mark.parametrize("url", ["http://www.eduvidual.at", "ftp://x", "www.eduvidual.at", ""])
def test_base_urls_must_be_https(section, url):
    bad(section, "base_url", f'"{url}"')


@pytest.mark.parametrize("section", ["eduvidual", "webuntis"])
def test_an_https_base_url_is_accepted(section):
    cfg = good(section, "base_url", '"https://example.invalid"')
    assert getattr(cfg, section).base_url == "https://example.invalid"


# --------------------------------------------------------------------------- choices


@pytest.mark.parametrize("method", ["auto", "webservice", "web"])
def test_eduvidual_method_accepts_the_three_routes(method):
    assert good("eduvidual", "method", f'"{method}"').eduvidual.method == method


def test_eduvidual_method_refuses_anything_else():
    bad("eduvidual", "method", '"browser"')


def test_webuntis_categories_are_checked():
    assert good("webuntis", "categories", '["homework"]').webuntis.categories == ("homework",)
    bad("webuntis", "categories", '["homework", "grades"]')


@pytest.mark.parametrize("value", ["problems", "warnings", "never"])
def test_report_open_on_accepts_the_three_modes(value):
    assert good("report", "open_on", f'"{value}"').report.open_on == value


def test_report_open_on_refuses_anything_else():
    bad("report", "open_on", '"always"')


# --------------------------------------------------------------------------- ranges


@pytest.mark.parametrize("value, ok", [(0, False), (1, True), (21, True), (120, True), (121, False)])
def test_window_days_range(value, ok):
    if ok:
        assert good("rules", "window_days", str(value)).rules.window_days == value
    else:
        bad("rules", "window_days", str(value))


@pytest.mark.parametrize("key", ["assignment_lead_days", "homework_lead_days", "exam_lead_days"])
@pytest.mark.parametrize("value, ok", [(-1, False), (0, True), (60, True), (61, False)])
def test_lead_days_range(key, value, ok):
    if ok:
        assert getattr(good("rules", key, str(value)).rules, key) == value
    else:
        bad("rules", key, str(value))


@pytest.mark.parametrize("value, ok", [(0, False), (1, True), (1440, True), (40320, True), (40321, False)])
def test_reminder_minutes_range(value, ok):
    if ok:
        assert good("rules", "reminder_minutes", str(value)).rules.reminder_minutes == value
    else:
        bad("rules", "reminder_minutes", str(value))


@pytest.mark.parametrize("value, ok", [(0, False), (1, True), (10, True), (11, False)])
def test_max_attempts_range(value, ok):
    if ok:
        assert good("retry", "max_attempts", str(value)).retry.max_attempts == value
    else:
        bad("retry", "max_attempts", str(value))


def test_delays_must_not_be_negative():
    assert good("retry", "delays_seconds", "[0, 5, 10.5]").retry.delays_seconds == (0.0, 5.0, 10.5)
    bad("retry", "delays_seconds", "[5, -1]")


@pytest.mark.parametrize("key", ["connect_timeout", "read_timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "0.0"])
def test_http_time_limits_must_be_positive(key, value):
    bad("retry", key, value)


@pytest.mark.parametrize("value", ["0", "0.0", "-5", "-0.5"])
def test_the_run_time_limit_must_be_positive(value):
    """0 made EVERY source fail with 'run_timeout' at every logon (a one-character typo, a permanently failing program)."""
    bad("retry", "run_timeout_seconds", value)


def test_the_run_time_limit_stays_below_what_the_scheduled_task_allows():
    """The task XML ends a run after PT1H without any report. The watchdog (run limit + grace) must fire before that, so the
    configured limit may not exceed the hour minus the grace; a value above is a configuration error, not a silent kill."""
    xml = build_task_xml(user_sid_or_name="PC\\user", command="pythonw.exe", arguments="-m schulsync run --auto", working_dir="C:\\")
    assert "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>" in xml
    assert MAX_RUN_TIMEOUT_SECONDS + WATCHDOG_GRACE_SECONDS < 3600
    assert good("retry", "run_timeout_seconds", str(MAX_RUN_TIMEOUT_SECONDS)).retry.run_timeout_seconds == MAX_RUN_TIMEOUT_SECONDS
    bad("retry", "run_timeout_seconds", str(MAX_RUN_TIMEOUT_SECONDS + 1))
    bad("retry", "run_timeout_seconds", "86400")


def test_a_small_positive_run_time_limit_is_fine():
    assert good("retry", "run_timeout_seconds", "0.5").retry.run_timeout_seconds == 0.5


@pytest.mark.parametrize("value", ["-1", "-0.1"])
def test_the_network_wait_must_not_be_negative(value):
    bad("retry", "network_wait_seconds", value)


def test_the_network_wait_may_be_zero_to_not_wait_at_all():
    assert good("retry", "network_wait_seconds", "0").retry.network_wait_seconds == 0.0


@pytest.mark.parametrize("key", ["keep_days", "log_keep_days"])
def test_retention_is_at_least_one_day(key):
    assert getattr(good("report", key, "1").report, key) == 1
    bad("report", key, "0")


# --------------------------------------------------------------------------- shape errors


def test_unknown_keys_and_sections_are_reported_not_ignored():
    with pytest.raises(ConfigError) as key_err:
        parse_config("[rules]\nwindow_dayz = 21\n")
    assert "window_dayz" in key_err.value.message
    with pytest.raises(ConfigError):
        parse_config("[nonsense]\na = 1\n")


def test_wrong_types_are_configuration_errors():
    bad("rules", "window_days", '"21"')
    bad("rules", "window_days", "true")  # a bool is not a number
    bad("rules", "window_days", "21.5")
    bad("eduvidual", "enabled", '"yes"')
    bad("retry", "read_timeout", '"fast"')
    bad("eduvidual", "courses", '"all"')
    bad("google", "calendar_name", "5")
    bad("retry", "delays_seconds", '["a"]')


def test_invalid_toml_and_a_scalar_instead_of_a_section_are_configuration_errors():
    with pytest.raises(ConfigError):
        parse_config("[rules\n")
    with pytest.raises(ConfigError):
        parse_config("rules = 5\n")


def test_load_config_reports_a_missing_file_with_the_next_step(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_config(tmp_path / "config.toml")
    assert "schulsync setup" in (err.value.hint or "")


def test_set_config_value_keeps_comments_and_validates_when_loaded_again(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# mein Kommentar\n[google]\ncalendar_id = \"\"  # wird gesetzt\n", encoding="utf-8")
    set_config_value(path, "google", "calendar_id", "abc@group.calendar.google.com")
    set_config_value(path, "retry", "run_timeout_seconds", 900)
    text = path.read_text(encoding="utf-8")
    assert "# mein Kommentar" in text
    cfg = load_config(path)
    assert cfg.google.calendar_id == "abc@group.calendar.google.com" and cfg.retry.run_timeout_seconds == 900.0
