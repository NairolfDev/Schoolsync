"""Guards on what the delivery contains (ANFORDERUNGEN 9): no secrets in code/repository/example files, no AI services,
no paid infrastructure, time-zone data shipped with the program.

These are inspection results turned into tests, so that a later change cannot silently break them.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".py", ".md", ".toml", ".ps1", ".txt", ".json", ".html", ".cfg", ".ini", ".gitignore"}

# Shapes of real Google / bearer secrets. The tests of the redaction itself use obvious dummies and are not scanned.
SECRET_SHAPES = [
    re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}"),  # Google OAuth client secret
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),  # Google API key
    re.compile(r"ya29\.[0-9A-Za-z_-]{20,}"),  # Google access token
    re.compile(r"\b1//[0-9A-Za-z_-]{30,}"),  # Google refresh token
    re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\."),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwort|wstoken|sesskey|client_secret)\s*[=:]\s*[\"'][^\"'\s]{6,}[\"']"),  # a literal secret assignment
]
AI_PACKAGES = ("openai", "anthropic", "langchain", "llama", "transformers", "google-generativeai", "google.generativeai", "mistralai", "cohere", "ollama", "playwright")


def _shipped_files() -> list[Path]:
    files = [ROOT / "config.example.toml", ROOT / "pyproject.toml", ROOT / ".gitignore"]
    for folder in ("src", "docs", "scripts", "tests/fixtures"):
        files += [p for p in (ROOT / folder).rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES and "__pycache__" not in p.parts]
    readme = ROOT / "README.md"
    if readme.is_file():
        files.append(readme)
    return [p for p in files if p.is_file()]


def test_no_secret_shaped_string_in_the_shipped_files():
    hits = []
    for path in _shipped_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for shape in SECRET_SHAPES:
            for m in shape.finditer(text):
                hits.append(f"{path.relative_to(ROOT)}: {m.group(0)[:12]}...")
    assert hits == []


def test_the_secret_scan_would_notice_a_secret():
    """The scan itself is not blind: every shape matches a typical real-looking value."""
    samples = [
        "GOCSPX-abcdefghijklmnopqrstuvwxyz12",
        "AIza" + "x" * 35,
        "ya29." + "a" * 30,
        "1//0g" + "b" * 40,
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl",
        "-----BEGIN PRIVATE KEY-----",
        'password = "correct-horse"',
    ]
    for sample in samples:
        assert any(s.search(sample) for s in SECRET_SHAPES), sample


def test_the_gitignore_keeps_local_secrets_and_state_out_of_the_repository():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("config.toml", "secrets.dat", "client_secret*.json", "token*.json", "*.sqlite3"):
        assert pattern in ignore, pattern


def test_tzdata_is_declared_so_the_vienna_time_zone_also_exists_on_windows():
    """Windows has no system time-zone database: `zoneinfo` needs the `tzdata` package. Installed on this machine is not
    enough, it has to be a declared dependency (ANFORDERUNGEN 9: 'Zeitzonendaten auch unter Windows mitliefern')."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert any(d.lower().startswith("tzdata") for d in deps)


def test_no_ai_service_and_no_browser_automation_is_declared_or_imported():
    """ANFORDERUNGEN 9: 'keine KI-Dienste voraussetzen'; DESIGN: no Playwright. Checked in the declared dependencies and in
    every import of the program."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = [d.lower() for d in project["dependencies"] + [x for group in project.get("optional-dependencies", {}).values() for x in group]]
    imports: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        imports += re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", path.read_text(encoding="utf-8"), re.M)
    for name in AI_PACKAGES:
        assert not any(d.startswith(name) for d in declared), name
        assert not any(i == name or i.startswith(name + ".") for i in imports), name


# --------------------------------------------------------------------------- the German README (ANFORDERUNGEN 9)


def _readme() -> str:
    path = ROOT / "README.md"
    assert path.is_file(), "README.md fehlt"
    return path.read_text(encoding="utf-8")


def test_the_readme_names_every_command_and_option_of_the_command_line():
    """The README is the only place a user reads how to start the program: a new command or option that is not described
    there is a documentation gap."""
    import argparse

    from schulsync.cli import _build_parser

    text = _readme()
    parser = _build_parser(lambda _text: None)
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name, command in sub.choices.items():
        assert f"schulsync {name}" in text, f"README nennt 'schulsync {name}' nicht"
        for action in command._actions:
            for option in action.option_strings:
                if option in ("-h", "--help", "--home", "-v", "--verbose"):
                    continue
                assert option in text, f"README nennt {option} ({name}) nicht"
    for action in ("install", "remove", "status"):
        assert f"schulsync autostart {action}" in text


def test_everything_the_readme_points_to_exists():
    text = _readme()
    for rel in re.findall(r"`((?:docs/|scripts\|scripts/)[\w./\-]+)`", text) + ["scripts/install.ps1", "config.example.toml"]:
        assert (ROOT / rel.replace("\\", "/")).exists(), rel
    for left_over in ("TODO", "FIXME", "XXX"):
        assert left_over not in text


def test_the_readme_gives_no_promise_of_an_unlimited_login_or_of_retroactive_reminders():
    low = _readme().lower()
    assert "unbegrenzt" not in low.replace("nicht unbegrenzt", "").replace("keine unbegrenzte", "")
    assert "keine rückwirkende benachrichtigung" in low  # the honest statement is there, not the promise
    assert "benachrichtigungseinstellungen" in low  # and the dependence on the calendar app
