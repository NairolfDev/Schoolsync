# Datenzugriff WebUntis (HTBLA Grieskirchen) – ehrlicher Bericht

Stand: 4. Oktober 2026. Betrifft `src/schulsync/adapters/webuntis.py`.
Grundsatz aus den Anforderungen (Abschnitt 3): keine erfundenen Schnittstellen. Dieser Bericht trennt deshalb
strikt zwischen **real geprüft**, **aus fremdem Quellcode/Dokumentation belegt** und **nur angenommen**.

## 1. Kurzfassung

- Für die Schul-WebUntis-Instanz ist **nichts hinter der Anmeldung real geprüft**. Es gab keine Zugangsdaten und es
  wurde nie ein Anmeldeversuch unternommen. Alle Antwortformen stammen aus Quellcode von Drittprojekten.
- Der Adapter ist **nur gegen synthetische Beispieldaten getestet** (`tests/fixtures/webuntis`, Vermerk
  „synthetic, not recorded“). Die Tests belegen die Regeln und die Fehlerbehandlung, nicht, dass WebUntis genauso
  antwortet.
- Ob der Zugriff auf deinem PC funktioniert, zeigt erst **`schulsync check`** (siehe Abschnitt 6). Schlägt ein Weg real
  fehl, steht das im Prüfbericht und nicht als stilles „leer“. Der dokumentierte nächste Schritt wäre dann ein
  Browser-Adapter (wurde bewusst nicht gebaut, solange nicht nötig).
- Es wird ausschließlich gelesen. Einzige schreibende Anfrage ist die Anmeldung selbst (siehe Abschnitt 7).

## 2. Real geprüft (ohne Anmeldung, 2026-10-04)

Alle Anfragen waren einfache, ohne Zugangsdaten mögliche Aufrufe (insgesamt unter zehn, höflich, mit eigenem
User-Agent):

| Anfrage | Beobachtung |
|---|---|
| `GET https://htbla-grieskirchen.webuntis.com/WebUntis/?school=htbla-grieskirchen` | HTTP 200; setzt die Cookies `JSESSIONID`, `schoolname`, `Tenant-Id`; eingebettete Konfiguration enthält `mandantName=htbla-grieskirchen`, `hideWuLogin=false` (Passwortanmeldung wird angeboten), `csrfHeader=X-CSRF-TOKEN` (Version laut Statik 2027.2.4) |
| `POST /WebUntis/jsonrpc.do?school=htbla-grieskirchen` mit Methode `getLatestImportTime` (ohne Sitzung) | HTTP 200 mit `{"error":{"message":"not authenticated","code":-8520}}` – die JSON-RPC-Schnittstelle ist aktiv und verlangt eine Anmeldung |
| `GET /WebUntis/api/exams?...` ohne Sitzung | HTTP 302 auf `/WebUntis/index.do` – der Endpunkt existiert, Sitzung nötig |
| `GET /WebUntis/api/homeworks/lessons?...` ohne Sitzung | HTTP 302 auf `/WebUntis/index.do` |
| (frühere Prüfung, siehe DESIGN §1) `/api/token/new` ohne Sitzung | 302 auf die Anmeldeseite |
| (frühere Prüfung, siehe DESIGN §1) `/api/rest/view/v1/app/data` ohne Sitzung | 404 mit Header `X-Untis-School-Url: unknown` |

Daraus folgt nur: Die Endpunkte sind vorhanden. **Nicht** geprüft: Anmeldeablauf, Antwortformen, Rechte der
Schülerrolle, ob ein CSRF-Header für die Anmeldung nötig ist, Zwei-Faktor-Pflicht.

## 3. Aus Quellen belegt, aber nicht gegen dein Konto geprüft

Die Quellen wurden am 4. Oktober 2026 im Quelltext gelesen (nicht aus dem Gedächtnis).

