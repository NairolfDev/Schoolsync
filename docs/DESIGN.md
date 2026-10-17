# SchulSync – Technisches Design (verbindlich für alle Module)

Quelle der Wahrheit für das *Was*: `docs/ANFORDERUNGEN.md` (Originaltext, Abschnitte 1–10).
Dieses Dokument legt das *Wie* fest. Bei Widerspruch gewinnt ANFORDERUNGEN.md; Abweichungen melden.

## 0. Arbeitsregeln

- Code, Bezeichner, Kommentare, Docstrings: Englisch. Alles, was der Nutzer sieht (Konsole, HTML-Bericht,
  Fehlermeldungen, Hinweise, Kalender-/Task-Texte, README): Deutsch, kurz und verständlich.
- Python >= 3.11 (getestet mit 3.13), `src/`-Layout, Paket `schulsync`. Tests: pytest.
  Testlauf: `.venv\Scripts\python.exe -m pytest tests/<deine_datei>.py` (Repo-Wurzel ist das Arbeitsverzeichnis).
- **Vertragsdateien dürfen NICHT editiert werden** (nur der Orchestrator ändert sie): `models.py`, `errors.py`,
  `timeutil.py`, `http.py`, `google_types.py`, `config.py`, `tests/support/fake_http.py`, `tests/conftest.py`,
  `pyproject.toml`. Brauchst du eine Änderung, beschreibe sie im Abschnitt "Vertragswünsche" deines Ergebnisses.
- Nur die dir zugewiesenen Dateien anlegen/ändern. Andere Agenten arbeiten parallel in anderen Dateien.
  Führe nur DEINE Testdateien aus (nicht die ganze Suite) – andere Module sind evtl. gerade halbfertig.
- Neue Abhängigkeiten nur, wenn zwingend; Standardbibliothek zuerst. Installiert sind: google-api-python-client,
  google-auth(+oauthlib,+httplib2), requests, tzdata, pytest. KEIN Playwright, KEIN bs4/lxml (HTML mit `html.parser`/Regex).
- Keine KI-/LLM-Aufrufe, keine Bildauswertung, keine Zusammenfassungen. Texte werden nur kopiert/normalisiert.
- Geheimnisse (Passwörter, Tokens, Cookies, `sesskey`, `authtoken`, `wstoken`, `Authorization`) nie loggen, nie in
  Fehlermeldungen/Berichten, nie in Testdaten (nur offensichtliche Dummywerte wie `hunter2-dummy`).
- Schulplattformen werden ausschließlich GELESEN (GET bzw. Login-POST). Niemals Abgaben/Häkchen/Inhalte ändern.
- Test-first (Rot-Grün) für die Fachregeln. Tests mit realistischen, anonymisierten Beispieldaten
  (Fantasienamen, keine echten Personen; Fächer/Kursnamen österreichischer HTL sind ok).
- Ehrlichkeit: Was nur gegen synthetische Fixtures getestet ist, wird so benannt (Docstring/README). Keine erfundenen
  Endpunkte/Felder: Jede Schnittstellen-Annahme braucht eine belegte Quelle (offizielle Doku, Quellcode, reale Probe)
  und steht kommentiert im Code bzw. in `docs/DATENZUGRIFF.md`.

## 1. Reale Befunde (am 2026-10-04 ohne Anmeldung geprüft)

