# SchulSync – Design Phase 2 (Engine, Tageslauf, Befehlszeile)

Ergänzt `docs/DESIGN.md` (dort stehen Regeln, Entscheidungstabellen, Zustandsmodell, Schreibprotokoll). Die Blattmodule
(rules, state, google_*, adapters, report, infra, autostart) existieren bereits; lies deren REALE öffentliche API im Code
(nicht aus dem Gedächtnis). Bei Widerspruch zwischen Doku und Code gewinnt der getestete Code, melde die Abweichung.

## runner.py – Tageslauf (öffentliche API)

```python
@dataclass
class Deps:
    paths: Paths
    config: Config
    adapter_factories: Mapping[Source, Callable[[], SourceAdapter]]   # lazy; dürfen SchulSyncError werfen (z. B. Passwort fehlt)
    gateway_factory: Callable[[], GoogleGateway]                       # lazy; darf GoogleAuthError/ConfigError werfen
    clock: Callable[[], datetime] = now_vienna
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    open_report: Callable[[Path], bool] = report.open_report
    network_ready: Callable[[], bool] = default_network_ready          # DNS+TCP 443 zu den konfigurierten Hosts + Google
    state: StateStore | None = None                                    # Tests injizieren; sonst StateStore(paths.db)

@dataclass
class RunOutcome:
    exit_code: int            # 0 Erfolg/nichts zu tun/still beendet, 1 Teilfehler/Fehler, 2 Konfigurations-/Aufruffehler
    report: RunReport | None  # None bei stillem Beenden/Sperre
    report_path: Path | None
    console_text: str         # kurze deutsche Zusammenfassung für die Konsole ("" wenn still)
    silent: bool              # True: nichts ausgeben (Tagesgate/Sperre im AUTO-Modus)

def run(deps: Deps, mode: RunMode, *, force: bool = False) -> RunOutcome
def restore(deps: Deps, uids: Sequence[str] | None) -> RunOutcome       # None = alle bewusst gelöschten Einträge
```
`run` implementiert DESIGN §7 vollständig (Sperre, Tagesgate, Netzwarten, Wiederholungen mit `deps.sleep`, Quellen unabhängig,
Auth-Sperre, Zeitlimit via `deps.monotonic`, Aufnahme, Google-Schreibphase, Bericht, Browser-Öffnung mit Tab-Flut-Schutz, Vorschau
auf Kopie + DryRun, Aufbewahrung/Pruning, RunReport). Alle Zeiten/Schlafen/Browser/Netzprüfung über `Deps` => deterministisch testbar.

## sync.py – Engine (Aufnahme + Schreibphase)

Reine Zustandslogik gegen `StateStore`, `GoogleGateway` und `rules`. Muss DESIGN §4.3 (Aufnahme) und §6 (Schreibprotokoll) 1:1 umsetzen,
inkl. Absichten (`intents`), Abgleich verlorener Antworten, Nutzer-gelöscht/erledigt-Erkennung, Zielprüfung ohne Ausweichen,
Realitätsabgleich vor dem Schreiben, Beschreibungs-Merge (`textblocks.merge_description`) nach frischem Lesen, Zähler je Quelle/Ziel,
`Change`-Liste, `Notice`s. Die Engine kennt weder Konsole noch Browser noch Uhr (bekommt `now` übergeben).

## factories.py – Produktionsverdrahtung

`build_deps(paths, config, secrets) -> Deps`: echte Adapter (Passwörter aus dem SecretStore, Geheimnisse bei `redact` registrieren),
echtes Gateway (`google_auth.load_credentials` + `RealGoogleGateway`), `RequestsHttp` mit den Zeitlimits aus `RetryConfig`.

## cli.py / __main__.py – Befehle (Deutsch, `python -m schulsync <befehl>` und Konsolenskript `schulsync`)

| Befehl | Zweck |
|---|---|
| `setup` | Einrichtungsassistent (siehe unten). Teil-Schalter: `--google`, `--eduvidual`, `--webuntis` |
| `run [--auto] [--force]` | Tageslauf. `--auto` = Autostart-Modus (still bei nichts zu tun, Bericht im Browser nur bei Problemen). `--force` umgeht die Tagesbegrenzung (manueller Start) |
| `preview` | Vorschau: liest die Quellen, zeigt geplante Google-Änderungen, schreibt NICHTS und verbucht keinen Erfolg |
| `check` | Verbindungs-/Datenzugriffsprüfung: Konfiguration, Geheimnisse vorhanden, Google (Token, Kalender „Schule“ ≠ Hauptkalender, Liste), je Quelle `adapter.check()` mit Kategorien. Entsperrt eine zuvor wegen Anmeldefehler gesperrte Quelle, wenn deren Login jetzt klappt. Keine Schreibzugriffe |
| `status` | Lokaler Überblick ohne Netz: letzter Lauf, je Quelle letzter Erfolg/Auth-Sperre, ausstehende Schreibvorgänge, bewusst gelöschte Einträge, Autostart-Zustand |
| `restore [UID ...]` | Bewusst gelöschte Google-Einträge ausdrücklich wiederherstellen (ohne UID: alle) |
| `report` | Neuesten Bericht im Browser öffnen |
| `autostart install\|remove\|status` | Aufgabenplanung (LogonTrigger) bzw. Startup-Verknüpfung |