| Annahme | Quelle |
|---|---|
| JSON-RPC `authenticate` mit `user`, `password`, `client` liefert `sessionId`, `personType`, `personId`, `klasseId`; Fehlercodes -8504 (falsche Zugangsdaten), -8520 (nicht angemeldet), -7004/-8507 (Datum nicht erlaubt), -32601 (Methode fehlt) | python-webuntis (`webuntis/session.py`, `webuntis/utils/remote.py`), SchoolUtils/WebUntis (`src/base.ts`) |
| `getTimetable` mit `options.element {id, type}` (Typ 5 = Schüler), `startDate`/`endDate` als Ganzzahl `yyyymmdd`; Stunde mit `id`, `date`, `startTime`, `endTime` (Ganzzahl `Hmm`, z. B. 800 = 08:00), `su`/`te`/`ro`, `code`, `info`, `substText` | python-webuntis (`objects.py`), SchoolUtils/WebUntis (`src/types.ts`, `src/base.ts`) |
| `code` ist fehlend (regulär), `"cancelled"` (Entfall) oder `"irregular"` (Supplierung/nicht geplante Stunde) | python-webuntis `objects.py` (`PeriodObject.code`), SchoolUtils `types.ts` |
| `getSchoolyears` liefert Schuljahre mit `startDate`/`endDate` | SchoolUtils `base.ts`, python-webuntis |
| `GET /WebUntis/api/homeworks/lessons?startDate&endDate` (Cookies `JSESSIONID` + `schoolname`) liefert `{"data":{"homeworks":[{id, lessonId, date, dueDate, text, remark, completed, attachments}],"lessons":[{id, subject, ...}]}}` | SchoolUtils `base.ts`/`types.ts`, stundenglas `core/src/untis.rs` |
| `GET /WebUntis/api/exams?startDate&endDate&klasseId=-1&withGrades=false` (zusätzlich `studentId`) liefert Prüfungen mit `id`, `examType`, `name`, `examDate`, `startTime`, `endTime`, `subject`, `teachers[]`, `rooms[]`, `text`; es wurde auch eine zweite Form mit ISO-Zeiten `startDateTime`/`endDateTime` und ohne `data`-Umschlag beobachtet | SchoolUtils `base.ts`/`types.ts`; stundenglas `SCOUT.md` und `core/src/untis.rs` (beide Formen) |
| Formularanmeldung `POST /WebUntis/j_spring_security_check` (`school`, `j_username`, `j_password`, `token=""`): Erfolg = 302 nach `index.do`, Ablehnung = 302 zurück zur Anmeldeseite, `{"state":"TOKEN_REQUIRED"}` = Zwei-Faktor | stundenglas `SCOUT.md` §1, `untis.rs` |
| `GET /WebUntis/api/token/new` (Cookie) liefert ein Bearer-JWT; alle `/api/rest/view/v*`-Pfade antworten ohne Bearer mit 404; `app/data` liefert `user.person.id` | stundenglas `SCOUT.md` §1 (beschrieben für WebUntis 2027.2.1, deine Schule läuft laut Probe auf 2027.2.4) |
| `GET /api/rest/view/v1/timetable/entries?start&end&format=3&resourceType=STUDENT&resources=<id>&periodTypes=&timetableType=MY_TIMETABLE` liefert `days[].gridEntries[]` mit `ids[]`, `duration{start,end}` (lokale Wanduhrzeit ohne Offset), `type` (`NORMAL_TEACHING_PERIOD`/`EVENT`), `status` (`REGULAR`/`CHANGED`/`CANCELLED`) | stundenglas `SCOUT.md` §2, `untis.rs` |

Hinweis aus derselben Quelle: Über JSON-RPC lässt sich eine Supplierung teils nicht von einer regulären Stunde
unterscheiden (beide ohne `code`). Für Unterrichtsentfälle ist das unerheblich, weil nur `cancelled` zählt.

## 4. Gewählte Wege und Ausweichwege

Je Kategorie gilt eine feste Reihenfolge. `schulsync check` nennt den Weg, der auf deinem PC tatsächlich benutzt wurde.

| Teil | Weg 1 (bevorzugt) | Ausweichweg | Wann Ausweichweg |
|---|---|---|---|
| Anmeldung | JSON-RPC `authenticate` | Webformular `j_spring_security_check` | nur wenn `jsonrpc.do` gar kein JSON-RPC spricht (HTTP 404/405, Weiterleitung, kein JSON, Code -32601). Jede inhaltliche Ablehnung beendet die Anmeldung. **Höchstens ein Anmeldeversuch pro Lauf**, nie wiederholt (kein Sperrrisiko). |
| Hausaufgaben | REST `/api/homeworks/lessons` (Cookie) | keiner belegt | – |
| Prüfungen/Schularbeiten | REST `/api/exams` (Cookie, `klasseId=-1`) | keiner belegt (REST-View `exams` bewusst nicht genutzt: Antwortform nicht belegt) | – |
| Unterrichtsentfall | JSON-RPC `getTimetable` für das eigene Schülerelement | REST `timetable/entries` mit Bearer-Token | wenn JSON-RPC-`getTimetable` dauerhaft nicht nutzbar ist (JSON-RPC-Fehler, 404, oder die Anmeldung musste per Webformular erfolgen). **Nicht** bei abgelaufener Sitzung oder „Datum nicht erlaubt“ – das sind Fehler bzw. unvollständige Abrufe. |

