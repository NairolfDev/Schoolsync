# Datenzugriff Eduvidual – ehrlicher Bericht

Stand: 2026-10-04. Modul: `src/schulsync/adapters/eduvidual.py`.
Eduvidual ist eine Moodle-Installation (https://www.eduvidual.at). Das Programm **liest nur**; es gibt keine
Abgaben ab, hakt nichts ab und ändert nichts in Eduvidual.

## 1. Kurzfassung

- **Nachgewiesen** (echte Abfragen ohne Anmeldung, 2026-10-04): Die Moodle-Schnittstellen, die der Adapter braucht,
  sind auf dem Server vorhanden und aktiv (Token-Endpunkt, REST-Protokoll, Webservices und Mobile-Dienst eingeschaltet,
  Anmeldeformular ohne Captcha).
- **NICHT nachgewiesen** (dafür braucht man ein Konto): ob die Anmeldung mit Benutzername/Passwort wirklich ein Token liefert,
  ob die benötigten Funktionen für dieses Token freigeschaltet sind, und wie die echten Antworten aussehen. Es gab
  während der Entwicklung keine Zugangsdaten, und es wurde nie ein Anmeldeversuch unternommen.
- **Alle Tests laufen gegen synthetische Testdaten** (`tests/fixtures/eduvidual/`, nicht aufgezeichnet), die nach den
  dokumentierten Moodle-Antwortstrukturen nachgebaut sind. Sie beweisen die Regeln des Programms, nicht das Verhalten des
  echten Servers.
- **Die echte Prüfung ist `schulsync check`** auf deinem PC (Abschnitt 6). Erst wenn alle Schritte dort grün sind, ist
  die Eduvidual-Anbindung real bestätigt.

## 2. Was real geprüft wurde (ohne Anmeldung)

| Datum | Abfrage | Ergebnis |
|---|---|---|
| 2026-10-04 | `POST /lib/ajax/service-nologin.php` mit `tool_mobile_get_public_config` | `enablewebservices=1`, `enablemobilewebservice=1`, `typeoflogin=2` (Browser-Login der App), `showloginform=1`, `tool_mfa_enabled=false`, `enableloginrecaptcha=false`, `identityproviders` = Bildungsportal (Shibboleth), `authloginviaemail=1` |
| 2026-10-04 | `GET /lib/ajax/service-nologin.php?args=[…]` (dieselbe Funktion, per GET) | Funktioniert ebenfalls; der Adapter nutzt GET für die Erreichbarkeitsprüfung. Die Antwort enthält **kein** Feld `release`/`version`: die Moodle-Version von Eduvidual ist unbekannt. |
| 2026-10-04 | `GET /login/token.php?service=moodle_mobile_app` ohne Benutzername | JSON-Objekt mit `error`, `errorcode` (`missingparam`), `stacktrace`, `debuginfo`, `reproductionlink` (die drei letzten `null`) – Endpunkt existiert und ist aktiv; so sehen Token-Fehler aus. |
| 2026-10-04 | `GET /webservice/rest/server.php?wsfunction=tool_mobile_get_public_config&moodlewsrestformat=json` | Fehlercode `invalidtoken`: REST-Protokoll aktiv, Token nötig. |
| 2026-10-04 | `GET /login/index.php` | Formular mit `username`, `password`, `logintoken`, `anchor`; kein Captcha. |

Hinweis `typeoflogin=2`: Das betrifft die Anmeldung der Moodle-App (Weiterleitung an den Browser). Der Token-Endpunkt
`login/token.php` prüft diese Einstellung im Quellcode nicht; ob Eduvidual ihn trotzdem für Benutzername/Passwort
freigibt, zeigt erst `schulsync check`.

## 3. Was angenommen wird (mit Quellen)

Quelle ist jeweils der Moodle-Quellcode (GitHub `moodle/moodle`, Zweige `MOODLE_405_STABLE` und `MOODLE_500_STABLE`,
am 2026-10-04 gelesen). Welche Version Eduvidual betreibt, ist nicht bekannt; die verwendeten Funktionen sind in beiden
Zweigen vorhanden.

| Annahme | Beleg |
|---|---|
| `POST login/token.php` (`username`, `password`, `service=moodle_mobile_app`) liefert `{"token":…, "privatetoken":…}`; Fehler als JSON-Objekt mit `error` + `errorcode` (`invalidlogin`, `usernotconfirmed`, `passwordisexpired`, `sitemaintenance`, `servicenotavailable` = Dienst abgeschaltet, `enablewsdescription` = Webservices aus) | `login/token.php` |
| Der Server legt beim Token-Abruf serverseitig ein Token an bzw. verwendet ein vorhandenes und protokolliert den Abruf | `login/token.php` (`generate_token_for_current_user`, `log_token_request`) |
| REST: `webservice/rest/server.php` mit `wstoken`, `wsfunction`, `moodlewsrestformat=json`; Arrays als `courseids[0]=…`; Fehler als `{"exception","errorcode","message"}` | `webservice/rest/locallib.php` (`parse_request`, `generate_error`) |
| `core_webservice_get_site_info` liefert `functions[].name` (freigegebene Funktionen), `release`, `version` | `webservice/externallib.php` |
| `core_course_get_enrolled_courses_by_timeline_classification` (`classification=inprogress`, `limit`, `offset`) liefert `courses[]` (`id`, `fullname`, `shortname`) und `nextoffset` (= `offset` + verarbeitete Kurse); Mobile-Dienst **und** ajax-fähig | `course/externallib.php`, `lib/db/services.php` |
| Die Klassifikation `inprogress` blendet vom Nutzer ausgeblendete Kurse und beendete/noch nicht begonnene Kurse aus | `course/externallib.php` (`get_hidden_courses_on_timeline`, `course_filter_courses_by_timeline_classification`) |
| `mod_assign_get_assignments(courseids)` liefert je Kurs `assignments[]` mit `id`, `cmid`, `name` (vollständig), `duedate` (Unix-Sekunden, **0 = keine Frist**), `intro` (HTML; nur wenn die Einführung angezeigt werden darf), `allowsubmissionsfromdate`, `cutoffdate`; Warnungen (`item` = `course`/`module`) bei nicht lesbaren Kursen/Aktivitäten; **nicht** auf die Zeitleiste beschränkt, bereits abgegebene Aufgaben sind enthalten; `duedate` berücksichtigt persönliche Fristverlängerungen des angemeldeten Nutzers (`update_effective_access`); Mobile-Dienst, **nicht** ajax-fähig | `mod/assign/externallib.php`, `mod/assign/db/services.php` |
| Web-Sitzungs-Weg: `POST login/index.php` (`logintoken`, `username`, `password`, `anchor`) → Cookie `MoodleSession`; die Seite enthält `M.cfg = {…}` mit `sesskey`, `userId`, `siteId`, `courseId` | `login/index.php`, `lib/classes/output/requirements/page_requirements_manager.php` |
| `POST lib/ajax/service.php?sesskey=…&info=<funktion>` mit JSON `[{"index":0,"methodname":…,"args":{…}}]` antwortet mit `[{"error":false,"data":…}]` bzw. `[{"error":true,"exception":{"errorcode":…}}]`; nur Funktionen mit `ajax => true`, für nicht angemeldete Nutzer `servicerequireslogin` | `lib/ajax/service.php`, `lib/external/classes/external_api.php` |
| `core_calendar_get_calendar_monthly_view` (ajax) liefert `weeks[].days[].events[]`; Aufgabenfristen sind Ereignisse `modulename=assign`, `eventtype=due`, `instance` = **cmid**, `activityname` = vollständiger Name, `timestart` = Frist, `description` = Einführung | `calendar/externallib.php`, `calendar/classes/external/event_exporter_base.php`, `mod/assign/lib.php` |
| Persönliche/Gruppen-Ausnahmen erzeugen zusätzliche `due`-Ereignisse; Ereignisse mit `userid` haben `courseid` 0 und daher **kein** `course`-Feld | `mod/assign/lib.php` (`assign_update_events`), `event_exporter_base.php` |

Nicht aus dem Quellcode belegbar (und deshalb bewusst vorsichtig behandelt): wie Eduvidual (eigenes Plugin
`local_eduvidual`, Bildungsportal-Anmeldung) einzelne dieser Funktionen einschränkt oder erweitert.

## 4. Zugangswege und Reihenfolge

Einstellung `method` in `config.toml`: `auto` (Standard), `webservice`, `web`.

**Weg 1 – Web-Service-Token (bevorzugt).** Anmeldung über `login/token.php`, Abfragen über das REST-Protokoll.
Warum bevorzugt: Die Funktion `mod_assign_get_assignments` liefert alle Aufgaben mit stabiler `cmid`, vollständigem
Namen, Einführung und der für dich gültigen Frist – auch Aufgaben ohne Frist (die dann als „keine Frist“ gemeldet
werden) und bereits abgegebene. Das Passwort steht nur im Anfragekörper, nie in der URL. Kein HTML wird ausgewertet.

**Weg 2 – Weboberfläche (Ersatz).** Anmeldung wie im Browser (`login/index.php` mit `logintoken`), dann
`lib/ajax/service.php`. `mod_assign_get_assignments` ist dort nicht erlaubt (nicht ajax-fähig); stattdessen werden die
Fristen aus dem Kalender gelesen (`core_calendar_get_calendar_monthly_view`, Ereignisse `assign`/`due`). Es wird
keine Detailseite geöffnet: Das Aufrufen von `mod/assign/view.php` würde in Moodle die Aktivität als „angesehen“
markieren (Aktivitätsabschluss) und damit eine Änderung auslösen.

**`auto`-Regel:**
1. Weg 1 versuchen. Lehnt Eduvidual **Benutzername/Passwort ab** (`invalidlogin` u. ä.): sofort **Anmeldefehler**, der
   Lauf endet für diese Quelle, es gibt **keinen** zweiten Versuch über Weg 2 (Schutz vor Kontosperre).
2. Ist Weg 1 **nicht verfügbar** (Dienst abgeschaltet, Endpunkt 404/403, Funktionen nicht freigegeben, REST liefert
   trotz Token keine Daten): Weg 2 mit **einer** Anmeldung. Im Bericht erscheint der Hinweis „Zugangsart nicht nutzbar“.
3. Netzwerkfehler und 5xx/429 sind vorübergehend; der Tageslauf wiederholt begrenzt, es gibt keinen Wechsel des Wegs.

## 5. Was gelesen wird und wie

- **Kurse:** nur *aktuelle* (`inprogress`), seitenweise (50 pro Seite) bis zum Ende; `courses` in `config.toml` (Kurs-ID oder
  exakter Kursname) wählt aus. Eine abgebrochene oder nicht fortschreitende Seitenfolge ergibt `INCOMPLETE`, nie „leer“.
- **Aufgaben:** je Eintrag `source_id = assign:<cmid>`, Titel = vollständiger Aufgabenname, Beschreibung = Einführung als
  Klartext (deterministische HTML-Umwandlung: Text, Zeilenumbrüche, Listen, http(s)-Links als Text; Bilder, Skripte
  und Anhänge werden nicht ausgewertet; Links mit geheimnisähnlichen Parametern werden nicht übernommen), Kurs =
  vollständiger Kursname, Link = `https://<host>/mod/assign/view.php?id=<cmid>`, Frist = Unix-Zeit → Europe/Vienna.
- **Welche Aufgaben geliefert werden:** Frist im Prüffenster **oder** bekannter (verfolgter) Eintrag, auch wenn seine
  Frist nun außerhalb liegt (so werden Verschiebungen erkannt). Alles andere wird stillschweigend ignoriert.
- **Keine Frist** (`duedate = 0`): kein Eintrag, Hinweis `no_date` (Warnung) mit Titel.
- **Abgabestatus** wird nie gelesen und nie zur „Erledigung“ herangezogen.
- **Fehlende Angaben** (Titel, Kursname, Beschreibung nicht geliefert): Eintrag trotzdem liefern, Hinweis in `notes` plus
  Meldung `incomplete_data`; nichts wird erfunden.
- **Absagen:** Moodle meldet für Aufgaben keine ausdrückliche Absage. Der Adapter setzt daher nie `withdrawn` oder
  `CANCELLED`; ein verschwundener Eintrag erzeugt nur den Hinweis `tracked_not_found`.
- **Schreibschutz:** Gesendet werden nur GET sowie die Anmelde-POSTs (`login/token.php`, `login/index.php`) und POSTs
  von Lesefunktionen aus einer festen Liste (`core_webservice_get_site_info`,
  `core_course_get_enrolled_courses_by_timeline_classification`, `mod_assign_get_assignments`,
  `core_calendar_get_calendar_monthly_view`); alles andere wird im Code verweigert. Nebenwirkungen auf dem Server, die
  sich nicht vermeiden lassen: Beim Anmelden entsteht ein Token bzw. eine Sitzung, und Moodle protokolliert den Zugriff
  und aktualisiert „zuletzt aktiv“.

## 6. Was du prüfen musst

1. `schulsync check` ausführen. Erwartung: „Eduvidual erreichbar“, „Anmeldung“ (mit Angabe des Zugangs und der Moodle-Version),
   „Kurse lesen“ (Anzahl plausibel?), „Aufgaben lesen“ (Anzahl plausibel?).
2. Stimmen Kurs- und Aufgabenzahl mit dem überein, was du im Browser unter Eduvidual siehst?
3. Funktioniert die Anmeldung mit Benutzername/Passwort überhaupt? Eduvidual bietet auch die Anmeldung über das
   Bildungsportal (Shibboleth) an. Hast du nur diesen Weg und kein eigenes Eduvidual-Passwort, schlägt die Anmeldung
   mit Benutzername/Passwort fehl; dann ist ein Browser-Adapter der nächste Schritt (siehe unten). Dein Konto kann zudem
   eine Zwei-Faktor-Anmeldung haben; auf Serverebene ist sie laut öffentlicher Konfiguration aus (`tool_mfa_enabled=false`),
   für dein Konto ist das nicht geprüft.
4. Nach einem Fehlversuch nicht wiederholt neu starten: Der Adapter versucht eine falsche Anmeldung bewusst nur einmal pro
   Lauf, damit das Konto nicht gesperrt wird.
5. Liefert `check` über den Weg „Weboberfläche“, merke dir die unten genannten Einschränkungen.

## 7. Was NICHT möglich ist

- **Aufgaben ohne Frist über den Weg „Weboberfläche“:** Der Kalender kennt nur Aufgaben mit Frist; der Hinweis `no_date`
  entfällt dort. Das wird nicht verschwiegen: Jeder Lauf nennt es (Hinweis `no_date_unseen` im Bericht und im Kategorie-Detail),
  und `check` weist ebenfalls darauf hin.
- **Kurse, die du in Eduvidual aus der Ansicht entfernt hast, sowie beendete oder noch nicht begonnene Kurse:** Sie zählen
  nicht als „aktuell“ (`inprogress`) und werden nicht gelesen.
- **Andere Aktivitätstypen** (Test/Quiz, Forum, Workshop …): nur `assign` ist umgesetzt; weitere Einträge in `modules`
  werden mit dem Hinweis `unsupported_module` gemeldet, nicht stillschweigend verworfen.
- **Persönliche Fristverlängerungen über den Weg „Weboberfläche“:** Es gibt zusätzliche Ereignisse für Ausnahmen; der Adapter
  nimmt die persönliche bzw. früheste Frist und weist per Warnung darauf hin. Ist der Kurs eines solchen Ereignisses
  nicht erkennbar, wird der Eintrag ohne Kursnamen geliefert (bei konfigurierter Kursauswahl übersprungen und gemeldet).
  Der Weg „Token“ ist hier genauer, weil er die für dich gültige Frist direkt liefert.
- **Fristen sehr weit in der Zukunft** (Weg „Weboberfläche“): Es werden höchstens 24 Kalendermonate gelesen. Solange bekannte offene
  Aufgaben verfolgt werden, liest dieser Weg immer die vollen 24 Monate (bis zu 24 Kalenderabrufe je Lauf), weil er eine verschobene Frist
  nur dort findet, wo das neue Datum liegt (es gibt keine Abfrage je Aufgabe). Ohne verfolgte Aufgaben genügen das Prüffenster plus zwei
  Monate. Liegt eine verfolgte Frist selbst außerhalb der 24 Monate, meldet der Lauf die Kategorie als unvollständig.
- **Anhänge, PDFs, Bilder:** werden nicht interpretiert.
- **Absagen erkennen:** nicht möglich (siehe Abschnitt 5).
- **Aufgaben, die nur der Browser darstellt:** Falls Eduvidual die Funktionen für den Mobile-Dienst sperrt **und** die
  Weboberfläche die Anmeldung per Formular verweigert (z. B. nur Bildungsportal), reicht reines HTTP nicht; dann braucht es
  einen Browser-Adapter (z. B. Playwright). Das ist nicht eingebaut und wäre ein eigener Schritt.

## 8. Kalenderexport (iCal) – bewertet, nicht verwendet

Moodle bietet unter `calendar/export.php` eine Export-Adresse mit `authtoken` (`calendar/export_execute.php`).
Was sie liefert (Quellcode `calendar/export_execute.php`): je Ereignis `UID` = `<Ereignis-ID>@<Host>`, `SUMMARY` = Ereignisname
(bei Fristen mit Zusatz „… ist fällig“), `DESCRIPTION` als Klartext, `DTSTART`/`DTEND`, `CATEGORIES` = Kurs-**Kurzname**.
Zeitbereiche: `recentupcoming` = 5 Tage zurück bis 60 Tage voraus, `custom` = Servereinstellung (Standard 5 Tage zurück,
365 voraus).

Was fehlt: die **stabile Aktivitätskennung (cmid)**, der Aktivitätstyp, der Link zur Aufgabe, der vollständige Kursname und
der saubere Titel. Die UID ist die Ereignis-ID und ändert sich, wenn Moodle die Ereignisse einer Aufgabe neu erzeugt.
Außerdem steckt das Geheimnis in der URL (darf nie geloggt werden) und muss einmalig im Browser erzeugt werden. Der Export
liefert also nicht alles, was die Anforderungen verlangen (Abschnitt 2/3: Quellkennung, vollständiger Titel); er wird daher
**nicht** als Datenquelle benutzt.

## 9. Nächster Schritt, falls `check` scheitert

Zeigt `check`, dass weder Weg 1 noch Weg 2 funktioniert (z. B. nur Bildungsportal-Anmeldung), ist ein Browser-Adapter mit
deterministischer Automation (Playwright, ohne KI) der dokumentierte nächste Schritt. Bis dahin meldet das Programm den
Fehler im HTML-Bericht und markiert die Quelle als nicht erfolgreich – es tut nie so, als gäbe es keine Aufgaben.
