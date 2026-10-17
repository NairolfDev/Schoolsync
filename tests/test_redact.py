"""Tests for schulsync.redact. All secret values are obvious dummies; shapes are realistic."""

from __future__ import annotations

import base64
import logging
from urllib.parse import quote, quote_plus

import pytest

from schulsync import redact as r


@pytest.fixture(autouse=True)
def _clean():
    r.clear_registered_secrets()
    yield
    r.clear_registered_secrets()


# --------------------------------------------------------------------------- registered values


def test_registered_secret_is_masked_everywhere():
    r.register_secret("hunter2-dummy")
    assert r.redact("Anmeldung mit hunter2-dummy fehlgeschlagen (hunter2-dummy)") == (
        "Anmeldung mit *** fehlgeschlagen (***)"
    )


def test_registered_secret_encoded_forms_are_masked():
    secret = "p@ss w/rd+dummy&1"
    r.register_secret(secret)
    raw = secret.encode()
    forms = [
        quote(secret, safe=""),
        quote_plus(secret),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    ]
    for form in forms:
        assert r.redact(f"x {form} y") == "x *** y", form


def test_json_escaped_form_is_masked():
    r.register_secret('pa"ss\\dummy')
    assert "pa" not in r.redact('{"x": "pa\\"ss\\\\dummy"}')


def test_short_values_are_ignored_and_several_can_be_registered():
    r.register_secret("abc")  # < 4 chars: ignored, otherwise "abc" would vanish everywhere
    r.register_secret("")
    r.register_secret(None)  # type: ignore[arg-type]
    assert r.redact("abc def") == "abc def"
    r.register_secret("alpha-dummy")
    r.register_secret("beta-dummy")
    assert r.redact("alpha-dummy / beta-dummy / gamma") == "*** / *** / gamma"


def test_longer_secret_is_not_cut_by_shorter_overlapping_one():
    r.register_secret("geheim")
    r.register_secret("geheim-lang-dummy")
    assert r.redact("wert geheim-lang-dummy ende") == "wert *** ende"


def test_clear_registered_secrets():
    r.register_secret("hunter2-dummy")
    r.clear_registered_secrets()
    assert r.redact("hunter2-dummy") == "hunter2-dummy"


# --------------------------------------------------------------------------- generic patterns