## 5. Fachregeln des Adapters

**Was übernommen wird:** Hausaufgaben (Art Hausaufgabe), Prüfungen/Schularbeiten (Art Prüfung, Untertyp = Prüfungsart
aus der Quelle, z. B. „Schularbeit“) und Unterrichtsentfälle. Reguläre Stunden, Supplierungen und reine
Lehrkraft-/Raumänderungen werden nie übernommen. Der Stundenplan wird nur gelesen, um Entfälle zu erkennen.

**Entfall ohne Verwechslung mit Supplierung** (Quellen: Abschnitt 3):
- JSON-RPC: nur `code == "cancelled"` ist ein Entfall. `irregular` (Supplierung, nicht geplante Stunde), fehlender `code`
  (regulär) und Raum-/Lehrkraftwechsel (erkennbar an `orgid`) werden ignoriert. Unbekannte Codes werden nie geraten.
- REST: nur `status == CANCELLED` bei `type == NORMAL_TEACHING_PERIOD`. `CHANGED`, `REGULAR` und Ereignisse (`EVENT`)
  werden ignoriert.
- Grenzfall (nicht geprüft): Wie Untis eine **Verlegung** codiert, ist unbekannt. Erscheint die alte Stunde als
  `cancelled`, wird sie als Entfall gemeldet; die neue Stunde ist `irregular` und wird nicht übernommen.

**Kennungen (stabil im Sinne der Anforderungen):**
- Hausaufgabe: die `id` der Hausaufgabe. Prüfung: die `id` der Prüfung. Eine Terminverschiebung ändert die Identität nicht.
- Unterrichtsentfall: `<Stunden-id>-<yyyymmdd>-<HHMM>` (Stunden-id aus JSON-RPC bzw. die erste `ids`-Zahl aus REST,
  Datum und Startzeit der Stunde). Wird eine Stunde verlegt, entsteht ein neuer Entfall; das ist gewollt, weil eine
  Stunde zu einem anderen Zeitpunkt ein anderes Ereignis ist.
- Nur angenommen: ob Untis Stunden-IDs nach einem Neuimport der Stundenplandaten behält und ob JSON-RPC-`id` und
  REST-`ids` dieselben Zahlen sind. Bei einem Routenwechsel zwischen zwei Läufen könnte daher ein Entfall einmal doppelt
  erscheinen.

**Zeit:** Hausaufgaben haben nur ein Fälligkeitsdatum und werden ganztägig geführt. Prüfungen tragen Datum plus Start-/
Endzeit (fehlende Uhrzeit oder unplausible Endzeit: ganztägig bzw. Endzeit unbekannt, mit Hinweis). Entfälle tragen die
exakte Start-/Endzeit als Europe/Vienna-Zeit (inklusive Sommer-/Winterzeit-Umstellung).

**Hausaufgaben-Titel (feste Regel, keine KI):** erste nicht leere Zeile des Hausaufgabentexts, höchstens 100 Zeichen
(an der Wortgrenze gekürzt, mit „…“). Wird gekürzt, steht ein Hinweis im Eintrag und im Bericht; der **vollständige**
Text steht immer in der Beschreibung. Fach = Fach der zugehörigen Stunde. Ohne Text lautet der Titel „Hausaufgabe
<Fach>“ mit Warnung. Ohne Fälligkeitsdatum wird kein Eintrag erzeugt, sondern im Bericht aufgeführt (das
Zuweisungsdatum ist keine Frist). Anhänge werden nicht übernommen, ihr Vorhandensein wird vermerkt. Der
Erledigt-Status in WebUntis wird ignoriert und nie verändert.

