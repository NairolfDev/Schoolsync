"""Secret masking for logs, reports and error texts.

Two layers, both replace with ``***``:

1. Exact values: every secret that is loaded at runtime is registered with
   :func:`register_secret`. Its URL-encoded, base64 and JSON-escaped forms are masked too.
2. Generic patterns (defense in depth for values we do not know, e.g. server-issued session
   cookies): ``key=value``, ``"key": "value"``, Authorization/Cookie headers, URL userinfo,
   query parameters and a few well-known token shapes.

Honest limits (see tests/test_redact.py): only the *shapes* listed here are covered. A secret that
appears in free text under an unknown key name and was never registered is NOT masked. The
patterns are deliberately conservative towards over-masking for the listed key names; ordinary
German sentences do not contain ``key=value`` or ``"key": "value"`` forms and stay unchanged.

Token shapes that are matched without a key name (``ya29.``, ``GOCSPX-``, ``1//``, JWT ``eyJ``) come
from Google's/OAuth's publicly observable token formats; they are heuristics, not a specification.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import traceback
from urllib.parse import quote, quote_plus

MASK = "***"
MIN_SECRET_LENGTH = 4

# Key names (case-insensitive) whose values are secrets. Exported so the secret store can pick the
# sensitive leaves out of JSON secrets (OAuth client file, token file).
SENSITIVE_KEY_NAMES = (
    "password", "passwd", "j_password", "token", "access_token", "refresh_token", "id_token",
    "wstoken", "authtoken", "sesskey", "client_secret", "secret", "api_key", "key", "code", "state",
    "code_verifier",
)

_lock = threading.Lock()
_secrets: set[str] = set()
_secret_rx: re.Pattern[str] | None = None


def _variants(value: str) -> set[str]:
    raw = value.encode("utf-8")
    out = {value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1],
           json.dumps(value, ensure_ascii=False)[1:-1]}
    for enc in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        s = enc.decode("ascii")
        out.add(s)
        out.add(s.rstrip("="))
    return {v for v in out if len(v) >= MIN_SECRET_LENGTH}


def register_secret(value: str | None) -> None:
    """Mask `value` (and its encoded forms) from now on. Values shorter than 4 chars are ignored."""
    global _secret_rx
    if not isinstance(value, str) or len(value) < MIN_SECRET_LENGTH:
        return
    with _lock:
        _secrets.update(_variants(value))
        _secret_rx = None


def clear_registered_secrets() -> None:
    global _secret_rx
    with _lock:
        _secrets.clear()
        _secret_rx = None


def _registered_regex() -> re.Pattern[str] | None:
    global _secret_rx
    with _lock:
        if not _secrets:
            return None
        if _secret_rx is None:
            # Longest first so a longer variant is never cut short by a shorter one.
            _secret_rx = re.compile("|".join(re.escape(s) for s in sorted(_secrets, key=len, reverse=True)))
        return _secret_rx


# --------------------------------------------------------------------------- generic patterns

_QUOTED = r"""\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'"""
# Strong keys: also match as suffix of an identifier (new_password, X-Auth-Token, oauth_token,
# wstoken, MoodleSessioneduvidual). `[A-Za-z0-9]*` before the base word covers glued names.
#
# Performance: a match may only start at the beginning of an identifier run (lookbehind excludes the
# run characters) and the lazy prefix then scans that run once. A prefix loop like
# `(?:[A-Za-z0-9]+[_.-])*` started at every position was quadratic on long `a.b_c-d...` strings
# (12 000 chars took ~6 s), which a logged response body can easily contain.
_STRONG = (
    r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*?"
    r"(?:password|passwd|passwort|kennwort|pwd|token|secret|sesskey|api[_-]?key|session[_-]?id"
    r"|jsessionid|phpsessid|code[_-]?verifier|moodlesession[A-Za-z0-9]{0,40})(?![A-Za-z0-9])"
)
# `moodlesession` + site suffix: Moodle names the cookie "MoodleSession" + $CFG->sessioncookie
# (lib/setup.php). The suffix is capped at 40 chars: an unbounded `[A-Za-z0-9]*` rescanned the rest of
# the identifier run at every occurrence (quadratic on "moodlesession" * n). code_verifier is the
# PKCE secret of the Google console flow (RFC 7636 section 4.1).
# Weak keys are common words: only the whole identifier counts (so `event_state`, `http_code`,
# `Fehlercode` stay untouched) and only the `=` form (no spaces) is used outside quoted keys.
_WEAK = (
    r"(?<![A-Za-z0-9_.-])(?:key|code|state|secret_key|access_key|private_key|auth_key"
    r"|sig|signature|auth|ticket|sid|session|pw|credential|credentials)(?![A-Za-z0-9_])"  # signed-URL / session style parameter names
)
_HEADERISH = r"authorization|proxy-authorization|cookie|set-cookie"

