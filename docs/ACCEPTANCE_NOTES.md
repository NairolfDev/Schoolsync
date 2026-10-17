# SchulSync – Abnahme-Notizen (Integration)

Stand: Gesamtlauf `.venv\Scripts\python.exe -m pytest -p no:cacheprovider` = **2015 Tests, davon 2014 grün und 1 rot**. Der eine rote Test verlangt eine Änderung der
Vertragsdatei `pyproject.toml` (fehlende Abhängigkeiten `httplib2` und `oauthlib`, siehe Abschnitt 6.3). Die Zahlen und Abschnitte 1-5 beschreiben den Stand der Abnahme
(1611 Tests) und der Härtung (1783 Tests); **Abschnitt 6 beschreibt die Nachprüfung der Anforderungen und ersetzt dort, wo er es sagt (A4, A8), die älteren Aussagen.**
Aufbau je Eintrag: *Anforderung* (Wortlaut/Abschnitt aus `docs/ANFORDERUNGEN.md`), *Entscheidung*, *Begründung*.

## 1. Ergebnis der Integration

- Der erste Gesamtlauf war grün (1572 Tests). Die beiden Engine-Fehler, die die unabhängigen Abnahmetests zuerst aufgedeckt
  hatten (Zeitlimit nicht hart, `console_text` nicht maskiert), waren bereits behoben. **Kein vorhandener Test wurde
  geändert, abgeschwächt oder gelöscht.**
- Neu: 39 Tests. `tests/test_integration_process.py` (21): der echte Einstiegspunkt in einem Kindprozess, auch unter echtem
  `pythonw.exe` (ohne stdout/stderr/stdin). `tests/test_integration_stack.py` (6): die echten Adapter Eduvidual und WebUntis
  (synthetische HTTP-Antworten) über Fabriken, Runner, Engine, Zustand und Bericht bis zu FakeGoogle, dazu `schulsync check`
  mit den echten Adaptern. Rest: 12 Regressionstests zu den Funden unten (`test_sync.py`, `test_runner.py`).
- Handlauf mit temporärem `SCHULSYNC_HOME` (nichts im echten Benutzerprofil): `--help`, `--version`, `status`, `check`,
  `preview`, `restore`, `report` ohne Konfiguration geben deutsche Meldungen und die Exit-Codes 0/2/1. `run --auto` ohne
  Konfiguration schreibt einen FAILED-Bericht und versucht ihn zu öffnen. Das lief nur als Test mit ersetztem Browser-Öffner.

## 2. Funde im Quelltext (behoben, jeweils mit Regressionstest)