**Datumsbereiche:** Abgerufen wird die Vereinigung aus dem Prüffenster (heute 00:00 bis heute + 21 Tage, Ende
ausgeschlossen) und den Zeiträumen der bekannten, noch offenen Einträge. Zurückgegeben werden nur Einträge im Fenster
oder mit bekannter Kennung. Lange Bereiche werden in Stücke von höchstens 62 Tagen geteilt (vorsorglich; ein Limit ist
in den Quellen nicht belegt). JSON-RPC-Abrufe werden auf die Schuljahre zugeschnitten (außerhalb gibt es keinen
Stundenplan; Code -7004/-8507). Hausaufgaben werden zusätzlich 28 Tage rückwärts gelesen und nach Fälligkeitsdatum
gefiltert, weil nicht belegt ist, ob der Server nach Zuweisungs- oder Fälligkeitsdatum filtert. Gibt es bekannte
Hausaufgaben bzw. Prüfungen, wird für diese Kategorie zusätzlich ab Fensterbeginn bis zum Schuljahresende (31.08.,
Annahme: Österreich) gelesen, damit eine verschobene Frist nicht unbemerkt veraltet (mehrere Abrufe zu je höchstens
62 Tagen). Einträge ohne Datum (Prüfung oder Hausaufgabe) werden nicht geliefert, sondern als Hinweis `no_date`
gemeldet. Hinweise zu Einträgen außerhalb von Fenster und Verfolgung werden nicht erzeugt.

**Rücknahme eines Entfalls:** Nur wenn dieselbe Stunde (gleiche Kennung) im abgerufenen Stundenplan ausdrücklich ohne
Entfall auftaucht (JSON-RPC: Stunde vorhanden ohne `code` oder mit `irregular`; REST: `REGULAR`/`CHANGED`), meldet
der Adapter eine Rücknahme mit deutschem Beleg. Fehlt die Stunde in der Liste, ist die Liste leer, ist der Status
unbekannt oder schlug der Abruf fehl, gibt es **keine** Rücknahme. Für Hausaufgaben und Prüfungen gibt es kein
Rücknahmesignal; sie werden nie als zurückgenommen gemeldet.

**Fehlerarten (nie „leer“):**

| Situation | Ergebnis |
|---|---|
| Echte leere Liste | Kategorie in Ordnung, 0 Einträge |
| Anmeldung abgelehnt (falsches Passwort, Zwei-Faktor, kein Schülerkonto, 403 bei der Anmeldung) | Anmeldefehler, alle Kategorien fehlgeschlagen, kein Wiederholen |
| Loginseite/Weiterleitung statt JSON, Sitzung mitten im Abruf abgelaufen | betroffene und alle folgenden Kategorien fehlgeschlagen („Sitzung abgelaufen“), keine weiteren Anfragen |
| 403 auf JSON-RPC `getTimetable` | Ausweichen auf den REST-Stundenplan (wie bei JSON-RPC-Code -8509); 403 auf `getSchoolyears` schaltet nur das Zuschneiden ab |
| 403 für eine Kategorie | nur diese Kategorie fehlgeschlagen, mit verständlicher Meldung; die übrigen sind unberührt |
| Unerwartete Antwortstruktur | Kategorie fehlgeschlagen; die Meldung nennt den JSON-Pfad (nie Datenwerte) |
| Das Datumsfeld (`dueDate` bei Hausaufgaben, `examDate`/`startDateTime` bei Prüfungen) fehlt in **allen** Einträgen einer Antwort mit mindestens zwei Einträgen | wie „Unerwartete Antwortstruktur“ (WebUntis hat das Feld vermutlich umbenannt), nicht „N Einträge ohne Datum, Kategorie in Ordnung“. Ein einzelner Eintrag ohne Datum bleibt der Hinweis `no_date` (Heuristik: ein einzelner Eintrag lässt sich nicht von einem rechtmäßig undatierten unterscheiden). Ein undatierter Eintrag trägt das Feld mit dem Wert 0. |
| „Datum nicht erlaubt“, Fehler im REST-Stundenplan | Kategorie unvollständig |
| Netzfehler, 5xx, 429 | vorübergehender Fehler wird nach oben gegeben (der Tageslauf wiederholt begrenzt) |
| Kategorie in der Konfiguration abgeschaltet | „abgeschaltet“, wird nicht abgerufen und zählt nicht als fehlend |