_PLAIN_VALUE = r"""[^\s&;"']+"""

_P_QUOTED_KEY = re.compile(
    rf"""(?P<pre>(?P<q>["'])(?:{_STRONG}|{_WEAK}|{_HEADERISH})(?P=q)\s*[:=]\s*)(?P<val>{_QUOTED}|[^\s,}}\])]+)""",
    re.I,
)
# `=` and `:` forms: `password=x`, `password: x`, JS literal `sesskey:"x"` (Moodle M.cfg), `authToken: x`.
_P_STRONG_KV = re.compile(rf"(?P<pre>{_STRONG}[ \t]*[=:][ \t]*)(?P<val>{_QUOTED}|{_PLAIN_VALUE})", re.I)
# Cookie jars dumped as name/value pairs: {"name":"JSESSIONID", ..., "value":"X"} in either key order.
# Server-issued session cookies are never registered, so this shape is the only protection. The gap
# between the two keys is bounded and brace-free (stays inside one object, linear time).
_COOKIE_NAME = rf"(?P<q>[\"'])name(?P=q)\s*:\s*(?P<q2>[\"']){_STRONG}(?P=q2)"
_COOKIE_VALUE = rf"(?P<q3>[\"'])value(?P=q3)\s*:\s*"
_P_COOKIE_PAIR = re.compile(
    rf"(?P<pre>{_COOKIE_NAME}[^{{}}]{{0,500}}?{_COOKIE_VALUE})(?P<val>{_QUOTED})", re.I
)
_P_COOKIE_PAIR_REV = re.compile(
    rf"(?P<pre>{_COOKIE_VALUE})(?P<val>{_QUOTED})(?=[^{{}}]{{0,500}}?{_COOKIE_NAME})", re.I
)
# HTML inputs: <input type="hidden" name="sesskey" value="..."> and <input type="password" ...>.
# `[^<>]*` keeps the scan inside one tag (linear time even for many unclosed `<input`).
_P_INPUT_TAG = re.compile(r"(?i)<input\b[^<>]*>")
_RX_ATTR_NAME = re.compile(r"""(?i)\bname\s*=\s*["']([^"']*)["']""")
_RX_ATTR_TYPE_PASSWORD = re.compile(r"""(?i)\btype\s*=\s*["']?password\b""")
_RX_ATTR_VALUE = re.compile(rf"""(?i)(\bvalue\s*=\s*)(?:{_QUOTED}|[^\s>"']+)""")
_RX_SENSITIVE_NAME = re.compile(rf"(?i)(?:{_STRONG}|{_WEAK})")


def _mask_input_tag(m: re.Match[str]) -> str:
    tag = m.group(0)
    name = _RX_ATTR_NAME.search(tag)
    if not (_RX_ATTR_TYPE_PASSWORD.search(tag) or (name and _RX_SENSITIVE_NAME.fullmatch(name.group(1)))):
        return tag
    return _RX_ATTR_VALUE.sub(lambda v: v.group(1) + f'"{MASK}"', tag)