| # | Anforderung | Befund | Entscheidung | Test |
|---|---|---|---|---|
| F1 | 8: „Wiederholte identische Fehler am selben Tag sollen keine Tab-Flut erzeugen“ | Eine abgelehnte Anmeldung, die der Adapter als `FetchResult.login_error` meldet, brachte vier fehlgeschlagene Kategorien in den Bericht. Der spätere (gesperrte) Start desselben Tages hat keine Kategorien, also einen anderen Fingerabdruck und öffnete einen zweiten Tab. | Bei abgelehnter Anmeldung werden die Kategorien nicht aufgeführt: Die Anmeldung ist das eine Problem. Erster und späterer Start teilen den Fingerabdruck. Kategorien bleiben bei strukturellen Login-Fehlern (`schema`/`incomplete`/`config`) erhalten. | `test_runner::test_a_rejected_login_is_one_problem...`, `test_integration_stack::test_rejected_login...` |
| F2 | 6: „Erledigte Tasks erhalten“ | Beim Absagen wurde die Task nach der **einmal pro Lauf** gelesenen Liste gelöscht. Hakt der Nutzer sie in der Zwischenzeit ab, wäre eine erledigte Task gelöscht worden. | Vor dem Löschen frisch lesen (`get_task`): erledigt oder ausgeblendet → DONE, gelöscht → USER_DELETED, nichts löschen. | `test_sync` (2 Tests, `StaleTaskList`) |
| F3 | 6: „Bewusst gelöschte eigene Google-Einträge nicht … kommentarlos neu erstellen“ | Nach Verlust der lokalen Datenbank antwortet Google auf das Anlegen mit 409. Las sich der gelöschte Termin dann als 404/410 (None), wurde das als dauerhafter Fehler gemeldet (jeder Lauf FAILED, falscher Hinweis). | 409 plus „nicht lesbar“ gilt wie ein Tombstone als vom Nutzer gelöscht (USER_DELETED, Hinweis, kein Fehler, nicht neu angelegt). Nur ein gelöschter Termin ist trotz 409 unlesbar. | `test_sync::test_lost_database_and_an_event_the_user_deleted_stays_deleted` |
| F4 | 1: „Zeitlimits und wenige begrenzte Wiederholungen“ | Die Netzwartezeit zählte nur die Pausen, nicht die Dauer der Prüfungen. Prüfungen mit eigenen Zeitüberschreitungen verlängerten das „höchstens 120 s“ auf ein Mehrfaches (im Test 600 s). | Die Wartezeit wird nach der verstrichenen Zeit (`deps.monotonic`) begrenzt. | `test_runner::test_the_network_wait_is_bounded_in_real_time...` |
| F5 | 7, letzter Absatz: „Falls eine alte Schulautomation noch läuft, auf mögliche Überschneidungen hinweisen“ | Fehlte vollständig. | Der Einrichtungsassistent weist darauf hin: fremde Einträge bleiben unangetastet, eine noch laufende alte Automation kann doppelte Einträge erzeugen und sollte abgeschaltet werden. Keine automatische Erkennung (das Gateway kann fremde Einträge nicht auflisten, unbekannte Automationen sind nicht erkennbar). | `test_cli::test_setup_happy_path` |
| F6 | 9: „Liefere vollständigen Quellcode … Tests“ | `.gitignore` enthält `token*.json` (für Google-Tokens). Das verschluckte vier Testdaten (`tests/fixtures/eduvidual/token_*.json`); in einem frischen Klon fehlten sie und Tests scheiterten. | Ausnahme `!tests/fixtures/**/*.json`; `tests/_out/` (erzeugte Beispielberichte) wird ignoriert. `git ls-files --others --exclude-standard` entspricht jetzt den Dateien auf der Platte. | geprüft per Git |
| F7 | 9: „Vorschau … ohne Google-Schreibzugriffe“; 6: „bewusst gelöschte“ Einträge nur bei Nachweis | Die Vorschau beantwortet ein nicht lesbares Google (abgelaufene Anmeldung, offline) mit „nichts vorhanden“. Die Engine las das als „vom Nutzer gelöscht“: jeder bekannte, geänderte Eintrag wurde als „in Google gelöscht“ gemeldet, und die Änderung fehlte in der Vorschau. Realistisch, weil die Anmeldung im Testing-Status nach 7 Tagen abläuft. | Ein nicht lesbares Google ist unbekannt, kein Nachweis einer Löschung: Die Änderung wird geplant, es wird nichts behauptet (die Warnung „Vorschau unvollständig“ bleibt). Betrifft nur die Vorschau (das echte Gateway hat kein `reads_unavailable`). | `test_sync` (4 Tests), `test_runner` (2 Tests) |

Blattmodule: nichts geändert, kein echter Fehler gefunden. Vertragsdateien: nichts geändert.

## 3. Auslegungen (wo der Anforderungstext mehrdeutig ist; im Zweifel das Datenschonendere)