## 6. Was du mit `schulsync check` prüfen musst

`schulsync check` meldet in deutschen Schritten: Erreichbarkeit, Anmeldeweg, Schülerkennung und je Kategorie einen
kleinen Abruf (nächste 7 Tage) mit Weg und Anzahl. So liest du das Ergebnis:

1. **Anmeldung fehlgeschlagen:** Zugangsdaten mit `schulsync setup` neu eingeben. Es gab genau einen Versuch. Meldet
   WebUntis Zwei-Faktor, wird das nicht unterstützt (persönlicher Zugang ohne Zwei-Faktor nötig).
2. **Anmeldung ok, aber Hausaufgaben/Prüfungen „Sitzung abgelaufen“:** Der Sitzungscookie passt vermutlich nicht zur
   Anmeldeantwort (der Prüfbericht gibt dazu einen Hinweis). Das ist eine offene Annahme (Abschnitt 8) und braucht eine
   Anpassung des Adapters; bitte die Meldung melden, nicht Zugangsdaten ändern.
3. **„Kein Zugriff“ bei einer Kategorie:** Die Schule hat dem Schülerkonto dieses Recht nicht gegeben. Die anderen
   Kategorien laufen weiter. Das lässt sich nur bei der Schule ändern.
4. **„Unerwartete Antwortstruktur“:** WebUntis liefert ein anderes Format als dokumentiert. Das ist der erwartete
   Fehler bei geänderten Seiten, nicht „leer“.
5. **Plausibilität:** Vergleiche die gemeldeten Anzahlen mit deiner WebUntis-Ansicht für die nächsten Tage
   (Hausaufgaben, Prüfungen, ausgefallene Stunden). Zeigt `check` bei bekannten Einträgen 0, ist ein Weg vermutlich leer
   oder falsch gefiltert.

## 7. Lesezugriff

Der Adapter sendet nur: GET-Anfragen, die Anmeldung (JSON-RPC `authenticate` bzw. das Login-Formular) und die lesenden
JSON-RPC-Methoden `getSchoolyears` und `getTimetable`. Es gibt keinen Aufruf, der Erledigt-Häkchen, Hausaufgaben,
Prüfungen oder andere Inhalte ändert. Das ist doppelt abgesichert: Die Schicht für JSON-RPC verweigert **im Code** jede
Methode außerhalb der festen Liste `READ_ONLY_RPC_METHODS` (`authenticate`, `getSchoolyears`, `getTimetable`), bevor irgendetwas
gesendet wird (wie bei Eduvidual), und Tests prüfen zusätzlich das Anfrageprotokoll. Unvermeidbare Nebenwirkungen beim Anmelden
(Sitzung, „zuletzt angemeldet“) bleiben. Passwort, Cookies, Sitzungskennung
und Bearer-Token stehen nie in Meldungen, Berichten oder Protokollen; Adressen werden ohne Parameter ausgegeben. Das
Passwort steht ausschließlich im Body der Anmeldeanfrage.

## 8. Was nicht möglich ist bzw. offen bleibt

- Keine Prüfung gegen echte Daten: Alle Feldnamen, Statuswerte und Formate sind aus Fremdquellen übernommen.
- Ob JSON-RPC-`authenticate` das Webcookie so setzt, dass die klassischen Endpunkte (Hausaufgaben/Prüfungen) damit
  funktionieren, ist in den Quellen nur indirekt belegt (SchoolUtils nutzt `sessionId` als `JSESSIONID`-Cookie). Der
  Prüfbericht warnt, falls Cookie und Sitzungskennung abweichen.
- Ob die Formularanmeldung auf dieser Instanz einen CSRF-Header (`X-CSRF-TOKEN`) verlangt, ist unbekannt (die
  Referenzimplementierung sendet keinen). Das Ergebnis wäre ein klarer Anmeldefehler.
- Keine Paginierung der klassischen Endpunkte belegt; eine abgeschnittene Antwort ließe sich daher nicht erkennen.
- Microsoft-Anmeldung (Office-365-SSO) wird nicht unterstützt; nur Benutzername und Passwort.
- Nicht erkennbar: Rücknahmen von Hausaufgaben und Prüfungen (kein Signal in den Quellen) und Entfälle, die WebUntis
  gar nicht als Entfall führt (z. B. Entfall nur als Freitext).