@pytest.mark.parametrize(
    "text, secret",
    [
        ("password=hunter2-dummy", "hunter2-dummy"),
        ("j_password=hunter2-dummy&j_username=max", "hunter2-dummy"),
        ("POST /login username=max.muster&password=hunter2-dummy&logintoken=Ab12Cd34Ef56", "hunter2-dummy"),
        ("POST /login username=max.muster&password=hunter2-dummy&logintoken=Ab12Cd34Ef56", "Ab12Cd34Ef56"),
        ("passwd = 'hunter2-dummy'", "hunter2-dummy"),
        ("new_password=hunter2-dummy", "hunter2-dummy"),
        ('{"password": "hunter2-dummy", "user": "max"}', "hunter2-dummy"),
        ("{'password': 'hunter2-dummy'}", "hunter2-dummy"),
        ('{"j_password":"hunter2\\"dummy"}', "hunter2"),
        ("wstoken=0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
        ("https://eduvidual.example/webservice/rest/server.php?wstoken=0123456789abcdef&moodlewsrestformat=json",
         "0123456789abcdef"),
        ('{"token":"0123456789abcdef0123456789abcdef","privatetoken":null}', "0123456789abcdef0123456789abcdef"),
        ("sesskey=Qw3rTy9Zx1", "Qw3rTy9Zx1"),
        ("authtoken=abc123def456", "abc123def456"),
        ("access_token=ya29.a0AfH6SMBdummy-token_value", "ya29.a0AfH6SMBdummy-token_value"),
        ('{"refresh_token": "1//0gDummyRefreshTokenValue1234567890abcd"}', "1//0gDummyRefreshTokenValue1234567890abcd"),
        ('{"id_token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"}', "eyJhbGciOiJSUzI1NiJ9"),
        ("client_secret=GOCSPX-DummyDummyDummy1234", "GOCSPX-DummyDummyDummy1234"),
        ('{"installed": {"client_secret": "GOCSPX-DummyDummyDummy1234"}}', "GOCSPX-DummyDummyDummy1234"),
        ("secret=topsecret-dummy", "topsecret-dummy"),
        ("api_key=AIzaDummyKey123456", "AIzaDummyKey123456"),
        ("GET /x?key=AIzaDummyKey123456 HTTP/1.1", "AIzaDummyKey123456"),
        ("redirect http://localhost:8080/?state=st4te-dummy&code=4/0AXdummyCode&scope=x", "4/0AXdummyCode"),
        ("redirect http://localhost:8080/?state=st4te-dummy&code=4/0AXdummyCode&scope=x", "st4te-dummy"),
        ("Authorization: Bearer ya29.a0AfH6SMBdummy-token_value", "ya29.a0AfH6SMBdummy-token_value"),
        ("authorization: Basic bWF4Lm11c3RlcjpodW50ZXIyLWR1bW15", "bWF4Lm11c3RlcjpodW50ZXIyLWR1bW15"),
        ('{"Authorization": "Bearer abc.def-ghi_jkl123"}', "abc.def-ghi_jkl123"),
        ("headers={'Authorization': 'Basic bWF4Om5pY2h0cw=='}", "bWF4Om5pY2h0cw"),
        ("Bearer abcdef1234567890 wurde abgelehnt", "abcdef1234567890"),
        ("Cookie: MoodleSessioneduvidual=a1b2c3d4e5f6; MOODLEID1_=%25DA", "a1b2c3d4e5f6"),
        ("Set-Cookie: JSESSIONID=9F8E7D6C5B4A; Path=/WebUntis; Secure; HttpOnly", "9F8E7D6C5B4A"),
        ("cookies: MoodleSessioneduvidual=a1b2c3d4e5f6 und mehr", "a1b2c3d4e5f6"),
        ("JSESSIONID=9F8E7D6C5B4A.worker1", "9F8E7D6C5B4A.worker1"),
        ("X-CSRF-TOKEN: 6b1c2d3e-aaaa-bbbb-cccc-0123456789ab", "6b1c2d3e-aaaa-bbbb-cccc-0123456789ab"),
        ("GET https://max:hunter2-dummy@proxy.example/x", "hunter2-dummy"),
        ("GET https://tok3n-dummy@host.example/x", "tok3n-dummy"),
        ("https://eduvidual.example/tokenpluginfile.php/0123456789abcdef/12/mod_assign/intro/a.pdf", "0123456789abcdef"),
        ("fehler: ya29.a0AfH6SMBdummy-token_value abgelehnt", "ya29.a0AfH6SMBdummy-token_value"),
    ],
)
def test_generic_patterns_mask_secret(text: str, secret: str):
    out = r.redact(text)
    assert secret not in out, out
    assert "***" in out


def test_masking_keeps_surrounding_structure():
    assert r.redact("a=1&password=x1y2z3w&b=2") == "a=1&password=***&b=2"
    assert r.redact('{"password": "x1y2z3w", "user": "max"}') == '{"password": "***", "user": "max"}'
    assert r.redact("Authorization: Bearer abcdef123456\nnächste Zeile") == "Authorization: ***\nnächste Zeile"
    assert r.redact("https://max:pw12345@host.example/p?x=1") == "https://***@host.example/p?x=1"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        # HTML inputs (Moodle pages, login form), any attribute order / quote style
        ('<input type="hidden" name="sesskey" value="AbC123xYz9">', "AbC123xYz9"),
        ('<input value="AbC123xYz9" name="sesskey" type=hidden>', "AbC123xYz9"),
        ("<input name='password' value='hunter2xyz'/>", "hunter2xyz"),
        ('<input type="password" name="pw_field" value="hunter2xyz">', "hunter2xyz"),
        # colon forms
        ('M.cfg = {wwwroot:"https://x", sesskey:"AbC123xYz9"}', "AbC123xYz9"),
        ("authToken: abcdef123456", "abcdef123456"),
        # cookie jars as name/value pairs, either key order, extra keys in between
        ('[{"name":"JSESSIONID","value":"ABCDEF123456"}]', "ABCDEF123456"),
        ("[{'name': 'MoodleSessionedu', 'value': 'abc123xyz', 'path': '/'}]", "abc123xyz"),
        ('{"domain":"x","value":"ABCDEF123456","name":"JSESSIONID","path":"/"}', "ABCDEF123456"),
        # PKCE
        ('{"code_verifier": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"}', "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"),
    ],
)
def test_html_colon_cookie_pair_and_pkce_shapes_are_masked(text: str, secret: str):
    out = r.redact(text)
    assert secret not in out, out
    assert "***" in out