Eduvidual (https://www.eduvidual.at, Moodle):
- `POST /lib/ajax/service-nologin.php` mit `[{"index":0,"methodname":"tool_mobile_get_public_config","args":{}}]`
  liefert: `enablewebservices=1`, `enablemobilewebservice=1`, `typeoflogin=2` (Browser-Login für die App),
  `identityproviders=[Bildungsportal (Shibboleth)]`, `showloginform=1`, `tool_mfa_enabled=false`,
  `enableloginrecaptcha=false`, `tool_mobile_minimumversion=5.2.1`, `authloginviaemail=1`.
- `GET /login/token.php?service=moodle_mobile_app` ohne Parameter antwortet JSON `{"error":"Notwendiger Parameter \"username\" fehlt","errorcode":"missingparam"}`
  -> Endpunkt existiert und ist aktiv.
- `GET /webservice/rest/server.php?wsfunction=tool_mobile_get_public_config&moodlewsrestformat=json` -> `invalidtoken`
  (REST-Protokoll aktiv, Token nötig).
- `GET /login/index.php` enthält ein Formular (`username`, `password`, `logintoken`, `anchor`), kein Captcha.
Nicht geprüft (Zugangsdaten nötig): ob `login/token.php` mit Benutzername/Passwort ein Token liefert, welche
Webservice-Funktionen für `moodle_mobile_app` freigeschaltet sind, reale Antwortformen.

WebUntis (https://htbla-grieskirchen.webuntis.com, Version laut Statik 2027.2.4):
- Startseite `GET /WebUntis/?school=htbla-grieskirchen` setzt `JSESSIONID`, `schoolname`, `Tenant-Id` und bettet
  `untis.config` ein (u. a. `mandantName=htbla-grieskirchen`, `mandant=16586`, `csrfHeader=X-CSRF-TOKEN`, `csrfToken`,
  `loginServiceConfig.hideWuLogin=false`, `ssoOffice365=true` (Microsoft-SSO existiert zusätzlich), `ssoSamlProviders=[]`).
- `POST /WebUntis/jsonrpc.do?school=htbla-grieskirchen` (ohne Login) -> `{"error":{"message":"not authenticated","code":-8520}}`
  -> JSON-RPC aktiv.
- `GET /WebUntis/api/homeworks/lessons?...`, `/api/exams?...`, `/api/token/new` ohne Sitzung -> `302 -> /WebUntis/index.do`
  (Endpunkte existieren, Sitzung nötig). `/api/rest/view/v1/app/data` und `/api/rest/view/v1/schoolinfo` ohne Sitzung -> 404
  (Header `X-Untis-School-Url: unknown`): Pfad/Kontext ungeklärt.
- Die Startseitenkonfiguration listet Legacy-Dienste, u. a. `jsonHomeworkService`.
Nicht geprüft (Zugangsdaten nötig): Login-Ablauf (Formular/CSRF), Antwortformen, Rechte der Schülerrolle.

Konsequenz: Beide Plattformen sind per reinem HTTP (requests) erreichbar; Playwright wird NICHT eingebaut. Falls `check`
auf dem Zielsystem zeigt, dass ein Weg real nicht funktioniert, ist ein Browser-Adapter der dokumentierte nächste Schritt.

## 2. Modulkarte und Zuständigkeiten

```
src/schulsync/
  __init__.py  errors.py  models.py  timeutil.py  http.py  config.py  google_types.py   [Vertrag, fertig]
  textblocks.py       Agent A   verwaltete Beschreibungsblöcke (Merge), Textkürzung, deutsche Datumsformate
  rules.py            Agent A   Planer: Regeln für Termin/Task, Fenster, "vollständig vergangen", Vorlauf
  state.py            Agent B   SQLite-Zustand (Items, Schreibabsichten, Tagesstatus, Läufe)
  google_bodies.py    Agent C   reine Funktionen: DesiredEvent/-Task -> API-Bodies, Records lesen
  google_client.py    Agent C   echte Gateway-Implementierung (offizielle Clients), Fehlerübersetzung
  google_auth.py      Agent C   OAuth (einmalig interaktiv), Token laden/erneuern/speichern, Einrichtungshilfen
  dryrun.py           Agent C   DryRunGateway für die Vorschau (liest echt, schreibt nie)
  adapters/__init__.py, adapters/eduvidual.py   Agent D
  adapters/webuntis.py                          Agent E
  report.py           Agent F   HTML-Bericht (escaped, offline), Speichern, Öffnen, Aufbewahrung
  secrets_store.py    Agent G   DPAPI-Geheimnisspeicher (+ Memory-Variante für Tests)
  lock.py             Agent G   Prozesssperre
  redact.py           Agent G   Geheimnismaskierung (Text + logging.Filter)
  logging_setup.py    Agent G   rotierende Tageslogs, Aufbewahrung
  autostart.py        Agent G   Aufgabenplanung (LogonTrigger) + Startup-Verknüpfung als Ausweichlösung
  sync.py, runner.py  Phase 2   Engine (Abgleich, Schreibprotokoll) und Tageslauf (Gate, Wiederholung, Vorschau)
  cli.py, __main__.py Phase 2   Befehle (setup, run, preview, check, status, restore, report, autostart)
tests/support/fake_google.py    Agent C   FakeGoogle (hochwertiger In-Memory-Ersatz mit Fehlerinjektion)
tests/support/fake_http.py      fertig    FakeHttp
```

## 3. Zeitsemantik (Europe/Vienna)

- `today` = `today_vienna(now)`. Alle fachlichen Vergleiche nutzen aware datetimes in `VIENNA` bzw. reine Kalenderdaten.
- Prüffenster neuer Termine: `fetch_window(now, window_days)` = `[heute 00:00, heute+21 Tage 00:00)`, Ende exklusiv.
  Ein Eintrag liegt im Fenster, wenn sein Intervall das Fenster schneidet (`Window.overlaps`); ein `date` steht für den ganzen Tag.
- Vorlauf (Task-Datum) = Fristdatum minus N **Kalendertage** (`minus_calendar_days`), N = 3 (Abgabe/Hausaufgabe), 7 (Prüfung).
  Nie `timedelta(hours=72)` über eine Zeitumstellung. Das Fristdatum ist das Wiener Kalenderdatum der Frist.
- "Vollständig vergangen" (`is_fully_past(item, now)`):
  - ASSIGNMENT/HOMEWORK (Frist): Frist als datetime -> `frist < now`; als date -> `frist < today`.
  - EXAM/CANCELLATION: Ende bekannt -> `end <= now`; sonst datetime-Start -> `start < now` (Fristmarkierung); date -> `date < today`.
- Neue Einträge (kein Zustand vorhanden) werden nur angelegt, wenn: Status aktiv UND nicht vollständig vergangen UND im Fenster.
  Sonst: übersprungen (Grund: `expired` / `outside_window` / `already_cancelled`), nichts gespeichert.
- Kalenderdarstellung:
  - Abgabe/Hausaufgabe mit Uhrzeit: einminütige Markierung `[frist, frist+1min]` (`add_real_minutes`), Beschreibung nennt die echte Frist.
  - Ohne Uhrzeit: ganztägig, `end` = Folgetag (exklusiv).
  - Prüfung/Entfall: tatsächliche Start-/Endzeit; nur Start bekannt -> einminütige Markierung + Hinweis "Endzeit unbekannt".
  - Zeitstempel zu Google immer mit Offset (`isoformat()`) UND `timeZone: "Europe/Vienna"`.
- Task-Fälligkeit: `due` = `YYYY-MM-DDT00:00:00.000Z` des Arbeitsbeginns (Google ignoriert die Uhrzeit; Tasks haben keine Fälligkeitsuhrzeit).

## 4. Regeln je Eintragsart (aus ANFORDERUNGEN §5/§6, verbindlich)

| Kind | Kalender-Titel | Kalender-Zeit | Task-Titel | Task-Datum |
|---|---|---|---|---|
| ASSIGNMENT | `Abgabe: {title}` | Frist (Markierung/ganztägig) | `Bearbeiten: {title}` | Frist-Datum − 3 Tage |
| HOMEWORK | `Abgabe: {title}` | Frist (meist ganztägig) | `Bearbeiten: {title}` | Frist-Datum − 3 Tage |
| EXAM | `{subtype or "Prüfung"}: {title}` | Start/Ende | `Lernen: {title}` | Start-Datum − 7 Tage |
| CANCELLATION | `Entfall: {title}` | Start/Ende der Stunde | – | – |

- Alle Termine: Popup-Erinnerung `reminder_minutes` (1440) vor Beginn, `useDefault=false`. Abgesagt: Titel `Abgesagt: …`, **keine** Erinnerung.
- Verwalteter Beschreibungstext (Termin und Task): Frist/Termin mit Wochentag und Uhrzeit (deutsch), Kurs/Fach, Lehrkraft, Raum,
  Originalbeschreibung, Quelllink (nur http/https), Hinweise (`item.notes`), Quelle. Tasks zusätzlich: Arbeitsbeginn und die
  Projektkennung als eigene Zeile `schulsync-id: <uid>`. Texte unverfälscht (nur Whitespace normalisiert), Längenlimits der APIs
  beachten (Kürzung mit "[… gekürzt]", Titel max. 1024).
- Termine tragen `extendedProperties.private`: `schulsync=1`, `schulsync_uid`, `schulsync_source`, `schulsync_kind`.
- Event-ID deterministisch: `make_event_id(uid, gen)`; `gen` steigt nur bei ausdrücklicher Wiederherstellung.

### 4.1 Entscheidungstabelle Kalendertermin (`rules.plan_item`, `ItemState` -> `EventPlan`)

| event_state | Bedingung | Aktion |
|---|---|---|
| USER_DELETED | – | SKIP (Grund `user_deleted`; nichts schreiben) |
| NONE | `cancelled` | SKIP (`already_cancelled`) |
| NONE | vollständig vergangen | SKIP (`expired`) |
| NONE | sonst | CREATE |
| ACTIVE | `desired.content_hash() == event_applied_hash` | NONE (unverändert) |
| ACTIVE | Hash verschieden, `cancelled` und nicht `applied_cancelled` | UPDATE als CANCEL |
| ACTIVE | Hash verschieden, nicht `cancelled` und `applied_cancelled` | UPDATE als REACTIVATE (gleicher Termin) |
| ACTIVE | Hash verschieden sonst | UPDATE |

### 4.2 Entscheidungstabelle Task (`TaskPlan`), nur für ASSIGNMENT/HOMEWORK/EXAM; CANCELLATION -> immer NONE

`work_start = deadline_date − lead`; `due_new = max(work_start, today)` (einmalige "heute"-Regel bei verspäteter Entdeckung).

| task_state | Bedingung | Aktion |
|---|---|---|
| DONE | – | NONE (nie anfassen, nie neu anlegen, nie zurücksetzen) |
| USER_DELETED | – | NONE (respektieren, auch bei Reaktivierung) |
| REMOVED | `cancelled` | NONE |
| REMOVED | nicht `cancelled` (Reaktivierung) und nicht vollständig vergangen | CREATE mit `due_new` |
| REMOVED | sonst | NONE |
| NONE | `cancelled` oder vollständig vergangen | NONE (kein neuer Lern-/Bearbeitungstask) |
| NONE | sonst | CREATE mit `due_new` |
| OPEN | `cancelled` | REMOVE (nur diese offene Task löschen -> Zustand REMOVED, Grund `cancelled`) |
| OPEN | Frist ≠ `task_base_deadline` (echte Friständerung) | UPDATE: Titel/Notiz neu; `due` = `due_new`, außer die neue Frist ist vollständig vergangen (dann `due` unverändert); `update_due=True` |
| OPEN | Frist gleich, Soll-Hash ≠ `task_applied_hash` | UPDATE nur Titel/Notiz (`update_due=False`) |
| OPEN | sonst | NONE (überfällige Tasks bleiben unverändert – NICHT täglich auf heute schieben) |

`task_base_deadline` = ISO der Frist (date oder datetime), `task_due` = zuletzt gesetztes Datum (beides im ItemState).
Erledigte Tasks werden NIE verändert (auch Titel/Notiz nicht). Der Erledigt-Status wird nur aus Google gelesen.

### 4.3 Neuer Eintrag aus der Quelle (Aufnahme, `sync.ingest`)

1. Duplikat innerhalb desselben Abrufs (gleicher `dedupe_key`, andere `uid`) -> der zweite NEUE Eintrag wird übersprungen, Hinweis `duplicate_in_source`.
   Bekannte `uid` gewinnen und werden NIE verworfen (verboten ist das ANLEGEN gleicher Inhalte; ein Eintrag mit eigenem Termin/eigener Task folgt weiter
   seiner Quelle, sonst bliebe eine Verschiebung oder Absage eines bekannten Zwillings ohne Hinweis veraltet).
2. Unbekannte `uid`: Zuordnung per Inhalt NUR für `id_stable=False`-Einträge (konservativ: genau ein Kandidat gleicher Quelle/Art/Kurs/
   normalisierter Titel, dessen alte `uid` im aktuellen vollständigen Abruf fehlt -> übernehmen, `id_uncertain=True`, Hinweis `identity_uncertain`;
   mehrdeutig -> als neu behandeln + Hinweis). Stabile IDs: nie über Titel/Datum zusammenführen.
3. Neuer Eintrag besteht Fenster-/Vergangenheits-/Absagen-Prüfung (§3) -> Zustand anlegen (`dirty=True`), sonst überspringen.
4. Bekannter Eintrag: `item_hash` geändert oder `cancelled`-Flag geändert -> Zustand aktualisieren, `dirty=True`. Außerhalb des Fensters
   weiter aktualisieren (Verschiebungen/Absagen!).
5. `fetch.withdrawn[uid]` (ausdrücklich bestätigt, mit Beleg) -> `cancelled=True`, Hinweis `withdrawn_confirmed`.
   NIE aus Abwesenheit, leerer Liste, Fensterende, Abgabestatus oder Verbindungsfehlern auf Absage schließen.
6. Reaktivierung: bekannter Eintrag kommt wieder mit `status=ACTIVE` obwohl `cancelled=True` gespeichert war -> `cancelled=False`, `dirty`.
   (Nur wenn der Abruf der Kategorie vollständig war.)

## 5. Zustand (SQLite, `state.py`)

`PRAGMA user_version` für Migrationen; `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`. Zeiten als ISO-8601 mit Offset.
Tabellen (Namen verbindlich, Spalten mindestens):
- `items`: `uid PK`, `source`, `kind`, `source_id`, `item_json`, `item_hash`, `cancelled`, `first_seen_at`, `last_seen_at`,
  `event_id`, `event_gen`, `event_state`, `event_applied_hash`, `applied_cancelled`, `dirty`,
  `task_id`, `task_state`, `task_applied_hash`, `task_base_deadline`, `task_due`, `task_removed_reason`, `id_uncertain`, `updated_at`.
- `intents` (Schreibabsichten, nur für nicht idempotente Vorgänge): `id PK`, `uid`, `target` (calendar|tasks), `action`
  (create|delete), `key` (Event-ID bzw. Task-Marke/-ID), `payload_hash`, `created_at`, `state` (open|resolved|aborted), `note`.
  Eine Absicht wird VOR dem API-Aufruf committet und im selben Commit wie das Ergebnis aufgelöst.
- `source_days`: `day`, `source`, `fetched_at`, `fetch_complete`, `writes_done_at` – PK (`day`,`source`).
  Tageserfolg einer Quelle = `fetch_complete` UND `writes_done_at` gesetzt (alle Google-Schreibvorgänge dieser Quelle erledigt).
- `source_status`: `source PK`, `last_attempt_at`, `last_error_code`, `auth_blocked_at` (Anmeldung dauerhaft abgelehnt -> automatische
  Läufe überspringen die Quelle, bis `check`/`setup`/manueller Lauf erfolgreich war).
- `runs`: `id`, `started_at`, `finished_at`, `day`, `mode`, `status`, `fingerprint`, `report_path`.
- `report_opened`: `seq PK`, `day`, `fingerprint`, `opened_at`, `UNIQUE (day, fingerprint)` (Tab-Flut-Schutz; seit Schema v2 mehrere
  Fingerabdrücke je Tag, `report_opened_fingerprints(day)`; `report_opened_fingerprint(day)` = zuletzt gemerkter). Schema v1 hatte `day` als
  Primärschlüssel; die Migration v1 -> v2 übernimmt die vorhandenen Zeilen.
- `meta`: Schlüssel/Wert. Schlüssel: `last_complete_fetch.<quelle>`, `target.calendar_id`, `target.tasklist_id` (die Ziel-Kennungen, zu denen der
  gespeicherte Google-Zustand gehört, siehe §6 „Zielwechsel“).
`StateStore.copy_to_memory()` liefert eine isolierte Kopie (Vorschau). Schreiben in die Kopie ändert das Original nie.

## 6. Google-Schreibprotokoll (`sync.py`)

Reihenfolge je Lauf: Zielprüfung (`check_targets`, nur wenn Schreibvorgänge anstehen) -> Intents wiederherstellen -> `list_tasks()` einmal
-> je `dirty`-Eintrag: Realität prüfen (Termin `get_event`, Task aus der Liste), planen (`plan_item`), schreiben, Zustand committen.
Fehler eines Eintrags stoppen die anderen nicht. Je Schreibvorgang genau ein `Change` im Bericht.

- Realitätsabgleich vor jedem Schreiben: Termin 404/410/`status=cancelled` -> `USER_DELETED` (Hinweis `user_deleted_event`), nicht neu anlegen.
  Task `completed` oder `hidden` -> `DONE`; `deleted` oder unauffindbar -> `USER_DELETED` (außer eine offene Lösch-Absicht von uns existiert -> `REMOVED`).
  Reihenfolge: `completed` zuerst (erledigt bleibt erledigt), dann `deleted`.
- Termin anlegen: ZUERST (nur ohne bekannte `event_id`) `find_events_by_uid` über die private Projekteigenschaft: ein LEBENDER eigener Termin ist unser Termin
  (auch wenn seine ID aus einer anderen `uid` oder einer späteren Generation stammt, z. B. nach Identitätszuordnung oder `restore` und anschließendem
  Datenbankverlust) -> übernehmen (Aktion ADOPT, `event_gen` setzt fort), mehrere -> der erste, Hinweis `duplicate_event_property`. Lesefehler -> nichts anlegen, der
  Eintrag bleibt `dirty`. Danach: Absicht `create/calendar` -> `insert_event(body mit deterministischer id)`. `ConflictError` -> `get_event(id)` -> übernehmen
  (Aktion ADOPT), danach ggf. normal aktualisieren. `OutcomeUnknownError` -> Absicht bleibt offen, nächster Versuch gleicht per ID ab.
- Task anlegen: ZUERST in der geladenen Liste nach Marke `schulsync-id: <uid>` suchen (lebende, auch erledigte/ausgeblendete) -> gefunden: übernehmen
  (kein Duplikat!). Gelöschte Tasks werden nie übernommen, aber ausgewertet (siehe „Gelöschte Tasks nach Datenbankverlust“ unten). Sonst Absicht `create/tasks` -> `insert_task`. Bei `OutcomeUnknownError` (Zeitüberschreitung/5xx): Liste frisch laden;
  Marke gefunden -> übernehmen; Liste ok und nicht gefunden -> höchstens begrenzt erneut versuchen; Liste nicht ladbar -> Absicht offen lassen,
  Change `UNCERTAIN`, KEIN zweiter Eintrag auf Verdacht. Mehrfachfunde -> erster nicht gelöschter, Hinweis `duplicate_task_marker`.
- Updates (`patch_*`) sind idempotent und brauchen keine Absicht. Beschreibung: aktuellen Google-Stand lesen (`get_event`/`get_task`), nur den
  verwalteten Block ersetzen (`textblocks.merge_description`), Text außerhalb unverändert lassen (Termin: `keep_personal=True`, die 8000 Zeichen
  sind nur das eigene Budget des verwalteten Blocks; eigener Text wird nie gekürzt). Erledigte Tasks nie patchen. Ort (`location`) des Termins: kommt aus der Quelle ein Raum, wird er kopiert; nennt die Quelle keinen Raum mehr, wird der Ort nur
  geleert, wenn er der von SchulSync kopierte Raum ist (der verwaltete Block nennt genau „Raum: <Ort>“); ein anderer Ort hat der Nutzer eingetragen und bleibt.
  Weil Antwort oder Prozess verloren gehen können, hält der Zustand VOR jedem Patch fest, dass der Google-Stand unbekannt ist (Termin:
  `event_applied_hash` = NULL; Task: `task_applied_hash` = NULL, bei einem Patch MIT Datum zusätzlich `task_base_deadline` = `unknown`, was der Planer als
  Friständerung liest). Erst der Erfolg schreibt den neuen Stand. Kehrt die Quelle nach einem unklaren Patch auf den alten Inhalt zurück, wird
  also neu geschrieben statt fälschlich "unverändert" angenommen.
- `dirty` wird nie gelöscht, solange für den Eintrag eine Absicht offen ist (Wiederherstellung konnte nicht laufen): erst die Klärung gleicht den
  gefundenen Orphan mit dem aktuellen Quellstand ab (z. B. zurückgezogen -> "Abgesagt:" bzw. Task entfernen).
- Gelöschte Tasks nach Datenbankverlust oder bei offener `create/tasks`-Absicht (Zustand NONE, keine lebende Task mit der Marke; `_settle_by_tombstones`): die API
  liefert gelöschte Tasks (`showDeleted`). Eine gelöschte Task, die erledigt oder ausgeblendet ist, ist `DONE` (SchulSync löscht nur OFFENE Tasks). Eine sonst
  gelöschte Task OHNE Entfernungsvermerk hat der Nutzer gelöscht -> `USER_DELETED` (Hinweis `user_deleted_task`), kein zweites Anlegen. Eine gelöschte Task MIT
  Entfernungsvermerk hat SchulSync bei einer Absage entfernt und sagt nichts über den Nutzer: eine Reaktivierung darf neu anlegen. Nach Absage/Reaktivierung und
  nach `restore` (Zustand REMOVED) beweist ein alter Grabstein nichts: dort wird neu angelegt.
- Event-ID: gehört die ID `make_event_id(uid, gen)` bereits dem Zustand eines ANDEREN Eintrags (Identitätszuordnung §4.3.2 übergibt den Termin samt
  alter ID an die neue `uid`), wird die nächste Generation verwendet; der Abgleich per ID bleibt deterministisch.
- Fällt nach der Zielprüfung ein Ziel aus (Aufgabenliste fehlt, Kalender verschwindet), werden nur die Schreibvorgänge DIESES Ziels zurückgestellt
  und im Bericht dem richtigen Ziel zugeordnet; `GoogleAuthError` betrifft beide (gemeinsame Anmeldung).
- Unerwartete Ausnahmen (Programmfehler) eines Eintrags: die Engine wird vom Runner mit `contain_errors=True` gebaut; der Eintrag bleibt `dirty`,
  erscheint als `FAILED`-Change (Code `unexpected`) und die übrigen Einträge beider Quellen werden weiter geschrieben. Ohne das Flag
  propagieren Programmfehler (Tests/Aufrufer). Einzelne Lone Surrogates in Texten der Quelle ersetzt `ingest` durch U+FFFD.
- Task entfernen (Absage): frisch lesen (`get_task`: erledigt/ausgeblendet -> `DONE`, nichts löschen), Absicht `delete/tasks`, Entfernungsvermerk, `delete_task`
  (404 = ok) -> Zustand REMOVED. Erledigte bleiben. Der Entfernungsvermerk (`textblocks.task_removed_line`: `schulsync-removed: <uid>`) wird als erste Zeile der Notizen
  gepatcht (nie ein Status), damit eine gelöschte Task später als „von SchulSync entfernt“ erkennbar ist. Best effort: ein Fehler oder Notizen am API-Limit kosten nur
  diese Unterscheidung (dann gilt im Zweifel „vom Nutzer gelöscht“).
- Zielwechsel: Beim ersten erfolgreichen `check_targets` werden die konfigurierten `calendar_id`/`tasklist_id` in `meta` (`target.*`) gemerkt. Weicht die
  Konfiguration später ab (Kalender oder Liste neu angelegt, `schulsync setup` fand neue Kennungen), würde jeder bekannte Termin 404 liefern und als
  „vom Nutzer gelöscht“ gelten. Deshalb setzt `SyncEngine.reset_targets_if_changed` (der Runner ruft sie VOR dem Tagesgate auf, auch in der Vorschau und
  bei `restore`) den Google-Zustand ALLER Einträge zurück: Termin-ID und angewendete Hashes leer, `event_state` NONE mit `event_gen`+1, Task-ID/-Zustand/
  Hash/Basisfrist/Fälligkeit leer, `dirty`; offene Absichten werden abgebrochen; Hinweis `targets_changed`. Vollständig vergangene/abgesagte Einträge
  überspringt der Planer wie immer. Bewusst anders als „alles“: eine vom Nutzer ERLEDIGTE Task bleibt erledigt (ANFORDERUNGEN §6, keine neue offene
  Kopie). Eine Datenbank aus der Zeit vor dieser Funktion lernt die Kennungen beim ersten Lauf, ohne Rücksetzung. In den alten Zielen wird nie geschrieben.
- Wiederherstellen (`restore`): Termin `event_gen`+1 und Zustand NONE; Task `USER_DELETED` -> `REMOVED` (Grund `restored`), NICHT NONE: für NONE läse die Wiederherstellung
  nach einem unklaren Anlegen den Grabstein der gerade freigegebenen Task als neue Nutzerlöschung. Je Kennung ein ausdrückliches Ergebnis (`RestoreEntry`): wiederhergestellt / nichts wiederherzustellen (unbekannt oder
  nicht gelöscht) / nicht wiederhergestellt, weil der Eintrag abgesagt oder vollständig vergangen ist (der Planer würde ihn ohnehin überspringen; der Zustand
  bleibt dann unverändert). Ausgabe in `RestoreResult`, Konsole und Berichtshinweisen (`restore_done`, `restore_blocked`, `restore_nothing`).
- `check_targets()` schlägt fehl (Kalender/Liste fehlt, Hauptkalender, Rechte) -> nur die Schreibvorgänge des BETROFFENEN Ziels bleiben `dirty` mit Changes `DEFERRED`
  und Fehler im Bericht. Das echte Gateway und FakeGoogle werfen dazu (DryRun reicht es durch) `google_targets.TargetsUnavailableError` (Unterklasse von `TargetUnavailableError`;
  `down`: Ziel -> Fehler, `verified`: geprüfte, nutzbare Ziele). Ein Kalender wird nur nach bestätigter Prüfung beschrieben (nie Hauptkalender, Schreibrecht), ein nicht
  geprüfter wartet; die Aufgabenliste prüft ihr eigener Lesezugriff (`_ensure_list`). Gemeinsame Anmeldung (`GoogleAuthError`), Netzfehler und unklassifizierte Fehler
  halten alle Schreibvorgänge an. NIE Ausweichen auf anderes Ziel. Die Kennungen werden nur nach einer vollständig erfolgreichen Prüfung gemerkt (`target.*`).
- Wiederholungen für Lese-/idempotente Aufrufe: begrenzt (`retry`-Konfig), steigende Wartezeit, nur bei `TransientError`.

## 7. Tageslauf (`runner.py`)

1. Prozesssperre (nicht blockierend). Gesperrt: AUTO still beenden (Exit 0, Logzeile), MANUAL Meldung "läuft bereits".
2. Tagesgate (nur AUTO und nicht `--force`): Für jede aktivierte Quelle `source_days(heute)` mit `fetch_complete` und `writes_done_at` und keine
   offenen Absichten/`dirty`-Einträge -> fertig. Alle Quellen fertig -> still beenden (OverallStatus NOTHING_TO_DO, kein Bericht, kein Browser).
   Teilweise fertig: nur die übrigen Quellen bearbeiten; hat eine Quelle heute vollständig abgerufen (Google-Schreiben fehlt noch), nur Schreiben nachholen (kein erneuter Abruf).
   Keine nachträglichen Tagesläufe für verpasste Tage: nur der heutige Tag zählt; ein Lauf prüft den aktuellen Datenbestand genau einmal.
3. Netz abwarten (AUTO): bis `network_wait_seconds` (DNS+TCP 443 zu den konfigurierten Hosts), dann trotzdem versuchen.
4. Je Quelle unabhängig: Anmeldung/Abruf mit begrenzten Wiederholungen (`max_attempts`, `delays_seconds`), nur bei `TransientError`.
   `AuthError` bzw. ein Anmeldefehler mit Code `auth`/`access_denied` -> keine Wiederholung, `auth_blocked_at` setzen. Jeder andere Anmeldefehler
   (z. B. HTTP 404, geänderte Seite) lässt nur diesen Start der Quelle scheitern, ohne Sperre. Eine blockierte Quelle wird in AUTO übersprungen
   (Fehler im Bericht) bis sie wieder funktioniert. Fehler einer Quelle beeinflussen die andere nie: auch eine unerwartete Ausnahme (Adapter-Fabrik,
   Parser, Aufnahme) macht nur diese Quelle `FAILED` (Fehlercode `unexpected`, kein Tageserfolg). `run_timeout_seconds` ist eine harte Obergrenze.
5. Aufnahme (§4.3) je Quelle; unvollständiger Abruf (`!fetch.complete`) darf aufnehmen/aktualisieren, aber nie Absagen/Löschungen ableiten
   und zählt nicht als Tageserfolg der Quelle.
6. Google-Schreibphase (§6). Danach je Quelle `record_fetch` und, wenn kein `dirty`/keine offene Absicht dieser Quelle übrig ist, `record_writes_done`.
7. `RunReport` bauen (Zähler je Quelle/Ziel, Changes, Notices, nächste Schritte, letzter Erfolg/veraltet). Immer speichern (Bericht-Datei, begrenzte
   Aufbewahrung). Browser öffnen nur wenn `needs_attention` UND Fingerabdruck heute noch nicht geöffnet UND `open_on != never`;
   Öffnen scheitert -> Pfad loggen, Bericht bleibt. Gemerkt werden ALLE Fingerabdrücke des Tages (Problem A, B, A öffnet keinen dritten Tab) in
   `report_opened` (Schema v2). Kein JSON-Ersatz daneben: Ist die Datenbank unbrauchbar, prüft `cli run` sie vorab und behandelt das wie jeden Fehler
   außerhalb des Runners (Bericht + ein Tab je Tag und Fehlerart über die Markerdatei `auto_failure_report.json`); ein direkt aufgerufener Runner
   öffnet dann ohne Gedächtnis. Kann der HTML-Bericht nicht geschrieben werden, loggt der Runner den vorgesehenen Pfad und legt eine Textkopie
   (`logs/bericht-<Tag>.txt`) neben die Logs, die wie der Bericht geöffnet wird; scheitert auch das, bleibt nur das Log.
   Ein Lauf, der mit einem `SchulSyncError` endet, wird mit Code und Meldung (maskiert) geloggt.
8. Vorschau (`RunMode.PREVIEW`): Quellen lesen, `StateStore.copy_to_memory()`, `DryRunGateway`; es wird nichts in der echten DB verbucht
   (`source_days`, `runs`, `report_opened` bleiben unberührt) und nichts bei Google geschrieben. Ausgabe: geplante `Change`s.
- Gesamtstatus: SUCCESS (alle aktivierten Quellen SUCCESS/ALREADY_DONE und nichts ausstehend), FAILED (keine Quelle erfolgreich, mindestens eine
  fehlgeschlagen), sonst PARTIAL. Quelle SUCCESS nur bei vollständigem Abruf UND erledigten Schreibvorgängen.
- Exit-Codes: 0 Erfolg/nichts zu tun/still beendet, 1 Teilfehler oder Fehler, 2 Aufruf-/Konfigurationsfehler.

## 8. Adapter-Vertrag (`models.SourceAdapter`)

- `fetch(window, tracked, now)`: liefert `FetchResult`. Neue Einträge nur aus dem Fenster zwingend; zusätzlich für `tracked` (bekannte, nicht
  vollständig vergangene Einträge) den Datumsbereich/die Detailansicht so abdecken, dass Verschiebungen/Absagen erkannt werden.
- Pro aktivierter Kategorie ein `CategoryOutcome`. Echte leere Liste = OK mit 0. Loginseite/Weiterleitung auf Login, 401/403, Ladefehler, abgeschnittene
  Paginierung, unerwartete Struktur (`SchemaError`) = FAILED/INCOMPLETE mit `ErrorInfo` – nie "leer".
- Vollständige Titel/Beschreibung/Frist/Kennung aus Detailansichten lesen, wenn die Listenansicht kürzt. Bleibt ein Titel abgeschnitten oder fehlt eine
  Angabe: Eintrag trotzdem liefern + `notes`/Notice `incomplete_data` (nie erfinden).
- Eintrag ohne Datum: NICHT als Item liefern, sondern Notice `no_date` (WARNING) mit Titel.
- Datum ohne Uhrzeit -> `ItemTime(date)`. Uhrzeiten als aware Europe/Vienna.
- `withdrawn`/`status=CANCELLED` nur bei ausdrücklichem Signal der Quelle (siehe §4.3).
- Geheimnisse (Passwort, Token, Cookies, `sesskey`) nie in Ausnahmetexten, Notices, Logs.
- Selbstprüfung `check()`: reale Schritte (Erreichbarkeit, Anmeldung, je Kategorie ein kleiner Abruf) mit deutschen Details; sie IST die reale
  Datenzugriffsprüfung auf dem Zielsystem (`schulsync check`).

## 9. Fehlerbericht (`report.py`)

Eigenständiges HTML5, Deutsch, ohne externe Ressourcen, mit strikter CSP-`meta` (`default-src 'none'; style-src 'unsafe-inline'`), alle fremden Texte
`html.escape`, Links nur `http(s)` und als `rel="noopener noreferrer"`; sonst Klartext. Inhalt gemäß ANFORDERUNGEN §8. Dateiname je Tag stabil
(`bericht-YYYY-MM-DD.html`), bei weiteren Läufen desselben Tages überschrieben; Aufbewahrung `report.keep_days`. Öffnen über `os.startfile`/`webbrowser`
im Benutzerkontext. Alles durch `redact.redact()` laufen lassen.

## 10. Sicherheit/Betrieb

- DPAPI-Speicher pro Eintrag (`CryptProtectData`, Benutzerprofil), Datei `secrets.dat`, atomares Schreiben (Temp-Datei mit eindeutigem Namen,
  `os.replace` mit begrenzter Wiederholung bei PermissionError). Schreiber (lesen-ändern-schreiben) werden durch eine Prozess-interne Sperre und eine
  OS-Dateisperre auf `secrets.dat.lock` (eine andere Datei als die Programmsperre) serialisiert; Lesen braucht keine Sperre. Schlüssel:
  `eduvidual_password`, `webuntis_password`, `google_client` (OAuth-Client-JSON), `google_token`. Geladene Geheimnisse bei `redact` registrieren.
- Prozesssperre über OS-Dateisperre (`msvcrt.locking`), wird beim Prozessende automatisch frei (keine Stale-Locks). `run`, `restore`, `check` und `setup`
  nehmen sie (nicht blockierend); `status` und `report` laufen ohne Sperre.
- Harte Zeitbegrenzung: `run_timeout_seconds` ist nur kooperativ. `cli run` startet einen Watchdog-Thread, der nach `run_timeout_seconds` + 60 s einen
  FAILED-Bericht (Code `timeout`) schreibt und den Prozess mit `os._exit(3)` beendet; das Betriebssystem gibt die Sperre frei, der nächste Start holt nach.
- Aufgabenplanung: Task-XML mit `LogonTrigger` (UserId = aktueller Benutzer, `Delay` ~PT1M), `InteractiveToken`/`LeastPrivilege`, kein Admin,
  Aktion `pythonw.exe -m schulsync run --auto`, `MultipleInstancesPolicy=IgnoreNew`, Zeitlimit, Akku-freundlich. Ausweichlösung: `.lnk` im Startup-Ordner.
  Zustand der Aufgabe (`schtasks /Query`): „nicht vorhanden“ gilt NUR bei Exit-Code 1 plus erkennbarem Nicht-gefunden-Hinweis („cannot find the file“,
  „kann die angegebene Datei nicht finden“, `0x80070002`); jeder andere Fehlschlag (Zugriff verweigert, unbekannte Sprache, Zeitüberschreitung) ist
  „unbekannt“: `status` sagt das mit der Originalzeile, `remove` versucht das Löschen trotzdem und meldet, was wirklich geschah.
- Google-Scopes minimal: `calendar.events`, `calendar.calendarlist.readonly` (nur zum Auffinden des Kalenders „Schule“), `tasks`.
  OAuth-App-Status "Testing" -> Refresh-Token läuft nach 7 Tagen ab; für Dauerbetrieb "In Produktion" setzen (unverifizierte App-Warnung ist für den
  eigenen Account normal). `invalid_grant` -> `GoogleAuthError` mit klarer Neuanmelde-Anleitung. Keine Garantie auf unbegrenzte Gültigkeit.

## 11. Testmatrix (ANFORDERUNGEN §10 -> mindestens ein Test je Zeile)

Dubletten-Freiheit (2× gleiche Daten) · mehrere Anmeldungen/Tag + Wiederholung nach Fehler · Teilfehler je Quelle/Ziel · Prozessabbruch nach Google-Schreiben ·
Fristverschiebung · Absage · Reaktivierung · erledigte Tasks · persönliche Notizen · 3-/7-Tage-Vorlauf · verspätete Entdeckung · überfällige Tasks unverändert ·
Zeitumstellung (März/Oktober) · fehlende Uhrzeiten · 21-Tage-Fensterränder · unvollständiger Abruf ohne Löschung · Kalender "Schule" fehlt/Hauptkalender ->
kein Ausweichen · HTML-Bericht (escaping, keine Geheimnisse, Öffnen im Benutzerkontext, kein Öffnen bei Erfolg, Tab-Flut-Schutz) · Vorschau ohne Schreiben/ohne
Erfolgsverbuchung · Prozesssperre · Netz-/Zeitlimits/Backoff · Auth-Fehler ohne Endlosschleife · Nutzer löscht Termin/Task -> nicht neu anlegen, `restore`.