- Dass diese Schule die Schnittstellen in der Praxis freigibt, ist erst mit deinem Konto prüfbar. Die Schule kann
  JSON-RPC oder einzelne Rechte für Schüler abschalten; dann zeigt `check` genau, welcher Teil fehlt.

## 9. Kalenderexport (iCal) – bewertet, nicht verwendet

Anforderung (Abschnitt 3): Ein Kalenderexport genügt nur für die Daten, die er tatsächlich vollständig liefert. Deshalb wurde
bewertet, ob WebUntis einen solchen Export anbietet und was er enthält.

**Gibt es einen Export?** Ja, laut der Hersteller-Dokumentation (Untis-Hilfe „Kalender-Abonnement per iCal“, siehe Quellenverzeichnis):
Schüler und Lehrer können in ihrem Profil unter „Datenzugriff“ die Funktion „Kalender veröffentlichen“ einschalten. WebUntis zeigt
dann einen **privaten iCal-Link** (`.ics`-Abonnement) an, den Kalenderprogramme wie Google Kalender oder Outlook abonnieren können
und der sich bei Änderungen am Stundenplan selbst aktualisiert. Die Dokumentation sagt außerdem, dass diese Funktion für Schüler
von der Schule ausdrücklich bestellt sein muss.

**Was liefert er?** Nach der Dokumentation den **Stundenplan** der eigenen Person (die Funktion gibt es nur für Rollen, die einen
Stundenplan haben). **Hausaufgaben und Prüfungen/Schularbeiten werden dort nicht als Bestandteil genannt.** Ob ausgefallene
Stunden im Export ausdrücklich als „abgesagt“ gekennzeichnet sind oder einfach fehlen, steht dort ebenfalls nicht.

**Real geprüft?** Nein. Dafür müsste man mit dem Schülerkonto eingeloggt den Export einschalten und den Link lesen. Der Export
wurde auf dieser Schulinstanz weder abgerufen noch aktiviert; alle Angaben stammen aus der Herstellerdokumentation, nicht aus einer
Probe.

**Warum wird er nicht benutzt?**
- Er deckt die geforderten Kategorien nicht vollständig ab: Hausaufgaben und Prüfungen sind laut Dokumentation nicht enthalten, und
  ob ein Entfall erkennbar ist (und nicht mit einer Supplierung verwechselt wird), ist nicht belegt. Das Programm darf aus „Stunde
  taucht nicht mehr auf“ keine Absage ableiten.
- Für die Einträge des Exports sind keine stabilen Kennungen belegt, die eine Terminverschiebung von einer neuen Identität unterscheiden würden (die Anforderungen verlangen stabile Quellkennungen).
- Der Link ist ein Geheimnis in der URL und entsteht erst, wenn man im WebUntis-Profil **etwas einschaltet**. Das wäre eine Änderung auf
  der Schulplattform (Anforderung: ausschließlich lesen) und kann die Schule für Schüler gesperrt haben.
- Für Hausaufgaben und Prüfungen wären ohnehin die Wege in Abschnitt 4 nötig; ein zusätzlicher Export brächte also keine Daten, die
  nicht ohnehin über JSON-RPC und REST kommen.

Das Programm schaltet den Export nie ein und liest ihn nie. Fehlen auf deinem PC beide Wege für Unterrichtsentfälle (JSON-RPC und
REST-Stundenplan), wäre der Export höchstens ein Notbehelf für den Stundenplan und müsste zuerst gegen dein Konto geprüft werden.

## 10. Quellenverzeichnis

- python-webuntis: https://github.com/python-webuntis/python-webuntis (Dateien `webuntis/session.py`,
  `webuntis/utils/remote.py`, `webuntis/errors.py`, `webuntis/objects.py`)
- SchoolUtils/WebUntis: https://github.com/SchoolUtils/WebUntis (`src/base.ts`, `src/types.ts`)
- stundenglas: https://github.com/137-Trimethylxanthin/stundenglas (`SCOUT.md`, `core/src/untis.rs`)
- Untis-Hilfe zum iCal-Kalender-Abonnement (Abschnitt 9): https://webhelp.untis.at/HTML/WebHelp/uk/untis/wu_ical_kalender_abonnement.htm
- Reale Probe: siehe Abschnitt 2 (4. Oktober 2026, ohne Anmeldung)
