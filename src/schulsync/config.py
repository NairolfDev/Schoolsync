"""Konfiguration (TOML) und Datenverzeichnis.

Die Konfiguration enthält KEINE Geheimnisse (Passwörter/Tokens liegen im geschützten
Geheimnisspeicher, siehe secrets_store.py). Unbekannte Schlüssel werden als Fehler gemeldet,
damit Tippfehler nicht stillschweigend ignoriert werden.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

ENV_HOME = "SCHULSYNC_HOME"
# Standardzeiten für die zwei täglichen Läufe.
RUN_TIMES: tuple[str, ...] = ("06:00", "18:00")
WEBUNTIS_CATEGORIES = ("homework", "exams", "cancellations")
# The scheduled task is ended by Windows after one hour (autostart.build_task_xml, ExecutionTimeLimit PT1H). The run limit
# plus the watchdog grace (cli.WATCHDOG_GRACE_SECONDS, 60 s) must stay clearly below that.
MAX_RUN_TIMEOUT_SECONDS = 3300


@dataclass(frozen=True)
class Paths:
    """Alle lokalen Speicherorte. Standard: %LOCALAPPDATA%\\SchulSync (oder $SCHULSYNC_HOME)."""

    home: Path

    @classmethod
    def default(cls) -> "Paths":
        env = os.environ.get(ENV_HOME)
        if env:
            return cls(Path(env))
        if sys.platform.startswith("linux"):
            return cls(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "schulsync")
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return cls(Path(base) / "SchulSync")

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def db(self) -> Path:
        return self.home / "state.sqlite3"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def reports(self) -> Path:
        return self.home / "reports"

    @property
    def lock(self) -> Path:
        return self.home / "schulsync.lock"

    @property
    def secrets(self) -> Path:
        return self.home / "secrets.dat"

    def ensure(self) -> None:
        for p in (self.home, self.logs, self.reports):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class EduvidualConfig:
    enabled: bool = True
    base_url: str = "https://www.eduvidual.at"
    username: str = ""
    method: str = "auto"  # auto | webservice | web
    courses: tuple[str, ...] = ()  # leer = alle aktuellen Kurse; sonst Kurs-IDs oder exakte Kursnamen
    modules: tuple[str, ...] = ("assign",)  # Moodle-Aktivitätstypen, die als Abgaben gelten


@dataclass(frozen=True)
class WebUntisConfig:
    enabled: bool = True
    base_url: str = "https://htbla-grieskirchen.webuntis.com"
    school: str = "htbla-grieskirchen"
    username: str = ""
    categories: tuple[str, ...] = WEBUNTIS_CATEGORIES


@dataclass(frozen=True)
class GoogleConfig:
    account: str = ""  # optional: erwartetes Google-Konto; leer = keine Prüfung beim Setup
    calendar_name: str = "Schule"
    calendar_id: str = ""  # wird bei der Einrichtung gesetzt
    tasklist_title: str = "Schule"
    tasklist_id: str = ""  # wird bei der Einrichtung gesetzt


@dataclass(frozen=True)
class RulesConfig:
    window_days: int = 31
    assignment_lead_days: int = 1  # nur wenn assignment_lead_hours = 0
    assignment_lead_hours: int = 24  # Eduvidual-Aufgabe: Frist minus 24 Stunden; 0 = assignment_lead_days
    assignment_calendar: bool = False  # Eduvidual-Aufgaben nur als Task; true = zusätzlich Kalendertermin
    homework_lead_days: int = 3
    exam_lead_days: int = 7
    study_every_days: int = 0  # Lernplan: alle x Tage vor einer Prüfung eine zusätzliche Lern-Task; 0 = aus
    study_start_days: int = 14  # Lernplan beginnt so viele Tage vor der Prüfung
    study_mode: str = "interval"  # interval | days | both
    study_days: tuple[int, ...] = ()  # einzelne Tage relativ zur Prüfung: -1 = Vortag, 0 = Prüfungstag
    reminder_minutes: int = 1440


@dataclass(frozen=True)
class ScheduleConfig:
    run_times: tuple[str, ...] = RUN_TIMES


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 4  # Versuche je Quelle/Google-Vorprüfung (inkl. des ersten)
    delays_seconds: tuple[float, ...] = (15.0, 60.0, 180.0)  # Wartezeit vor Versuch 2, 3, 4 ...
    network_wait_seconds: float = 120.0  # max. Wartezeit auf Internet direkt nach der Anmeldung
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    failures_before_report: int = 3  # automatische Läufe nacheinander mit Fehler, bevor Bericht/Meldung erscheinen
    run_timeout_seconds: float = 1500.0  # harte Obergrenze für einen Lauf


@dataclass(frozen=True)
class ReportConfig:
    open_on: str = "problems"  # problems | warnings | never
    notify: bool = True  # Windows-Benachrichtigung zusätzlich zum Bericht
    keep_days: int = 30  # Berichte
    log_keep_days: int = 30  # Logdateien


@dataclass(frozen=True)
class Config:
    eduvidual: EduvidualConfig = field(default_factory=EduvidualConfig)
    webuntis: WebUntisConfig = field(default_factory=WebUntisConfig)
    google: GoogleConfig = field(default_factory=GoogleConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    report: ReportConfig = field(default_factory=ReportConfig)

    @property
    def enabled_sources(self) -> list[str]:
        out = []
        if self.eduvidual.enabled:
            out.append("eduvidual")
        if self.webuntis.enabled:
            out.append("webuntis")
        return out


_SECTIONS: dict[str, type] = {
    "eduvidual": EduvidualConfig,
    "webuntis": WebUntisConfig,
    "google": GoogleConfig,
    "rules": RulesConfig,
    "schedule": ScheduleConfig,
    "retry": RetryConfig,
    "report": ReportConfig,
}


def _build(section: str, cls: type, raw: Mapping[str, Any]) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(
            f"Unbekannte Einstellung in [{section}]: {', '.join(unknown)}",
            hint=f"Erlaubt sind: {', '.join(known)}. Tippfehler prüfen.",
        )
    kwargs: dict[str, Any] = {}
    default = cls()
    for name, value in raw.items():
        want = type(getattr(default, name))
        if want is tuple:
            if not isinstance(value, list):
                raise ConfigError(f"[{section}] {name} muss eine Liste sein, z. B. [\"a\", \"b\"].")
            kwargs[name] = tuple(float(v) if name == "delays_seconds" else v for v in value)
        elif want is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"[{section}] {name} muss true oder false sein.")
            kwargs[name] = value
        elif want is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"[{section}] {name} muss eine ganze Zahl sein.")
            kwargs[name] = value
        elif want is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"[{section}] {name} muss eine Zahl sein.")
            kwargs[name] = float(value)
        else:
            if not isinstance(value, str):
                raise ConfigError(f"[{section}] {name} muss ein Text in Anführungszeichen sein.")
            kwargs[name] = value
    return cls(**kwargs)


def _validate(cfg: Config) -> None:
    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise ConfigError(msg)

    need(cfg.eduvidual.method in ("auto", "webservice", "web"), "[eduvidual] method: auto, webservice oder web.")
    need(cfg.eduvidual.base_url.startswith("https://"), "[eduvidual] base_url muss mit https:// beginnen.")
    need(cfg.webuntis.base_url.startswith("https://"), "[webuntis] base_url muss mit https:// beginnen.")
    times = cfg.schedule.run_times
    need(len(times) == 2 and all(isinstance(t, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t) for t in times)
         and len(set(times)) == 2, "[schedule] run_times braucht zwei verschiedene Uhrzeiten im Format HH:MM.")
    bad = [c for c in cfg.webuntis.categories if c not in WEBUNTIS_CATEGORIES]
    need(not bad, f"[webuntis] categories: unbekannt {bad}; erlaubt {list(WEBUNTIS_CATEGORIES)}.")
    r = cfg.rules
    need(1 <= r.window_days <= 120, "[rules] window_days muss zwischen 1 und 120 liegen.")
    for name in ("assignment_lead_days", "homework_lead_days", "exam_lead_days"):
        need(0 <= getattr(r, name) <= 60, f"[rules] {name} muss zwischen 0 und 60 liegen.")
    need(1 <= r.reminder_minutes <= 40320, "[rules] reminder_minutes muss zwischen 1 und 40320 liegen.")
    t = cfg.retry
    need(1 <= t.max_attempts <= 10, "[retry] max_attempts muss zwischen 1 und 10 liegen.")
    need(1 <= t.failures_before_report <= 24, "[retry] failures_before_report muss zwischen 1 und 24 liegen.")
    need(0 <= r.study_every_days <= 30 and 1 <= r.study_start_days <= 60, "[rules] study_every_days 0-30, study_start_days 1-60.")
    need(r.study_mode in ("interval", "days", "both"), "[rules] study_mode muss interval, days oder both sein.")
    need(all(isinstance(d, int) and not isinstance(d, bool) and -60 <= d <= 0 for d in r.study_days)
         and len(set(r.study_days)) == len(r.study_days), "[rules] study_days: verschiedene ganze Zahlen von -60 bis 0.")
    need(all(d >= 0 for d in t.delays_seconds), "[retry] delays_seconds dürfen nicht negativ sein.")
    need(t.connect_timeout > 0 and t.read_timeout > 0, "[retry] Zeitlimits müssen positiv sein.")
    need(t.network_wait_seconds >= 0, "[retry] network_wait_seconds darf nicht negativ sein.")
    # 0 or less would make every source fail with "run_timeout" at every logon; more than the scheduled task allows would
    # be ended by the Task Scheduler without a report before the watchdog (limit + 60 s) can fire.
    need(
        0 < t.run_timeout_seconds <= MAX_RUN_TIMEOUT_SECONDS,
        f"[retry] run_timeout_seconds muss größer als 0 und höchstens {MAX_RUN_TIMEOUT_SECONDS} Sekunden sein "
        "(die Aufgabenplanung beendet einen Lauf nach 1 Stunde).",
    )
    need(cfg.report.open_on in ("problems", "warnings", "never"), "[report] open_on: problems, warnings oder never.")
    need(cfg.report.keep_days >= 1 and cfg.report.log_keep_days >= 1, "[report] Aufbewahrung mindestens 1 Tag.")


def parse_config(text: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml ist kein gültiges TOML: {exc}") from exc
    unknown = sorted(set(raw) - set(_SECTIONS))
    if unknown:
        raise ConfigError(f"Unbekannter Abschnitt in config.toml: {', '.join(unknown)}")
    sections: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] muss ein Abschnitt sein (Zeile mit [{name}] und darunter Einstellungen).")
        try:
            sections[name] = _build(name, cls, section)
        except (ValueError, TypeError) as exc:  # z. B. delays_seconds = ["a"]
            raise ConfigError(
                f"Ungültiger Wert in [{name}].", hint="Typ und Format der Einstellung prüfen.", detail=str(exc)[:200]
            ) from exc
    cfg = Config(**sections)
    _validate(cfg)
    return cfg


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            "Konfigurationsdatei fehlt.", hint="Bitte zuerst 'schulsync setup' ausführen."
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            "Konfigurationsdatei kann nicht gelesen werden.",
            hint="Datei und Zugriffsrechte prüfen; sie muss als UTF-8 gespeichert sein.",
            detail=type(exc).__name__,
        ) from exc
    return parse_config(text)


# --------------------------------------------------------------------------- gezieltes Schreiben


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"Nicht unterstützter TOML-Wert: {value!r}")


def set_config_value(path: Path, section: str, key: str, value: Any) -> None:
    """Setzt `key` in `[section]`, erhält Kommentare und übrige Zeilen. Legt Abschnitt/Zeile an."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    header = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
    key_re = re.compile(rf"^(\s*){re.escape(key)}\s*=")
    new_line = f"{key} = {_toml_value(value)}"

    start = next((i for i, ln in enumerate(lines) if (m := header.match(ln)) and m.group(1) == section), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{section}]", new_line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
        for i in range(start + 1, end):
            if key_re.match(lines[i]):
                lines[i] = new_line
                break
        else:
            insert_at = end
            while insert_at > start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, new_line)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