@pytest.mark.parametrize(
    "text",
    [
        '<input type="text" name="username" value="max.muster">',
        "<input name='q' value='suche'>",
        '[{"name":"theme","value":"dark"}]',
        '{"name":"JSESSIONID"}',
        "Passwort-Hinweis: mindestens 8 Zeichen",
        "Zeitüberschreitung bei tokens: 3",
    ],
)
def test_new_shapes_do_not_mask_harmless_text(text: str):
    assert r.redact(text) == text


@pytest.mark.parametrize("param", ["sig", "signature", "auth", "ticket", "sid", "session", "pw", "credential"])
def test_secret_looking_url_parameter_names_of_signed_or_session_urls_are_masked(param):
    """ANFORDERUNGEN 8: 'geheime URL-Parameter' dürfen weder im Bericht noch in Logs erscheinen. Masking is by NAME (a value is
    never recognisable): besides token/sesskey/password also the names of signed and session URLs. Neighbours stay readable."""
    out = r.redact(f"GET https://x.example/a?id=7&{param}=GEHEIMWERT123&view=1 HTTP/1.1")
    assert "GEHEIMWERT123" not in out and "id=7" in out and "view=1" in out, out


@pytest.mark.parametrize(
    "text",
    [
        "Die Sitzung (session) beginnt um 8 Uhr; Auth-Konzept: siehe Kapitel 2",
        "Unterschrift (Signatur) fehlt noch, das Ticket wird morgen bearbeitet",
        "https://example.at/mod/page?sidebar=1&author=Muster&ticketing=ja&signal=3",
    ],
)
def test_the_new_parameter_names_only_count_as_whole_names(text):
    assert r.redact(text) == text


def test_redact_is_idempotent():
    once = r.redact("password=hunter2-dummy&token=abcdef123456 Cookie: a=b")
    assert r.redact(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "Die Abgabe für Mathematik ist am Freitag, 16.10.2026 um 23:59 fällig.",
        "Prüfung: Netzwerktechnik, Raum 3.12, Prof. Muster",
        "Das Passwort wurde nicht akzeptiert. Bitte neu eingeben.",
        "Kein Token nötig; der Schlüssel zur Lösung steht in Kapitel 3: Grundlagen.",
        "Fehlercode 404 beim Abruf (http_code=404, event_state=ACTIVE, task_state=OPEN)",
        "Status: Anmeldung erfolgreich. Code geprüft, state bleibt gleich.",
        "Basic Konfiguration abgeschlossen, Bearer ist ein englisches Wort",
        "Cookie-Banner wurde ignoriert; Cookie: ",
        "https://www.eduvidual.at/mod/assign/view.php?id=4711&action=view",
        "Kontakt: lehrer@htl-beispiel.example, Telefon +43 7248 12345",
        "Keys und Codes werden nicht gespeichert, tokens=3 Stück gezählt",
        "Der Autor des Dokuments (Secretary) hat das Kennwort-Formular geändert.",
        "2026-10-14T08:00:00+02:00 Sync gestartet: 3 neue Termine, 2 Aufgaben",
        "Entfall: Mathematik 3AHITN, 5. Stunde, Zimmer 2.04",
        "",
    ],
)
def test_normal_text_is_unchanged(text: str):
    assert r.redact(text) == text