| # | Anforderung | Entscheidung | Begründung |
|---|---|---|---|
| A1 | 1: manueller Start, „der die Tagesbegrenzung ausdrücklich umgehen kann“ | `schulsync run` (manuell) wird nie vom Tagesgate gebremst; `--force` wirkt zusätzlich bei `run --auto`. Eine wegen Anmeldefehler gesperrte Quelle überspringt AUTO immer (auch mit `--force`); MANUAL und PREVIEW versuchen sie stets. | DESIGN 7 („nur AUTO und nicht --force“); ein bewusster Start soll immer prüfen können, ein automatischer nie endlos wiederholen. |
| A2 | 1: „Ein Teilfehler darf nicht als vollständiger Erfolg gelten“ | Eine Quelle gilt am Tag nur als erledigt bei vollständigem Abruf **und** keinem offenen Schreibvorgang. Wurde heute vollständig abgerufen, fehlen aber Google-Schreibvorgänge, werden nur diese nachgeholt (kein erneuter Abruf). | Tageserfolg je Quelle (DESIGN 5/7); spart Anmeldungen bei den Schulplattformen. |
| A3 | DESIGN 7: Gesamtstatus FAILED/PARTIAL | Quelle FAILED = nichts erreicht, PARTIAL = etwas erreicht, aber nicht alles. Gesamt SUCCESS, wenn alle aktiven Quellen SUCCESS/ALREADY_DONE; FAILED, wenn keine erfolgreich und mindestens eine FAILED; sonst PARTIAL. | Der Wortlaut lässt „zurückgestellte Schreibvorgänge“ offen; so ist der Bericht eher alarmierend als beschönigend. |
| A4 | 4: „Ist ein Ziel nicht erreichbar, die betroffenen Schreibvorgänge zurückstellen“ | **Seit der Nachprüfung (6.1):** Nur die Schreibvorgänge des betroffenen Ziels werden zurückgestellt: Fehlt die Aufgabenliste, werden die Termine trotzdem geschrieben; fehlt der Kalender, die Aufgaben. Ein Kalender wird nur nach bestätigter Prüfung beschrieben (kein Hauptkalender, Schreibrecht); wurde er nicht geprüft, wartet er. Fällt die gemeinsame Anmeldung aus (`GoogleAuthError`) oder lässt sich nichts über die Ziele sagen (Netz), warten alle Schreibvorgänge. Nie ein Ausweichen. | Wortlaut von §4: „die BETROFFENEN Schreibvorgänge“. (Bis zur Nachprüfung galt hier die konservativere Fassung „alles zurückstellen“.) |
| A5 | 9: „Eine Vorschau darf keine Synchronisation als erfolgreich verbuchen und keine später erforderlichen Schreibvorgänge unterdrücken“ | Vorschau arbeitet auf einer Datenbankkopie mit DryRun-Gateway, setzt **keine** Anmeldesperre, verbucht keinen Tag/Lauf/Browserstatus, räumt nichts auf. Sie schreibt `vorschau.html`; die CLI (nicht der Runner) öffnet sie. | Eine Vorschau mit Anmeldesperre würde spätere automatische Läufe unterdrücken. |
| A6 | 7: „Fehlen stabile Kennungen, … konservative Zuordnung, Unsicherheiten melden“ | Ohne stabile Kennung nur bei genau einem unbeanspruchten Kandidaten (gleiche Quelle/Art/Kurs/Titel, fehlt im vollständigen Abruf) zuordnen, sonst als neu anlegen; immer Hinweis `identity_uncertain`. Bei unvollständiger Kategorie wird die Zuordnung auf den nächsten vollständigen Abruf verschoben. Stabile Kennungen nie über Titel verbinden. | „Ähnliche Titel allein genügen nicht“; ein Duplikat ist behebbar, ein falsch überschriebener Eintrag nicht. |
| A7 | 6/10: Fehlende Einträge, Fensterende, leere Liste, Verbindungsfehler sind keine Absage | Absage nur bei `status = CANCELLED` oder `withdrawn` mit Beleg; Reaktivierung nur bei vollständig gelesener Kategorie. Eine verschwundene Entfall-Meldung ist keine Rücknahme. | Wortlaut von §6. |
| A8 | 7 „auch … gelöschte Task-Zustände bei der Zuordnung berücksichtigen“ gegen 6 „eine wegen Absage entfernte offene Task darf passend neu entstehen; eine zuvor erledigte oder bewusst gelöschte Task bleibt respektiert“ | **Seit der Nachprüfung (6.1):** Gelöschte Tasks werden nie übernommen, aber als Nachweis gelesen (die API liefert sie mit `showDeleted`). Ohne lokalen Zustand und ohne lebende Task mit der Projektkennung gilt: eine gelöschte Task, die erledigt oder ausgeblendet ist, ist **erledigt** (SchulSync löscht nur offene Tasks); eine sonst gelöschte Task **ohne Entfernungsvermerk** hat der Nutzer gelöscht (USER_DELETED, Hinweis, nicht neu angelegt, `restore` möglich); eine gelöschte Task **mit Entfernungsvermerk** hat SchulSync bei einer Absage entfernt, dann darf sie nach einer Reaktivierung neu entstehen. Den Vermerk (eine Zeile `schulsync-removed: <uid>` in den Notizen) schreibt SchulSync unmittelbar vor dem Löschen. **Grenze:** Gelingt dieser Vermerk nicht (Fehler beim Patchen, Notizen am Limit), gilt später im Zweifel „vom Nutzer gelöscht“ (das Datenschonendere); nach Datenbankverlust entsteht eine so entfernte Task dann auch nach Reaktivierung nicht neu. | Ohne Spur sind „von uns bei Absage entfernt“ und „vom Nutzer gelöscht“ nach einem Datenbankverlust nicht unterscheidbar; mit der Spur sind es beide Vorgaben des Auftrags. Mit echtem Google nicht beobachtet (ob `showDeleted` die Notizen gelöschter Tasks liefert, folgt der Annahme in `fake_google.py`). |
| A9 | 2/5: „Bereits abgelaufene Abgabefristen und vollständig vergangene Ereignisse nicht neu anlegen“ | Frist genau „jetzt“ ist noch nicht abgelaufen. Ereignis mit Ende genau „jetzt“ ist vorbei. Mit nur bekanntem Start gilt: Start vor jetzt = vorbei. Ein laufendes Ereignis wird angelegt. | DESIGN 3 (`is_fully_past`). |
| A10 | 8: Tab-Flut-Schutz | Browser pro Tag und Fingerabdruck einmal; ein gescheitertes Öffnen gilt nicht als geöffnet (nächster Start versucht erneut, Pfad im Log). Bericht-Datei wird immer aktualisiert. Ist die Datenbank selbst unlesbar, wird der Bericht ohne Tab-Flut-Schutz geschrieben und geöffnet. | „Falls das Öffnen scheitert, den Bericht erhalten und seinen Speicherort protokollieren.“ |
| A11 | 8: „Anzahl neuer, aktualisierter, unveränderter, abgesagter und übersprungener Einträge“ | Übernahme eines vorhandenen eigenen Eintrags zählt als „neu“; Wiederherstellung ebenso. Zurückgestellte, fehlgeschlagene und unklare Schreibvorgänge zählen als „fehlgeschlagen“ (es gibt keinen eigenen Zähler). Überspringen wird mit den Gründen von `report.py` gezählt, nicht als Änderung. | `models.Counts` kennt keinen Zähler „zurückgestellt“. |
| A12 | DESIGN 7: Exit-Codes | 0 Erfolg/nichts zu tun/still; 1 Teilfehler/Fehler (auch manueller Start bei laufender Instanz); 2 Konfigurations-/Aufruffehler (fehlende oder kaputte `config.toml`, unlesbarer Speicher, unbekannte UID bei `restore`). AUTO bei laufender Instanz: still, Exit 0. | Für den Fall „Quelle nicht baubar“ lassen beide Codes zu. Es gilt 1, weil die Fehlerbeschreibung im Bericht steht. |
| A13 | 8: Fehler nie stumm | `run --auto` schreibt auch bei Fehlern außerhalb des Runners (fehlende/kaputte Konfiguration, unlesbarer Geheimnisspeicher, Absturz) einen minimalen FAILED-Bericht und öffnet ihn einmal pro Tag und Fehlerart (Markerdatei, unabhängig von der Datenbank). | Die Datenbank kann genau das kaputte Teil sein. |
| A14 | 1: „Berücksichtige noch fehlendes Internet“ | Netzwartezeit endet, sobald **ein** Host (Google oder eine aktivierte Schule) per DNS und TCP 443 antwortet; danach entscheiden die Wiederholungen. Nach Ablauf wird trotzdem versucht. | Ein ausgefallener Schulserver soll nicht jede Anmeldung um 2 Minuten verzögern. |
| A15 | 9: „Prüfen der Verbindung“ | `check` und `status` schreiben nichts und legen keine Datenbank an. `check` entsperrt eine Quelle nur, wenn ihr Schritt „Anmeldung“ gelingt. `status` fragt die Aufgabenplanung nur lesend ab. | „Keine Schreibzugriffe“ (DESIGN_PHASE2). |
| A16 | 5/6: Tasks nur planen, nie erfinden | Tasks einer verschobenen Frist: `due` wird neu berechnet, außer die neue Frist ist bereits vollständig vergangen. Überfällige offene Tasks bleiben unverändert. Beim Anlegen/Adoptieren werden `task_applied_hash`, `task_base_deadline` und `task_due` zusammen gespeichert. | DESIGN 4.2. |