`check` und `setup` nehmen die Prozesssperre (nicht blockierend; belegt: „SchulSync läuft gerade, bitte kurz warten.“, Exit 1), weil sie das Google-Token
auffrischen bzw. schreiben können. `status`/`report` bleiben sperrenfrei. `check` zeigt zu jedem Fehlschlag Meldung, Fehlercode, Hinweis und technische
Details (die genaue Fehlerzeile für eine Fehlermeldung), alles maskiert. `run` startet einen Watchdog: nach `run_timeout_seconds` + 60 s Bericht
(Code `timeout`) und `os._exit(3)`; `CliEnv.hard_exit`/`watchdog_grace` sind für Tests austauschbar. `restore` nennt je Kennung das Ergebnis
(wiederhergestellt / nichts wiederherzustellen / nicht wiederhergestellt, weil abgesagt oder vergangen).

Allgemein: Ausgaben Deutsch; unter `pythonw.exe` ist `sys.stdout`/`sys.stderr` `None` -> alles Konsolen-I/O muss das abfangen (nie crashen);
Logging immer in die Datei (`logging_setup`), Konsole nur wenn vorhanden. Exit-Codes wie `RunOutcome`. `--version` und `--help` Deutsch.
Jede Konsolenausgabe läuft über `_Session.say` und damit durch `redact`. `--home` wird absolut gemacht (der Autostart startet in einem anderen
Arbeitsverzeichnis); der Autostart hängt `--home` an, wenn der Datenordner nicht `%LOCALAPPDATA%\SchulSync` ist (auch wenn er aus `SCHULSYNC_HOME`
stammt, die der Aufgabenplaner nicht erbt). Ein falsch typisierter Konfigurationswert gilt überall als `ConfigError` (`config.py` lässt ihn als
`ValueError` entweichen; `_Session.load_config` wandelt ihn um). `run --auto` ohne Bericht des Runners (z. B. Sperrdatei nicht anlegbar,
Exit-Code != 0) schreibt und öffnet den minimalen FAILED-Bericht selbst.

### Einrichtungsassistent `setup` (interaktiv, wiederholbar, idempotent)
1. Datenverzeichnis anlegen; `config.toml` aus `config.example.toml` (Repo-Wurzel) kopieren bzw. aus den Standardwerten erzeugen, wenn sie fehlt.
2. Google: Pfad zur heruntergeladenen OAuth-Client-JSON („Desktop-App“) abfragen -> `authorize_interactive` (Browser) -> die neuen Zugangsdaten bleiben im
   ARBEITSSPEICHER -> Konto bestimmen (Kennung des Primärkalenders aus `calendarList` = Adresse des angemeldeten Kontos); weicht sie (ohne Beachtung der
   Groß-/Kleinschreibung) von `google.account` ab: STOPP mit deutscher Erklärung (mehrere Google-Konten? im Browser das richtige wählen oder `[google] account`
   bewusst ändern), nichts wird gespeichert -> Kalender „Schule“ eindeutig finden (nie Hauptkalender; 0/mehrere Treffer = verständlicher Abbruch mit
   Anleitung) -> Aufgabenliste „Schule“ finden oder anlegen -> ERST DANN Client-Datei und Token im SecretStore speichern und die IDs per
   `set_config_value` in die Konfiguration schreiben. Jeder Abbruch vorher lässt die bisherige Anmeldung, die Client-Datei und die IDs unverändert (die
   Anmeldung im Browser muss dann nach der Korrektur wiederholt werden). Klare Erklärung zum 7-Tage-Ablauf im Testing-Status (kurz; Details in der README).
3. Eduvidual: Benutzername (Konfiguration), Passwort (`getpass`, SecretStore); optional sofort `adapter.check()`.
4. WebUntis: dasselbe. Erfolgreiche Anmeldung hebt eine Auth-Sperre auf.
5. Angebot: Autostart installieren.
Nie Passwörter anzeigen oder loggen; Wiederholung des Assistenten überschreibt nur, was der Nutzer neu eingibt (leere Eingabe = behalten).

## Tests (Phase 2)
- `tests/support/samples.py`, `tests/support/fake_adapter.py`, `tests/support/fake_google.py`, `tests/support/fake_http.py` sind die gemeinsamen Bausteine.
- Engine-/Runner-Tests prüfen jede Zeile der Testmatrix (DESIGN §11) deterministisch (Uhr, Schlafen, Browser, Netzprüfung injiziert).
- Unabhängige Akzeptanztests (`tests/test_acceptance_*.py`) werden VON JEMAND ANDEREM aus ANFORDERUNGEN §10 ohne Kenntnis der Engine-Interna geschrieben
  (nur `runner.run/restore`, `Deps`, die Fakes, `StateStore` lesend) – so fallen Fehlinterpretationen auf.
