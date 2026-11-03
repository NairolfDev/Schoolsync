"""`schulsync settings`: ausgewählte Einstellungen ändern, ohne config.toml von Hand zu bearbeiten.

Jede Änderung wird zuerst in einer Kopie der Datei gebaut und mit config.parse_config (alle Prüfungen) validiert;
erst danach wird die echte Datei atomar ersetzt. Bei einem Fehler bleibt config.toml unverändert.
Die Uhrzeiten der Läufe (config.RUN_TIMES) werden bewusst nicht angeboten.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, fields
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from . import config as config_module
from . import rules
from .config import Config, WEBUNTIS_CATEGORIES, parse_config, set_config_value
from .errors import ConfigError

INT, BOOL, CHOICE, MULTI, LIST = "int", "bool", "choice", "multi", "list"


@dataclass(frozen=True)
class Setting:
    key: str  # 'abschnitt.name', muss in config.py existieren
    label: str
    help: str
    kind: str
    lo: int = 0
    hi: int = 0
    choices: tuple[str, ...] = ()

    @property
    def section(self) -> str:
        return self.key.split(".")[0]

    @property
    def name(self) -> str:
        return self.key.split(".")[1]


SETTINGS: tuple[Setting, ...] = (
    Setting("rules.reminder_minutes", "Erinnerung vor Terminen (Minuten)",
            "Popup-Erinnerung der Kalendertermine vor Beginn, z. B. 1440 = 24 Stunden.", INT, 1, 40320),
    Setting("rules.assignment_lead_hours", "Eduvidual-Task-Datum: Frist minus x Stunden",
            "Das Task-Datum ist das Datum von (Frist minus x Stunden). 0 = stattdessen assignment_lead_days (1 Tag).", INT, 0, 168),
    Setting("rules.assignment_calendar", "Eduvidual-Aufgaben zusätzlich im Kalender",
            "ja = neben der Google-Task auch ein Kalendertermin; nein = nur Task.", BOOL),
    Setting("rules.homework_lead_days", "Hausaufgaben-Task: Tage vor der Abgabe",
            "WebUntis-Hausaufgabe: Task-Datum = Abgabe minus x Kalendertage.", INT, 0, 60),
    Setting("rules.exam_lead_days", "Lern-Task für Prüfungen: Tage vorher",
            "Die Lern-Task einer Prüfung bekommt das Datum Prüfung minus x Kalendertage.", INT, 0, 60),
    Setting("rules.study_every_days", "Lernplan: alle x Tage (0 = aus)",
            "Zusätzliche Lern-Task alle x Tage vor einer Prüfung. 0 schaltet den Lernplan aus.", INT, 0, 30),
    Setting("rules.study_start_days", "Lernplan: Beginn y Tage vor der Prüfung",
            "Der Lernplan beginnt so viele Tage vor der Prüfung (wirkt nur, wenn 'alle x Tage' größer als 0 ist).", INT, 1, 60),
    Setting("rules.window_days", "Prüffenster (Tage)",
            "Neue Termine werden für heute bis heute + x Tage angelegt.", INT, 1, 120),
    Setting("report.notify", "Windows-Meldung bei Problemen",
            "ja = zusätzlich zum Bericht eine Windows-Benachrichtigung.", BOOL),
    Setting("report.open_on", "Bericht im Browser öffnen",
            "problems = nur bei Problemen, warnings = auch bei Warnungen, never = nie (Bericht wird trotzdem gespeichert).",
            CHOICE, choices=("problems", "warnings", "never")),
    Setting("report.keep_days", "Berichte aufbewahren (Tage)", "Ältere Berichte werden gelöscht.", INT, 1, 365),
    Setting("report.log_keep_days", "Logdateien aufbewahren (Tage)", "Ältere Logdateien werden gelöscht.", INT, 1, 365),
    Setting("webuntis.categories", "WebUntis-Kategorien",
            "Kommagetrennt, erlaubt: homework (Hausaufgaben), exams (Prüfungen), cancellations (Entfälle). Mindestens eine.",
            MULTI, choices=WEBUNTIS_CATEGORIES),
    Setting("eduvidual.courses", "Eduvidual-Kurse",
            "Kommagetrennt Kurs-IDs oder exakte Kursnamen; leer (in der Auswahl: '-') = alle aktuellen Kurse.", LIST),
)
BY_KEY = {s.key: s for s in SETTINGS}


def current_config_value(cfg: Config, st: Setting) -> Any:
    return getattr(getattr(cfg, st.section), st.name)


def default_value(st: Setting) -> Any:
    section_cls = type(getattr(Config(), st.section))
    return getattr(section_cls(), st.name)


def format_value(st: Setting, value: Any) -> str:
    if st.kind == BOOL:
        return "ja" if value else "nein"
    if st.kind in (MULTI, LIST):
        return ", ".join(value) if value else ("(alle Kurse)" if st.kind == LIST else "(keine)")
    return str(value)


def allowed_text(st: Setting) -> str:
    if st.kind == INT:
        return f"ganze Zahl von {st.lo} bis {st.hi}"
    if st.kind == BOOL:
        return "ja oder nein"
    if st.kind == CHOICE:
        return "eines von: " + ", ".join(st.choices)
    if st.kind == MULTI:
        return "Liste aus: " + ", ".join(st.choices)
    return "Liste, durch Kommas getrennt (leer = alle)"


def parse_value(st: Setting, text: str) -> Any:
    """Text -> Wert; ConfigError (deutsch, mit Bereich) bei ungültiger Eingabe."""
    text = text.strip()
    bad = ConfigError(f"Ungültiger Wert für {st.key}: '{text}'.", hint=f"Erlaubt: {allowed_text(st)}.")
    if st.kind == INT:
        try:
            n = int(text)
        except ValueError:
            raise bad from None
        if not st.lo <= n <= st.hi:
            raise bad
        return n
    if st.kind == BOOL:
        low = text.lower()
        if low in ("ja", "j", "true", "1", "an", "ein", "yes", "y"):
            return True
        if low in ("nein", "n", "false", "0", "aus", "no"):
            return False
        raise bad
    if st.kind == CHOICE:
        if text not in st.choices:
            raise bad
        return text
    items = [p.strip() for p in text.split(",") if p.strip() and p.strip() != "-"]
    if st.kind == MULTI:
        if not items or any(i not in st.choices for i in items) or len(set(items)) != len(items):
            raise bad
    return items


def study_preview(every: int, start: int, exam: date, exam_lead_days: int = 7) -> str:
    """Beispiel für den Lernplan, z. B. 'Prüfung am 30.10.: Lern-Tasks am 16.10., 20.10., ...'."""
    head = f"Prüfung am {exam:%d.%m.}"
    if every <= 0:
        return f"{head}: kein Lernplan (nur die normale Lern-Task)."
    cfg = config_module.RulesConfig(study_every_days=every, study_start_days=start, exam_lead_days=exam_lead_days)
    days = [exam - timedelta(days=d) for d in rules.study_offsets(cfg)]
    if not days:
        return f"{head}: keine zusätzliche Lern-Task (nur die normale Lern-Task)."
    shown = ", ".join(f"{d:%d.%m.}" for d in days[:5]) + (", ..." if len(days) > 5 else "")
    return f"{head}: Lern-Tasks am {shown} ({len(days)} zusätzliche)."


def validated_write(path: Path, changes: dict[str, Any]) -> Config:
    """Alle Änderungen in einer Kopie bauen, per parse_config prüfen, dann die echte Datei atomar ersetzen."""
    if not path.is_file():
        raise ConfigError("Keine Konfiguration vorhanden.", hint="Bitte zuerst schulsync setup ausführen.")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "config.toml"
        copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        for key, value in changes.items():
            st = BY_KEY[key]
            set_config_value(copy, st.section, st.name, list(value) if isinstance(value, tuple) else value)
        candidate = copy.read_text(encoding="utf-8")
    cfg = parse_config(candidate)  # wirft ConfigError; die echte Datei wurde noch nicht angefasst
    tmp_file = path.with_suffix(path.suffix + ".tmp")
    tmp_file.write_text(candidate, encoding="utf-8")
    os.replace(tmp_file, path)
    return cfg


def _show_all(say: Callable[[str], None], cfg: Config, *, details: bool) -> None:
    for i, st in enumerate(SETTINGS, 1):
        cur, dflt = format_value(st, current_config_value(cfg, st)), format_value(st, default_value(st))
        say(f"{i:2}. {st.label} [{st.key}]: {cur}" + (f"  (Standard: {dflt})" if details else ""))
        if details:
            say(f"      {st.help}  Erlaubt: {allowed_text(st)}.")


def _after(say: Callable[[str], None], cfg: Config, keys: set[str], today: date) -> None:
    if keys & {"rules.study_every_days", "rules.study_start_days"}:
        say("Beispiel: " + study_preview(cfg.rules.study_every_days, cfg.rules.study_start_days, today + timedelta(days=30), cfg.rules.exam_lead_days))
    say("Gilt ab dem nächsten Lauf (bereits angelegte Einträge werden bei Änderungen nur teilweise angepasst).")


def run_settings(
    path: Path,
    *,
    say: Callable[[str], None],
    ask: Callable[[str], str],
    today: date,
    do_list: bool = False,
    sets: list[str] | None = None,
    resets: list[str] | None = None,
) -> int:
    if not path.is_file():
        say("Keine Konfiguration gefunden. Bitte zuerst schulsync setup ausführen.")
        return 1
    cfg = config_module.load_config(path)
    if do_list:
        _show_all(say, cfg, details=True)
        return 0
    if sets or resets:
        changes: dict[str, Any] = {}
        try:
            for item in sets or []:
                key, sep, text = item.partition("=")
                key = key.strip()
                if not sep or key not in BY_KEY:
                    raise ConfigError(f"Unbekannte Einstellung oder Format falsch: '{item}'.",
                                      hint="Aufruf: --set KEY=WERT. Verfügbar: " + ", ".join(BY_KEY) + ".")
                changes[key] = parse_value(BY_KEY[key], text)
            for key in resets or []:
                if key not in BY_KEY:
                    raise ConfigError(f"Unbekannte Einstellung: '{key}'.", hint="Verfügbar: " + ", ".join(BY_KEY) + ".")
                changes[key] = default_value(BY_KEY[key])
            new = validated_write(path, changes)
        except ConfigError as exc:
            say(f"Nicht geändert. {exc}" + (f" {exc.hint}" if getattr(exc, "hint", None) else ""))
            return 2
        for key in changes:
            say(f"{key} = {format_value(BY_KEY[key], current_config_value(new, BY_KEY[key]))}")
        _after(say, new, set(changes), today)
        return 0
    changed: set[str] = set()
    while True:
        say("")
        say("Einstellungen (Nummer wählen, q = fertig):")
        _show_all(say, cfg, details=False)
        pick = ask("Nummer: ").strip().lower()
        if pick in ("q", "quit", "ende", ""):
            break
        if not pick.isdigit() or not 1 <= int(pick) <= len(SETTINGS):
            say(f"Bitte eine Nummer von 1 bis {len(SETTINGS)} oder q eingeben.")
            continue
        st = SETTINGS[int(pick) - 1]
        say(f"{st.label} [{st.key}]")
        say(f"  {st.help}")
        say(f"  Erlaubt: {allowed_text(st)}. Standard: {format_value(st, default_value(st))}.")
        text = ask(f"Neuer Wert (aktuell: {format_value(st, current_config_value(cfg, st))}, leer = behalten): ")
        if not text.strip():
            continue
        try:
            cfg = validated_write(path, {st.key: parse_value(st, text)})
        except ConfigError as exc:
            say(f"Nicht geändert. {exc}" + (f" {exc.hint}" if getattr(exc, "hint", None) else ""))
            continue
        changed.add(st.key)
        say(f"Gespeichert: {st.key} = {format_value(st, current_config_value(cfg, st))}")
    if changed:
        _after(say, cfg, changed, today)
    else:
        say("Nichts geändert.")
    return 0


def all_keys_exist() -> list[str]:
    """Schlüssel der Tabelle, die es in config.py nicht (mehr) gibt (für den Wächter-Test)."""
    missing = []
    for st in SETTINGS:
        cls = type(getattr(Config(), st.section, None))
        if st.section not in {f.name for f in fields(Config)} or st.name not in {f.name for f in fields(cls)}:
            missing.append(st.key)
    return missing