## 4. Nicht real geprüft / Grenzen (ehrlich)

Alles oben ist gegen **FakeGoogle, DryRunGateway, synthetische Schul-Antworten und eingespielte Uhr/Browser/Netzprüfung** geprüft.
Echt ausgeführt wurden: der Python-Einstiegspunkt (auch unter `pythonw.exe`), Dateisperre, SQLite, Protokolldateien,
Berichtsdateien, Argumentverarbeitung. Nicht geprüft:

1. **Google:** echte OAuth-Anmeldung, Token-Erneuerung, 7-Tage-Ablauf im Testing-Status, Kalender-/Tasks-API. Die Randfälle (Tombstone vs. 404/410 bei
   gelöschten Terminen, `deleted`/`hidden` bei Tasks, 409 auf reservierte IDs) folgen den Annahmen in `fake_google.py`, nicht einer Beobachtung.
2. **Eduvidual und WebUntis:** welche Zugangsart wirklich funktioniert, echte Antwortformen, Rechte der Schülerrolle. `schulsync check` am Ziel-PC ist die
   eigentliche Datenzugriffsprüfung.
3. **Windows:** Aufgabenplanung (`autostart install`) mit dem Anmeldetrigger und `--home`, das Verhalten nach echter Anmeldung (Netz kommt verzögert),
   echtes Öffnen des Standardbrowsers aus der Aufgabenplanung, DPAPI-Speicher nach Benutzerwechsel. `scripts\install.ps1` wurde nicht ausgeführt
   (es würde eine Umgebung anlegen und Pakete laden).