_P_WEAK_KV = re.compile(rf"(?P<pre>{_WEAK}=)(?P<val>{_QUOTED}|{_PLAIN_VALUE})", re.I)
# Header lines in plain text: Authorization (incl. Basic), Cookie / Set-Cookie (whole line).
_P_AUTH_HEADER = re.compile(r"(?im)(\b(?:proxy-)?authorization\b[ \t]*[:=][ \t]*)[^\s][^\r\n]*")
_P_COOKIE_HEADER = re.compile(r"(?im)(\b(?:set-)?cookie\b[ \t]*:[ \t]*)[^\s][^\r\n]*")
# CSRF/auth style custom headers, e.g. `X-CSRF-TOKEN: abc` (WebUntis csrfHeader).
# Matched as "whole header name + colon" and the keyword is checked in the callback (linear time).
_P_X_HEADER = re.compile(r"(?i)(?<![a-z0-9_-])(?P<name>x-[a-z0-9-]+)(?P<pre>[ \t]*:[ \t]*)\S+")
_X_HEADER_KEYWORDS = ("token", "csrf", "secret", "auth", "api-key")


def _mask_x_header(m: re.Match[str]) -> str:
    if any(k in m.group("name").lower() for k in _X_HEADER_KEYWORDS):
        return m.group("name") + m.group("pre") + MASK
    return m.group(0)
_P_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_P_USERINFO = re.compile(r"(?P<scheme>(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@?#]+@")
# Moodle's tokenpluginfile.php carries the web service token as a path segment (defensive; unverified).
_P_TOKEN_PATH = re.compile(r"(?i)(tokenpluginfile\.php/)[^/\s?#]+")
_P_SHAPES = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"  # JWT
    r"|ya29\.[A-Za-z0-9_-]{10,}"  # Google access token
    r"|GOCSPX-[A-Za-z0-9_-]{10,}"  # Google OAuth client secret
    r"|(?<![A-Za-z0-9/])1//[A-Za-z0-9_-]{30,}"  # Google refresh token
)


def _mask_value(m: re.Match[str]) -> str:
    val = m.group("val")
    return m.group("pre") + (f"{val[0]}{MASK}{val[0]}" if val[:1] in ("'", '"') else MASK)


_PATTERNS: tuple[tuple[re.Pattern[str], object], ...] = (
    (_P_USERINFO, lambda m: m.group("scheme") + MASK + "@"),
    (_P_AUTH_HEADER, lambda m: m.group(1) + MASK),
    (_P_COOKIE_HEADER, lambda m: m.group(1) + MASK),
    (_P_X_HEADER, _mask_x_header),
    (_P_BEARER, lambda m: m.group(1) + MASK),
    (_P_INPUT_TAG, _mask_input_tag),
    (_P_COOKIE_PAIR, _mask_value),
    (_P_COOKIE_PAIR_REV, _mask_value),
    (_P_QUOTED_KEY, _mask_value),
    (_P_STRONG_KV, _mask_value),
    (_P_WEAK_KV, _mask_value),
    (_P_TOKEN_PATH, lambda m: m.group(1) + MASK),
    (_P_SHAPES, lambda m: MASK),
)


def redact(text: str) -> str:
    """Return `text` with registered secrets and generic secret patterns replaced by ``***``."""
    if not text:
        return text
    rx = _registered_regex()
    out = rx.sub(MASK, text) if rx else text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


class RedactingFilter(logging.Filter):
    """Redacts message, arguments and exception/stack text of a record.

    Attach to HANDLERS (not loggers): logger filters are not applied to records of child loggers.
    The fully formatted message is redacted once and `args` is dropped, so `%s` arguments (including
    exception objects and dict reprs) cannot leak. Extra attributes passed via ``extra=`` are not
    inspected.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # broken format string: still never emit the raw args
            try:
                message = f"{record.msg} {record.args}"
            except Exception:  # str()/repr() of an argument raises too: a log call must never crash the app
                message = "<Logmeldung nicht formatierbar>"
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True
