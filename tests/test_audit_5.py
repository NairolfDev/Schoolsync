"""Requirements audit 5 (ANFORDERUNGEN sections 8, 9, 10): tests that demonstrate gaps.

Every test in this file is expected to FAIL on the current code, for the reason stated in its docstring. They are
deterministic (injected clock/opener, FakeGoogle, no network). Probes that passed (report content, tab-flood
protection, escaping, secret masking, preview bookkeeping, retention in the runner, DPAPI store) are NOT kept here.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from schulsync.cli import CliEnv, main
from schulsync.errors import NetworkError
from schulsync.logging_setup import shutdown_logging
from schulsync.timeutil import VIENNA
from tests.support.acceptance_harness import (
    ASSIGNMENT,
    World,
    at,
    fail_adapter,
    read_report,
    world,  # noqa: F401  (fixture)
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "schulsync"


# --------------------------------------------------------------------------- 9: German README (setup instructions)


def _readme() -> str:
    path = ROOT / "README.md"
    assert path.is_file(), (
        "README.md fehlt: ANFORDERUNGEN 9 verlangt 'eine deutsche README', die Einrichtungsanleitung ist Teil der Abgabe "
        "(pyproject.toml deklariert readme = 'README.md', cli.py verweist zweimal auf 'die README')"
    )
    return path.read_text(encoding="utf-8")


def test_readme_explains_the_google_cloud_setup_and_the_seven_day_testing_limit():
    """ANFORDERUNGEN 4 + 9: 'Dokumentiere Einrichtung von Google-Cloud-Projekt, Calendar API, Tasks API und Desktop-OAuth-
    Zugang verstaendlich' and the 7-day refresh-token expiry of an external app in 'Testing' (with a setup that suits
    permanent use). The wizard (`cli._setup_google`) tells the user 'Details stehen in der README' - there is none."""
    text = _readme()
    low = text.lower()
    for needle in ("google cloud", "calendar api", "tasks api", "desktop", "testing", "in produktion"):
        assert needle in low, f"README erklaert '{needle}' nicht"
    assert re.search(r"(7|sieben)\s*tage", low), "README nennt den 7-Tage-Ablauf im Status 'Testing' nicht"
    assert "oauth" in low and ("client" in low), "README beschreibt den OAuth-Client (JSON-Datei) nicht"


def test_readme_covers_installation_the_five_commands_and_the_honest_limits():
    """ANFORDERUNGEN 9/10: simple German instructions for install, `setup`, automatic start, manual start, `check` and the
    preview without Google writes, plus 'melde genau, welche lokale Einrichtung oder reale Pruefung noch fehlt' (nothing
    was ever run against real Google/Eduvidual/WebUntis accounts: `schulsync check` is the real test)."""
    text = _readme()
    low = text.lower()
    for cmd in ("schulsync setup", "schulsync check", "schulsync preview", "schulsync run", "schulsync autostart install"):
        assert cmd in low, f"README nennt '{cmd}' nicht"
    assert "install.ps1" in low or "pip install" in low, "README erklaert die Installation nicht"
    assert "python" in low
    assert "echt" in low, "README benennt nicht, was nur gegen synthetische Daten geprueft ist und real noch geprueft werden muss"


# --------------------------------------------------------------------------- 8: 'Welche Daten bereits erfolgreich uebernommen wurden'


def test_a_retry_report_of_the_same_day_still_tells_what_was_already_taken_over(world: World):
    """ANFORDERUNGEN 8: the report must show 'Welche Daten bereits erfolgreich uebernommen wurden'. The report file is one
    per day and is OVERWRITTEN by every later start of that day (DESIGN 9) - and that is the file `schulsync report`
    opens. After logon 1 (Eduvidual taken over, WebUntis down) logon 2 fails the same way: Eduvidual is 'bereits
    erledigt' and the section 'Bereits erfolgreich uebernommen' says 'Keine.', although the Eduvidual entry from
    logon 1 is in Google. The section only lists the changes of THIS start."""
    world.load_standard()
    fail_adapter(world.webuntis, NetworkError("Keine Verbindung (Test)."), times=500)

    first = world.start(at=at(2026, 10, 14, 8, 0))
    assert ASSIGNMENT.title in read_report(first)  # logon 1 names it
    second = world.start(at=at(2026, 10, 14, 9, 0))

    assert second.report_path == first.report_path  # the day's single report file
    html = read_report(second)
    assert len(world.rec.opened) == 1  # same problem: no second tab, so this overwritten file is all the user can read later
    assert ASSIGNMENT.title in html, "der Bericht des zweiten Starts nennt die schon uebernommene Eduvidual-Abgabe nicht mehr"


# --------------------------------------------------------------------------- 9: 'saubere Abhaengigkeiten'


_DIST_OF_MODULE = {
    "google": "google-auth",  # google.auth / google.oauth2 (namespace package)
    "googleapiclient": "google-api-python-client",
    "google_auth_httplib2": "google-auth-httplib2",
    "google_auth_oauthlib": "google-auth-oauthlib",
}


def _imported_third_party_modules() -> dict[str, str]:
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top not in stdlib and top != "schulsync":
                    found.setdefault(top, path.name)
    return found


def test_every_directly_imported_third_party_package_is_declared_in_pyproject():
    """ANFORDERUNGEN 9: 'saubere Abhaengigkeiten'. `google_auth.py`/`google_client.py` import `httplib2` and
    `google_auth.py` imports `oauthlib` directly, but pyproject.toml lists neither (they only arrive transitively through
    google-api-python-client / google-auth-oauthlib, so a changed dependency tree would break the program at import time).
    pyproject.toml is a contract file: this test documents the needed change."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    declared = {re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0].lower().replace("_", "-") for d in deps}
    undeclared = {
        mod: where
        for mod, where in _imported_third_party_modules().items()
        if _DIST_OF_MODULE.get(mod, mod).lower().replace("_", "-") not in declared
    }
    assert undeclared == {}, f"direkt importiert, aber nicht in pyproject.toml deklariert: {undeclared}"


# --------------------------------------------------------------------------- 8: 'Berichte und Logs begrenzt aufbewahren'


def test_a_failure_report_written_outside_the_runner_also_ages_out_old_reports(paths):
    """ANFORDERUNGEN 8: 'Berichte und Logs begrenzt aufbewahren'. Pruning of reports happens only in `runner._housekeeping`;
    `run --auto` that fails BEFORE the runner (no/broken config, unreadable secret store: `cli._auto_failure_report`) writes
    a new `bericht-<day>.html` per day but never removes old ones, so a permanently broken installation grows the folder
    without limit (logs are pruned in `setup_logging`, reports are not)."""
    paths.ensure()
    old = paths.reports / "bericht-2026-08-01.html"  # 74 days old, keep_days defaults to 30
    old.write_text("alt", encoding="utf-8")
    env = CliEnv(
        say=lambda text="": None,
        ask=lambda prompt: "",
        ask_secret=lambda prompt: "",
        clock=lambda: datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA),
        paths=paths,
        open_report=lambda path: True,
    )
    try:
        assert main(["run", "--auto"], env=env) == 2  # no config.toml: a failure outside the runner
    finally:
        shutdown_logging()

    assert (paths.reports / "bericht-2026-10-14.html").is_file()  # the new failure report exists ...
    assert not old.exists(), "der 74 Tage alte Bericht bleibt liegen: Aufbewahrung ist nur im Runner begrenzt"