4. **Parallelität:** Die Prozesssperre ist mit einem echten zweiten Prozess getestet (`test_lock.py`), der Runner unter belegter Sperre im selben Interpreter.
   Ein echter Doppelstart (Aufgabenplanung plus manueller Start gleichzeitig) wurde nicht beobachtet.
5. **Bekannte Lücken:** Zeitlimit ist kooperativ (ein laufender HTTP-Aufruf wird nur von den HTTP-Zeitlimits begrenzt). Einzelne Schreibvorgänge werden im Lauf
   nicht wiederholt (nächster Start holt sie nach). Eine adoptierte Task ohne Fälligkeitsdatum bekommt keines. Die Engine löscht beim Übernehmen
   eines id-losen Eintrags eine Zeile über `store._conn` (Wunsch: `StateStore.delete_item`). (`README.md` gibt es seit der Nachprüfung, Abschnitt 6.)

### Stand nach der Härtung (H1-H9)

Von den bekannten Lücken oben sind geschlossen: das Zeitlimit ist jetzt auch hart (Watchdog in `cli run`, `os._exit(3)`), `store._conn` wird von der Engine
nicht mehr berührt (öffentliche `StateStore`-Methoden), und die Datei `report_opened.json` ist durch Schema v2 (`report_opened` mit mehreren Fingerabdrücken je
Tag) ersetzt. Offen bleibt: einzelne Schreibvorgänge werden im Lauf nicht wiederholt; eine adoptierte Task ohne Fälligkeitsdatum bekommt keines. (`README.md`: seit Abschnitt 6 vorhanden.)
Die Auslegung A10 (Datenbank unlesbar) gilt so: `cli run` prüft die Datenbank vorab und meldet den Fehler einmal je Tag und Fehlerart über die Markerdatei; ein
direkt aufgerufener Runner öffnet den Bericht ohne Gedächtnis. Alle neuen Verhaltensweisen sind gegen Fakes und echte lokale Prozesse getestet (Watchdog mit
echtem `os._exit(3)`, Geheimnisspeicher mit Threads und Prozessen auf der echten DPAPI), NICHT gegen echte Google-, Eduvidual- oder WebUntis-Konten.

## 5. Was der Nutzer am echten System prüfen muss

1. `.\scripts\install.ps1`, danach `schulsync setup` (OAuth-Client-Datei, Google-Anmeldung, Kalender „Schule“ und Aufgabenliste „Schule“, beide Schulkonten).
2. `schulsync check`: zeigt je Quelle, welche Kategorien wirklich lesbar sind (Eduvidual: Webservice oder Web-Weg, WebUntis: Hausaufgaben, Prüfungen, Entfälle).
   Eine Kategorie „UNVOLLSTÄNDIG/FEHLGESCHLAGEN“ ist ein echter Befund, kein Testfehler.
3. `schulsync preview`, dann `schulsync run`: Termine und Aufgaben in Google prüfen (Titel, 24-h-Erinnerung, Aufgabendatum, Bereich „Persönliche Notizen“).
4. In Google einen Termin und eine Aufgabe löschen, eine Aufgabe abhaken, Notizen ergänzen; nochmals `schulsync run --force`: nichts wird neu angelegt oder
   überschrieben. `schulsync status` zeigt die bewusst gelöschten Einträge; `schulsync restore` stellt sie wieder her.
5. `schulsync autostart install`, abmelden/anmelden: nach etwa einer Minute läuft ein stiller Lauf (Aufgabenplanung „Letztes Ergebnis“ 0). Bei Problemen
   öffnet sich der Bericht im Browser, bei Erfolg nicht.
6. Internet trennen, `schulsync run --auto --force`: Bericht öffnet sich einmal, keine Tab-Flut beim zweiten Start.
7. Ein falsches Passwort eintragen (`setup --eduvidual`): ein Versuch, Quelle gesperrt, Hinweis im Bericht; nach Korrektur hebt `check` oder ein manueller Lauf
   die Sperre auf.
8. Nach 7 Tagen im Testing-Status die Google-Neuanmeldung (`schulsync setup --google`) erleben oder die App in Google Cloud auf „In Produktion“ stellen.

## 6. Nachprüfung der Anforderungen (Audit)

Unabhängige Prüfer haben jede Anforderung mit Quelltext und Tests verglichen und für die Lücken im Quelltext fehlschlagende Tests geschrieben
(`tests/test_audit_1.py` bis `test_audit_5.py`). Der Quelltext wurde angepasst, **ohne** diese Tests zu ändern. Die Regressionstests der Behebungen stehen in
`tests/test_audit_fixes.py`, `tests/test_audit_adapters.py` und `tests/test_config.py`. Wie überall in diesem Dokument gilt: alles gegen Fakes und synthetische Daten,
nichts gegen echte Konten.