_PATHOLOGICAL = {
    "dash_runs": lambda: "a-" * 30_000,  # e.g. base64url data in a logged response body
    "dot_underscore_runs": lambda: "ab.cd_" * 30_000,
    "token_prefix_runs": lambda: "tok-" * 30_000 + "token",
    "token_runs": lambda: "token-" * 30_000,
    "x_header_runs": lambda: "x-" * 30_000,
    "x_token_runs": lambda: "x-token" * 10_000,
    "userinfo_runs": lambda: "a.b" * 30_000,
    "scheme_runs": lambda: "a+" * 30_000 + "b",
    "moodlesession_runs": lambda: "moodlesession" * 10_000,
    "unclosed_input_runs": lambda: "<input " * 30_000,
    "cookie_name_runs": lambda: '"name":"JSESSIONID",' * 10_000,
    "cookie_value_runs": lambda: '"value":"x",' * 20_000,
    "colon_runs": lambda: "password:" * 30_000,
}


@pytest.mark.parametrize("name", sorted(_PATHOLOGICAL))
def test_redact_is_linear_on_pathological_input(name: str):
    # Regression: key-name patterns used to be quadratic (12 000 chars of "ab.cd_" took ~6 s,
    # 120 000 chars ~9 min). Generous bound; linear time needs a few milliseconds.
    import time

    text = _PATHOLOGICAL[name]()
    start = time.perf_counter()
    r.redact(text)
    assert time.perf_counter() - start < 2.0


def test_redact_handles_large_input_quickly():
    text = ("Eintrag ohne Geheimnis; " * 50_000) + "password=hunter2-dummy"
    out = r.redact(text)
    assert out.endswith("password=***")


# --------------------------------------------------------------------------- logging filter


def _logger(stream_list: list[str]) -> logging.Logger:
    class ListHandler(logging.Handler):
        def emit(self, record):
            stream_list.append(self.format(record))

    handler = ListHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(r.RedactingFilter())
    log = logging.getLogger("schulsync.test_redact." + str(id(stream_list)))
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log


def test_filter_redacts_message_args_and_exception():
    r.register_secret("hunter2-dummy")
    lines: list[str] = []
    log = _logger(lines)
    log.info("login hunter2-dummy ok")
    log.info("login %s ok", "hunter2-dummy")
    log.info("login %(pw)s ok", {"pw": "hunter2-dummy"})
    log.info("params %s", {"password": "anders-dummy"})
    log.info("value %d", 5)
    try:
        raise RuntimeError("Antwort enthielt token=abcdef123456 und hunter2-dummy")
    except RuntimeError:
        log.exception("Abruf fehlgeschlagen")
    joined = "\n".join(lines)
    assert "hunter2-dummy" not in joined
    assert "abcdef123456" not in joined
    assert "anders-dummy" not in joined
    assert "INFO login *** ok" in lines[0]
    assert lines[1] == "INFO login *** ok"
    assert lines[2] == "INFO login *** ok"
    assert lines[4] == "INFO value 5"
    assert "Traceback" in joined and "RuntimeError" in joined  # traceback kept, just masked


def test_filter_redacts_exception_object_as_argument_and_chained_cause():
    r.register_secret("hunter2-dummy")
    lines: list[str] = []
    log = _logger(lines)
    try:
        try:
            raise ValueError("inner hunter2-dummy")
        except ValueError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as exc:
        log.warning("Fehler: %s", ValueError("arg hunter2-dummy"))
        log.error("mit stack", exc_info=exc, stack_info=True)
    assert "hunter2-dummy" not in "\n".join(lines)
    assert any("inner ***" in ln for ln in lines)


def test_filter_survives_broken_format_string():
    lines: list[str] = []
    log = _logger(lines)
    log.info("zwei Platzhalter %s %s", "password=hunter2-dummy")
    assert lines and "hunter2-dummy" not in lines[0]
