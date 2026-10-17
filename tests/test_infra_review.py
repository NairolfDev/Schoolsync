"""Adversarial review tests for the infra module (redact, logging_setup). Each test demonstrates one defect."""

from __future__ import annotations

import logging
import time

import pytest

from schulsync.config import Paths
from schulsync.logging_setup import setup_logging, shutdown_logging
from schulsync.redact import clear_registered_secrets, redact, register_secret


@pytest.fixture(autouse=True)
def _clean():
    clear_registered_secrets()
    yield
    clear_registered_secrets()
    shutdown_logging()


def test_html_hidden_input_sesskey_is_masked():
    """Moodle pages carry the session key as <input name="sesskey" value="...">; a logged HTML snippet leaks it."""
    out = redact('<input type="hidden" name="sesskey" value="AbC123xYz9">')
    assert "AbC123xYz9" not in out


def test_html_password_input_value_is_masked():
    out = redact('<input type="password" name="password" value="hunter2xyz">')
    assert "hunter2xyz" not in out


def test_unquoted_js_key_with_colon_is_masked():
    """M.cfg-style JS object literal: sesskey:"..." (unquoted key, colon)."""
    out = redact('M.cfg = {wwwroot:"https://x", sesskey:"AbC123xYz9"}')
    assert "AbC123xYz9" not in out


@pytest.mark.parametrize("text", ["authToken: abcdef123456", "password: hunter2xyz", "sesskey: AbCdEf1234"])
def test_plain_colon_form_of_strong_key_is_masked(text):
    secret = text.split(": ")[1]
    assert secret not in redact(text)


def test_cookie_name_value_pair_dump_is_masked():
    """Cookie jars are often dumped as [{'name': 'JSESSIONID', 'value': '...'}] (server-issued, never registered)."""
    out = redact('{"cookies":[{"name":"JSESSIONID","value":"ABCDEF123456"}]}')
    assert "ABCDEF123456" not in out


def test_pkce_code_verifier_is_masked():
    out = redact("code_verifier=abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG")
    assert "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG" not in out


def test_redact_is_linear_on_repeated_moodlesession_keyword():
    """The key-name pattern is quadratic for `moodlesession<alnum>*` repeated without separators."""
    text = "moodlesession" * 4000  # 52k chars
    t0 = time.perf_counter()
    redact(text)
    assert time.perf_counter() - t0 < 2.0


def test_logging_call_never_raises_when_argument_str_is_broken(tmp_path):
    """A log call must never crash the app; stdlib routes formatting errors to handleError, the filter's fallback raises."""

    class Bad:
        def __str__(self):
            raise RuntimeError("boom")

        __repr__ = __str__

    register_secret("topsecret99")
    log = setup_logging(Paths(tmp_path))
    log.info("value %s", Bad())  # must not raise