### 6.1 Behobene Lücken

| Ref | Lücke | Behebung | Tests |
|---|---|---|---|
| 1.4e, 4.10, 4.12a, 5.2, 9.10a, 10.13 | Keine README; Autostart-Alternative, Google-Cloud-Einrichtung, 7-Tage-Ablauf und „keine rückwirkende Erinnerung“ waren nur in Code-Texten erwähnt. | `README.md` (deutsch): Installation, Google-Cloud-Projekt, Calendar API, Tasks API, Desktop-OAuth-Client, Testing und die 7 Tage gegen „In Produktion“ (ohne Versprechen), alle Befehle, Aufgabenplanung mit Anmeldetrigger, Startup-Verknüpfung (`shell:startup`), Erinnerungen und Kalender-App-Einstellungen, Daten und Entfernen, ehrliche Grenzen. | `test_audit_1/3/5`, `test_project_files` (README nennt jeden Befehl und jede Option, Verweise existieren, kein Unbegrenztversprechen) |
| 1.7b | `retry.run_timeout_seconds` war nicht geprüft (0 ließ jede Quelle bei jeder Anmeldung mit `run_timeout` scheitern; über etwa 3500 s hätte die Aufgabe den Lauf still beendet). | `config._validate` (die Datei gilt als stabil, die Änderung ist unvermeidbar): Zeitlimit größer 0 und höchstens 3300 s (die Aufgabe endet nach PT1H, der Watchdog feuert bei Limit + 60 s), `network_wait_seconds` nicht negativ. | `test_config`, `test_audit_1` |
| 2.9c, 3.4c | Die Eduvidual-Weboberfläche las nur bis zum spätesten bekannten Datum + 2 Monate: eine weit verschobene Frist blieb als „nicht gefunden“ stehen. | Solange offene Einträge verfolgt werden, liest der Web-Weg den ganzen dokumentierten Bereich (`WEB_MAX_MONTHS`, 24 Monate), sonst nur das Fenster; jenseits des Bereichs der Hinweis „begrenzt“. Kosten: bis zu 24 lesende Anfragen je Lauf auf diesem Ausweichweg. | `test_audit_2`, `test_audit_adapters`, `test_integration_stack` (Web-Weg durch Runner, Engine, Zustand und Bericht) |
| 3.2b | Der WebUntis-Kalenderexport war nicht bewertet. | `docs/datenzugriff_webuntis.md` bewertet ihn: was er liefern kann, was nicht, warum er nicht verwendet wird. | `test_audit_2` |
| 3.2c | Web-Weg: Aufgaben ohne Frist sind unsichtbar und wurden nur von `check` genannt; Fenster außerhalb aller Schuljahre gaben ein stilles OK/0. | Hinweis `no_date_unseen` bei jedem Lauf des Web-Wegs und im Kategoriedetail; WebUntis nennt „kein Zeitraum innerhalb der Schuljahre“ als Hinweis. | `test_audit_2`, `test_audit_fixes`, `test_audit_adapters` |
| 3.3c | WebUntis hatte keine Sperrliste für Methoden. | `READ_ONLY_RPC_METHODS` (`authenticate`, `getSchoolyears`, `getTimetable`); jede andere Methode wirft `ForbiddenMethodError`, bevor etwas gesendet wird. Dazu ein Test, der den Adapter-Quelltext nach Methodennamen durchsucht. | `test_audit_2`, `test_audit_fixes` |
| 3.4f | Benennt WebUntis `dueDate`/`examDate` für alle Einträge um, blieb die Kategorie OK mit lauter `no_date`-Warnungen. | `expect_field_somewhere`: hat bei mindestens zwei Einträgen keiner das Feld (oder die dokumentierte Alternative), ist das ein `SchemaError` mit JSON-Pfad (ohne Datenwerte). Ein einzelner undatierter Eintrag bleibt der Hinweis `no_date`. | `test_audit_2`, `test_audit_fixes` |
| 4.9 | Eine fehlende Aufgabenliste stellte auch alle Termine zurück und umgekehrt. | `google_targets.TargetsUnavailableError` (Unterklasse des Vertragsfehlers `TargetUnavailableError`, keine Vertragsdatei geändert) nennt je Ziel Ergebnis und Fehler; echtes Gateway und FakeGoogle werfen ihn (DryRun reicht ihn durch); die Engine stellt nur die Schreibvorgänge des betroffenen Ziels zurück (A4 neu gefasst). | `test_audit_3`, `test_audit_fixes`, `test_acceptance_04_targets` |
| 6.2, 7.10 | Nach Datenbankverlust bekam eine erledigte und danach gelöschte Task eine neue offene Kopie; eine vom Nutzer gelöschte Task wurde neu angelegt. | `_settle_by_tombstones` und der Entfernungsvermerk, siehe A8. | `test_audit_4`, `test_audit_fixes`, `test_acceptance_07_sync` |
| 6.7 | `location=None` beim Patch löschte einen Ort, den der Nutzer selbst eintrug. | Der Ort wird nur geleert oder ersetzt, solange er der von SchulSync kopierte Raum ist (der verwaltete Block nennt genau „Raum: …“); ein anderer Ort ist die Eintragung des Nutzers und bleibt. | `test_audit_4`, `test_audit_fixes` |
| 6.9 | Nach `restore` einer Task konnte nach einem unklaren Anlegen ein alter Grabstein die Wiederherstellung als neue Nutzerlöschung lesen. | `restore` setzt die Task auf REMOVED (Grund „restored“), nicht auf NONE; für REMOVED beweist ein alter Grabstein nichts. | `test_audit_4`, `test_audit_fixes` |
| 7.6a | `_dedupe` verwarf auch bekannte Einträge, die einem anderen bekannten Eintrag gleich wurden; ihre Verschiebungen und Absagen gingen verloren. | Bekannte `uid` werden nie verworfen (der Auftrag verbietet das ANLEGEN gleicher Inhalte); neue Dubletten bleiben mit Hinweis übersprungen. | `test_audit_4`, `test_audit_fixes` |
| 7.8 | Termine wurden nur über die abgeleitete ID abgeglichen, nie über die private Projekteigenschaft. | Vor dem ersten Anlegen sucht die Engine über `find_events_by_uid` einen lebenden eigenen Termin und übernimmt ihn (auch mit ID aus anderer `uid` oder Generation; die Wiederherstellungsgeneration wird fortgesetzt); mehrere Funde: Hinweis `duplicate_event_property`; Lesefehler: nichts anlegen, später erneut. Kosten: eine lesende Anfrage je neu anzulegendem Termin. | `test_audit_4`, `test_audit_fixes`, `test_integration_kill` (echter Kindprozess) |
| 8.6 | Der Bericht des Wiederholungsstarts am selben Tag nannte „Bereits erfolgreich übernommen: Keine.“ für eine Quelle, die heute schon übernommen war. | Für nicht erneut abgerufene Quellen führt der Bericht auf, was Google bereits hält (Hinweise `already_taken_over`: Kalender / Aufgabe / von dir erledigt). | `test_audit_5`, `test_audit_fixes` |
| 8.16 | `cli._auto_failure_report` räumte alte Berichte nie auf. | Aufräumen mit der konfigurierten Aufbewahrung (der heutige Bericht bleibt). | `test_audit_5`, `test_audit_fixes` |
| 8.14 | Die Maskierung kannte die URL-Parameter `sig`, `ticket`, `sid`, `session`, `pw`, `credential(s)`, `auth`, `signature` nicht. | Diese Namen gelten als ganze Namen in `redact` und beim Kopieren von Links in Kalendertexte (`rules._safe_url`); `assignment=5` oder `design=1` bleiben unberührt. Grenze: Maskierung bleibt eine Namens- und Formheuristik. | `test_redact`, `test_rules` |
| 9.3, 9.5b, 9.6, 9.7 | Nur von Hand geprüft bzw. ohne Testmodul. | `tests/test_project_files.py` (Geheimnis-Scan der ausgelieferten Dateien, `tzdata` deklariert, keine KI- oder Browser-Abhängigkeit), `tests/test_config.py` (https-Pflicht, Bereiche, unbekannte Schlüssel, Beispieldatei gleich den Standardwerten und ohne Zugangsdaten). | dort |

