"""Audit of ANFORDERUNGEN section 1 (Windows-Start und taeglicher Ablauf): gaps found by reading code and tests.

Every test here FAILS today and fails for the stated reason (the gap), not because of the test set-up. Probes that passed
(preview leaves every table of the real database byte-identical, also on the first ever preview and after a target
change; the generated task XML is accepted by the real Task Scheduler parser) are not kept in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schulsync.config import parse_config
from schulsync.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- 1.4: "dokumentierte" Autostart-Alternative


def test_the_user_documentation_explains_autostart_install_remove_and_the_startup_shortcut_fallback():
    """ANFORDERUNGEN 1: simple installation AND removal of the autostart; if the scheduler is impossible, a *documented*
    user startup shortcut. The README (ANFORDERUNGEN 9, German) is the only place a user reads that; it does not exist
    (pyproject.toml even declares readme = "README.md"). Today the wording lives only in code docstrings and CLI output."""
    readme = ROOT / "README.md"
    assert readme.is_file(), "README.md fehlt"
    text = readme.read_text(encoding="utf-8").lower()
    assert "autostart install" in text and "autostart remove" in text
    assert "startup" in text or "shell:startup" in text or "autostart-ordner" in text  # the shortcut alternative
    assert "anmeldung" in text and "aufgabenplanung" in text


# --------------------------------------------------------------------------- 1.7: Zeitlimits sind konfigurierbar, aber ungeprueft


@pytest.mark.parametrize("key, value", [("run_timeout_seconds", 0), ("run_timeout_seconds", -5)])
def test_senseless_time_limits_are_a_configuration_error_not_a_run_that_always_times_out(key, value):
    """`_validate` checks the HTTP limits (> 0) but not `retry.run_timeout_seconds`. With
    `run_timeout_seconds = 0` the runner's first check (`_timed_out`) makes EVERY source fail with 'run_timeout' at every
    logon, and the watchdog would fire after 60 s: the user gets a permanently failing program from a one-character typo
    instead of a German configuration error."""
    with pytest.raises(ConfigError):
        parse_config(f"[retry]\n{key} = {value}\n")