### 6.2 Geänderte oder verschärfte bestehende Tests

Gelöscht oder abgeschwächt wurde kein Test. Geändert wurde, wo ein Test die ältere Auslegung festhielt oder nichts bewies:

- `test_acceptance_07_sync::test_after_the_database_was_lost_a_task_the_user_deleted_never_leads_to_two_open_tasks` verlangte nur „höchstens eine offene Task“ und galt
  auch, wenn die Löschung des Nutzers kommentarlos rückgängig gemacht wurde. Er prüft jetzt die bewusste Verhaltensänderung A8: keine neue Task, Hinweis, Zustand USER_DELETED.
- `test_sync::TestReconciliation::test_orphan_task_that_is_done_or_deleted` (zweite Hälfte): Das Verhalten (nach eigener Absage-Entfernung und Datenbankverlust entsteht eine neue Task) gilt
  weiter, nun begründet durch den Entfernungsvermerk; nur der Kommentar wurde angepasst.
- `test_acceptance_04_targets` (zwei Tests) beweisen jetzt „nur die betroffenen Schreibvorgänge“ statt „nichts Falsches“.
- `test_report::test_render_sample_for_eyeballing` ersetzt: schrieb in den Quellbaum und prüfte nur eine Dateigröße; der Nachfolger prüft Überschriften und die UTF-8-Hin-und-Rück-Probe in einem Temp-Ordner.
- Einheitliche Anrede: alle Texte für den Nutzer sind jetzt in der Du-Form (Konsole, Bericht, Anmeldeseite, `scripts\install.ps1`, `docs/datenzugriff_webuntis.md`), wie die Adaptertexte, die
  Kalendertexte und die README. Zwei Tests, die die alte Sie-Fassung festhielten (`test_cli`, `test_harden_runner`), prüfen jetzt dieselben Sätze in der Du-Form.
- Verschärft (Nachweis nur noch mit Gegenprobe): Link-Test (`test_acceptance_05`), Netzwartezeit (`test_acceptance_01`), Integrationsstack (`test_integration_stack`: WebUntis jetzt genau, plus Web-Weg),
  Geheimnisse in Logs (`test_acceptance_08`: ohne die Datei-Protokollierung der CLI waren die Log-Dateien dort LEER, die Prüfung konnte nicht fehlschlagen), Google-Berechtigungen
  (`test_google_auth`: Abgleich jedes API-Aufrufs im Quelltext mit den Discovery-Dokumenten), Übersetzung von Google-Fehlern (`test_google_client`: viele Geheimnisformen in jedem lesbaren Feld, Detail nur Status oder Klassenname),
  `task_patch_body` (genau), Aufgaben-Schreibvorgänge der Engine (`test_acceptance_06`: nie ein Status, nie ein fremdes Feld; Prüfung auch für Prüfungs-Tasks), Zustand in der Datenbank (`test_acceptance_07`),
  fremde Einträge mit Projektmarke (`test_acceptance_07`), die Registrierung der Google-Geheimnisse (`test_factories`), Autostart-Aufgabe (`test_autostart`: die relevanten Einstellungen und der Parser der Aufgabenplanung selbst, ohne etwas zu registrieren).
- Neu: `tests/test_google_auth_loopback.py` lässt den ECHTEN Loopback-Ablauf der Google-Anmeldung der Bibliothek laufen (lokaler Weiterleitungsserver, ein "Browser", der der Weiterleitung folgt,
  und ein lokaler Token-Endpunkt): geprüft werden die Adresse, die der Browser öffnen würde (Berechtigungen, `offline`, `consent`, PKCE), die Token-Anfrage und die deutschen Fehler bei Abbruch,
  abgewählter Berechtigung und fehlendem Refresh-Token. Google selbst bleibt dabei nachgebaut: ob Google den Client akzeptiert, zeigt erst `schulsync setup --google` mit dem echten Konto.
- Neu: `tests/test_integration_kill.py` beendet einen echten Kindprozess mit `os._exit` unmittelbar nachdem Google ein Anlegen angenommen hat, und prüft den nächsten Start (kein Duplikat, Sperre und Datenbank frei).

### 6.3 Offen

- **Vertragsdatei `pyproject.toml`:** `google_auth.py` und `google_client.py` importieren `httplib2`, `google_auth.py` auch `oauthlib` direkt; beides ist nicht deklariert (kommt nur über andere Pakete).
  Die Datei durfte nicht geändert werden. Gewünscht: `"httplib2>=0.22",` und `"oauthlib>=3.2",` unter `dependencies` (geprüft wurde nur mit den installierten Versionen 0.32.0 und 4.0.0).
  Bis dahin bleibt `tests/test_audit_5.py::test_every_directly_imported_third_party_package_is_declared_in_pyproject` rot; das ist der einzige rote Test.
- **Vertragswunsch:** Die Information „welches Ziel fehlt“ gehört langfristig in `google_types.TargetCheck` bzw. `errors.TargetUnavailableError` (heute: `google_targets.py`).
- **Grenzen, die bleiben:** Ein einzelner Eintrag ohne Datum ist nicht von einem umbenannten Feld zu unterscheiden (Hinweis statt Fehler); bei Eduvidual führt ein umbenanntes `intro` nur zu einem Hinweis je Eintrag.
  Ein Vermerk, der nicht geschrieben werden konnte, macht eine später entfernte Task nach Datenbankverlust zur „vom Nutzer gelöscht“ (A8). Das Maskieren von Geheimnissen bleibt eine Namens- und Formheuristik.
  Dass „In Produktion“ den 7-Tage-Ablauf beendet, ist Googles Verhalten und nicht geprüft.
