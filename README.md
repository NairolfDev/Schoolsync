# SchulSync

SchulSync liest deine Schulplattformen **Eduvidual** (Moodle) und **WebUntis** nach festen Regeln aus und legt
Abgabefristen, Hausaufgaben, Prüfungen und Unterrichtsentfälle in deinem **Google-Kalender „Schule“** und in der
**Google-Tasks-Liste „Schule“** an.

Voreingestellt ist WebUntis der **HTBLA Grieskirchen**; für eine andere Schule änderst du in der Konfiguration nur `base_url` und `school`
(Abschnitt 6).

> **Wichtig vorab:** SchulSync ist ein privates Schülerprojekt ohne Verbindung zu Eduvidual, Untis oder Google. Es ist mit rund 2000
> automatischen Tests gegen Testdaten abgesichert. Ob der Datenabruf **mit deinem Konto** klappt, zeigt `schulsync check` (Abschnitt 7).
> Lies bitte auch Abschnitt 15 („Ehrliche Einschränkungen“).
>
> **Lizenz:** GNU GPL v3 oder neuer (Copyleft), siehe Abschnitt 17 und `LICENSE`.

## Inhalt

1. [Was SchulSync tut (und was nicht)](#1-was-schulsync-tut-und-was-nicht)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Installation](#3-installation)
4. [Google einrichten (Cloud-Projekt, APIs, OAuth-Client)](#4-google-einrichten-cloud-projekt-apis-oauth-client)
5. [Kalender „Schule“ vorab anlegen](#5-kalender-schule-vorab-anlegen)
6. [Einrichten: `schulsync setup`](#6-einrichten-schulsync-setup)
7. [Zuerst prüfen: `schulsync check`](#7-zuerst-prüfen-schulsync-check)
8. [Vorschau: `schulsync preview`](#8-vorschau-schulsync-preview)
9. [Manueller Start: `schulsync run`](#9-manueller-start-schulsync-run)
10. [Autostart nach der Windows-Anmeldung](#10-autostart-nach-der-windows-anmeldung)
11. [Täglicher Ablauf und Regeln](#11-täglicher-ablauf-und-regeln)
12. [Fehlerbericht, Protokolle, Daten](#12-fehlerbericht-protokolle-daten)
13. [Datenschutz und Sicherheit](#13-datenschutz-und-sicherheit)
14. [Fehlerbehebung](#14-fehlerbehebung)
15. [Ehrliche Einschränkungen](#15-ehrliche-einschränkungen)
16. [Entwicklung](#16-entwicklung)
17. [Lizenz (Copyleft)](#17-lizenz-copyleft)

## 1. Was SchulSync tut (und was nicht)

**Es tut:**

- Nach jeder Windows-Anmeldung und täglich um **6:00 und 18:00 Uhr** liest es Eduvidual (Aufgaben und Abgaben) und WebUntis (Hausaufgaben, Prüfungen, Schularbeiten,
  Unterrichtsentfälle).
- Es trägt Fristen und Termine in den Google-Kalender „Schule“ ein und legt abhakbare Aufgaben in der Google-Tasks-Liste „Schule“ an.
- Verschobene Fristen und Tests aktualisiert es (derselbe Termin, kein Duplikat), ein zurückgenommener Entfall wird als „Abgesagt:“ markiert.
- **Abgegeben in Eduvidual = abgehakt in Google Tasks:** Eine in Eduvidual eingereichte Abgabe hakt die Aufgabe „Bearbeiten: …“ automatisch ab.
  Gibt die Lehrkraft sie zur Überarbeitung zurück, entsteht eine **zweite** Aufgabe; die erste bleibt erledigt.
- Es läuft ohne Server und ohne Dauerbetrieb: kurze Läufe über die Windows-Aufgabenplanung, kostenlos auf einem einzelnen Windows-PC.
- Bei Problemen öffnet sich ein HTML-Bericht in deinem Browser, und Windows zeigt zusätzlich eine kurze **Benachrichtigung**
  („SchulSync: Problem beim Abgleich“); beides höchstens einmal pro Problem und Tag. Bei Erfolg öffnet sich nichts.
- `schulsync status` zeigt ohne Internet, was in den **nächsten 7 Tagen** ansteht und was neu erfasst wurde.

**Es tut nicht:**

- **Keine KI.** Es gibt keine KI, keine Sprachmodelle, keine Bild- oder PDF-Auswertung. Texte werden nur kopiert, alle
  Entscheidungen folgen festen Regeln.
- **Schulplattformen nur lesen.** Es gibt nie eine Abgabe ab, hakt nichts ab und ändert nichts in Eduvidual oder WebUntis.
  (Unvermeidbar: Beim Anmelden entsteht dort eine Sitzung, und die Plattform vermerkt „zuletzt aktiv“.)
- **Kein Zusammenführen über Quellen hinweg.** Gleich klingende Einträge aus Eduvidual und WebUntis bleiben getrennte Einträge.
- **Sonst hakst du selbst ab.** WebUntis-Hausaufgaben und Lernaufgaben für Prüfungen werden nie automatisch erledigt.
- **Kein Ausweichen.** Ist der Kalender „Schule“ oder die Aufgabenliste „Schule“ nicht erreichbar, schreibt SchulSync nicht
  ersatzweise in den Hauptkalender oder in eine andere Liste.

## 2. Voraussetzungen

- **Windows 11.** Dort wurde SchulSync entwickelt und getestet. Ein normales Benutzerkonto genügt, **Administratorrechte sind nicht nötig**.
- **Python 3.11 oder neuer**, empfohlen **3.13** (damit wurden alle Tests ausgeführt). Download:
  https://www.python.org/downloads/ . Beim Installieren „Add python.exe to PATH“ ankreuzen.
- Ein **Google-Konto** (z. B. Gmail).
- Dein Eduvidual- und dein WebUntis-Zugang mit Benutzername und Passwort. Eine Anmeldung nur über das Bildungsportal,
  Microsoft-Single-Sign-on oder Zwei-Faktor wird nicht unterstützt (Abschnitt 15).
- Internet für die Einrichtung und für die Läufe.

## 3. Installation

### Variante A (empfohlen): fertige `SchulSync-Setup.exe`

Du brauchst weder Python noch Git.

1. Lade auf der Seite **Releases** dieses Repositories die Datei **`SchulSync-Setup.exe`** der neuesten Version herunter.
2. **Doppelklick.** Windows zeigt eventuell „Der Computer wurde durch Windows geschützt“ (die Datei ist nicht signiert): **Weitere Informationen**,
   dann **Trotzdem ausführen**. Manche Virenscanner melden bei Programmen dieser Art Fehlalarme; den Quellcode kannst du hier selbst prüfen und die Datei selbst bauen
   (Abschnitt 16).
3. Das Fenster (es führt `schulsync install` aus, das du auch selbst eingeben kannst) installiert SchulSync nach `%LOCALAPPDATA%\Programs\SchulSync`, trägt den Ordner in deinen PATH ein (ohne Adminrechte) und fragt, ob
   du gleich einrichten willst (Abschnitt 6). Danach in einem **neuen** PowerShell-Fenster: `schulsync --help`.
4. Die Google-Client-Datei (Abschnitt 4.4) legst du in `%LOCALAPPDATA%\Programs\SchulSync` (neben die `schulsync.exe`); `schulsync setup --google` findet sie dort.

**Aktualisieren:**

```powershell
schulsync update
```

Der Befehl fragt GitHub nach der neuesten Version, lädt sie herunter, prüft die SHA-256-Prüfsumme und ersetzt die `schulsync.exe`. Einstellungen,
Passwörter und Daten bleiben erhalten; die neue Version gilt ab dem nächsten Start (auch für den Autostart, der denselben Pfad nutzt). Nur nachsehen,
ohne zu installieren: `schulsync update --check` (Exit-Code 10 = neue Version vorhanden). Das funktioniert nur, solange das Repository öffentlich ist und
eine Release enthält.

**Deinstallieren:**

```powershell
schulsync uninstall
```

Der Befehl entfernt den Autostart, den PATH-Eintrag und den Programmordner (der Ordner wird gelöscht, sobald sich das Fenster schließt).
Deine Einstellungen, Passwörter und Daten in `%LOCALAPPDATA%\SchulSync` bleiben erhalten. Sie mitzulöschen: `schulsync uninstall --data` (mit Rückfrage).

Beim Start über den Autostart kann das Konsolenfenster für einen Augenblick aufblitzen; es schließt sich sofort wieder.

### Variante B: aus dem Quellcode

Öffne PowerShell im SchulSync-Ordner (dem Ordner, in dem diese README liegt) und führe aus:

```powershell
powershell -File scripts\install.ps1
```

Das Skript sucht Python 3.11+, legt die Umgebung `.venv` an und führt `pip install -e .` aus. Es braucht keine
Administratorrechte, richtet keinen Autostart ein und fragt keine Passwörter ab. Du kannst es gefahrlos mehrmals ausführen.

**Meldet Windows, dass Skripte nicht ausgeführt werden dürfen** („Ausführung von Skripts ist auf diesem System deaktiviert“), starte es
einmalig so. Das gilt nur für diesen einen Aufruf und ändert keine Windows-Einstellung:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

**Ohne das Skript** (gleiches Ergebnis, von Hand):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

Danach heißt der Befehl `.\.venv\Scripts\schulsync`. In dieser README steht der Einfachheit halber nur `schulsync`. Entweder tippst du
den ganzen Pfad, oder du aktivierst die Umgebung einmal pro PowerShell-Fenster mit `.\.venv\Scripts\Activate.ps1`. Gleichwertig ist
`.\.venv\Scripts\python -m schulsync <Befehl>`.

**Tipp: `schulsync` in jedem Ordner nutzen.** Trage den Ordner `.venv\Scripts` einmal in deinen Benutzer-PATH ein (ohne Adminrechte;
danach ein **neues** PowerShell-Fenster öffnen). Im SchulSync-Ordner ausführen:

```powershell
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path","User") + ";$PWD\.venv\Scripts", "User")
```

Prüfen, ob es klappt:

```powershell
schulsync --version
schulsync --help
```

## 4. Google einrichten (Cloud-Projekt, APIs, OAuth-Client)

SchulSync spricht direkt mit der **Google Calendar API** und der **Google Tasks API**. Dafür brauchst du ein **eigenes, kostenloses Projekt**
in der Google Cloud Console (Rechnungsdaten sind nicht nötig). Die Namen der Menüpunkte ändert Google gelegentlich; die Schritte
bleiben sinngemäß gleich. Weicht etwas bei dir ab, suche den ähnlich benannten Eintrag.

### 4.1 Projekt anlegen

1. Öffne https://console.cloud.google.com/ und melde dich mit dem Google-Konto an, dem der Kalender „Schule“ gehört.
2. Oben auf die Projektauswahl klicken, **Neues Projekt**, Name z. B. `SchulSync`, **Erstellen**, danach das Projekt auswählen.

### 4.2 Calendar API und Tasks API aktivieren

Beide Links aktivieren die jeweilige API für das ausgewählte Projekt (dort **Aktivieren** klicken):

- Calendar API: https://console.cloud.google.com/flows/enableapi?apiid=calendar-json.googleapis.com
- Tasks API: https://console.cloud.google.com/flows/enableapi?apiid=tasks.googleapis.com

Alternativ: **APIs & Dienste**, **Bibliothek**, nach „Google Calendar API“ bzw. „Google Tasks API“ suchen und aktivieren.

### 4.3 „Google Auth Platform“ einrichten

Menü (☰) **Google Auth Platform** (bei älteren Oberflächen: „APIs & Dienste“, „OAuth-Zustimmungsbildschirm“). Beim ersten Mal **Erste Schritte**.

1. **Branding:** App-Name z. B. `SchulSync`, deine E-Mail-Adresse als **Support-E-Mail** und als Kontakt.
2. **Zielgruppe** (Audience): **Extern**. Dein privates Gmail-Konto kann keine „Intern“-App anlegen.
3. **Datenzugriff** (Data Access, **Bereiche hinzufügen oder entfernen**): Wähle **nur diese drei** Bereiche:
   - `.../auth/calendar.events` (Termine lesen und schreiben, ohne Zugriff auf Kalendereinstellungen),
   - `.../auth/calendar.calendarlist.readonly` (nur, um den Kalender „Schule“ in deiner Kalenderliste zu finden),
   - `.../auth/tasks` (Aufgaben lesen und schreiben).

   Mehr verlangt SchulSync nicht. Findest du sie nicht in der Liste, trage sie unten bei „Bereiche manuell hinzufügen“ ein.
4. **Testnutzer:** Unter **Zielgruppe** → **Testnutzer** deine eigene Google-Adresse hinzufügen. **Ohne diesen Schritt lässt Google die Anmeldung nicht zu.**

### 4.4 OAuth-Client „Desktop-App“ anlegen und JSON herunterladen

1. **Google Auth Platform**, **Clients** (bei älteren Oberflächen: „APIs & Dienste“, „Anmeldedaten“, „Anmeldedaten erstellen“, „OAuth-Client-ID“).
2. **Client erstellen**, Anwendungstyp **Desktop-App**, Name beliebig (z. B. `SchulSync PC`).
3. **JSON herunterladen** (Dateiname etwa `client_secret_....json`). Das ist deine „OAuth-Client-Datei“.
4. **Lege die Datei in den SchulSync-Ordner** (Quellcode: dorthin, wo diese README liegt; fertige .exe: `%LOCALAPPDATA%\Programs\SchulSync`). `schulsync setup --google` findet sie dort automatisch,
   du musst keinen Pfad eingeben. Liegt keine Datei dort, fragt der Assistent nach dem Pfad. Der Inhalt wird zusätzlich geschützt gespeichert
   (Abschnitt 13).
5. **Gib die Datei nie weiter und lade sie nirgends hoch.** `.gitignore` schließt `client_secret*.json` aus, damit sie nicht in Git landet.

### 4.5 Test-Modus und automatische Neuanmeldung

Deine App bleibt im Veröffentlichungsstatus **„Test“** (Testing). Für **„In Produktion“** verlangt Google eine Startseite und eine
Datenschutzerklärung im Internet; für eine private App ist das unnötig.

Im Test-Modus läuft die Google-Anmeldung laut Google nach **7 Tagen** ab
(https://developers.google.com/identity/protocols/oauth2#expiration). **SchulSync kümmert sich selbst darum:** Ist die Anmeldung
abgelaufen, **öffnet sich beim nächsten Lauf automatisch ein Browserfenster mit dem Google-Login**. Du meldest dich an, bestätigst die
Berechtigungen, und der Abgleich läuft direkt weiter. Meldest du dich nicht innerhalb von 10 Minuten an, wird der Lauf abgebrochen und im
Bericht gemeldet; beim nächsten Start kommt das Fenster wieder. Von Hand geht es jederzeit mit:

```powershell
schulsync setup --google
```

**Die Warnung „Google hat diese App nicht überprüft“** ist für deine eigene App normal: **Erweitert**, dann **Weiter zu SchulSync (unsicher)**,
danach **alle Kästchen** für Kalender und Aufgaben anhaken.

Eine dauerhafte Gültigkeit garantiert Google ohnehin nicht (Widerruf, sechs Monate ohne Nutzung u. a.). Bereits Gelesenes und noch nicht
Geschriebenes geht dabei nicht verloren, es wird nach der Anmeldung nachgeholt.

## 5. Kalender „Schule“ vorab anlegen

Der Kalender **wird nicht automatisch angelegt.** Lege ihn in Google Kalender (https://calendar.google.com) selbst an:
links bei „Weitere Kalender“ auf **+**, **Neuen Kalender erstellen**, Name genau **Schule**. Er muss ein **eigener** Kalender sein, nicht der
Hauptkalender, und dir gehören (oder du brauchst Bearbeitungsrechte).

Die **Aufgabenliste „Schule“** dagegen legt `schulsync setup` selbst an, falls es sie noch nicht gibt.

Gibt es den Kalender „Schule“ nicht oder mehrfach, bricht die Einrichtung mit einem deutschen Hinweis ab und speichert nichts. Dann Kalender
anlegen bzw. umbenennen und `schulsync setup` wiederholen.

## 6. Einrichten: `schulsync setup`

```powershell
schulsync setup
```

Der Assistent führt dich der Reihe nach durch alles. Eine leere Eingabe behält den vorhandenen Wert. Jeder Teil lässt sich einzeln
wiederholen:

```powershell
schulsync setup --google
schulsync setup --eduvidual
schulsync setup --webuntis
```

Ablauf:

1. **Datenordner und Konfiguration:** Er legt `%LOCALAPPDATA%\SchulSync` an und kopiert die Vorlage `config.example.toml` als `config.toml` dorthin.
2. **Google:**
   - Die OAuth-Client-Datei wird im SchulSync-Ordner gefunden (Abschnitt 4.4), sonst fragt der Assistent nach dem Pfad.
   - Der Browser öffnet sich für die Google-Anmeldung. Melde dich mit dem Konto an, dem der Kalender „Schule“ gehört, bestätige die
     Warnung (Abschnitt 4.5) und hake **alle** Kästchen an.
   - Steht in `config.toml` unter `[google]` bei `account` eine Adresse, prüft der Assistent, dass die Anmeldung zu genau diesem Konto
     gehört (Schutz bei mehreren Google-Konten; leer = keine Prüfung). Dann findet er den Kalender „Schule“ (**nie den Hauptkalender**),
     findet oder legt die Aufgabenliste „Schule“ an und schreibt deren Kennungen in `config.toml`. **Erst wenn alles stimmt, wird etwas
     gespeichert.** Bei jedem Abbruch bleibt der vorherige Zustand unverändert.
3. **Eduvidual und WebUntis:** Benutzername und Passwort (die Eingabe des Passworts bleibt unsichtbar; es wird geschützt gespeichert). Auf Wunsch
   wird die Anmeldung gleich getestet.
4. **Autostart:** Er fragt, ob der Autostart eingerichtet werden soll (Abschnitt 10). Du kannst „Nein“ sagen und das später nachholen. Mach
   zuerst `schulsync check`.

Läuft noch eine **alte Schulautomation** (Skript, Kalender-Plugin, Zapier o. Ä.), die in denselben Kalender oder dieselbe Aufgabenliste schreibt,
können doppelte Einträge entstehen. Schalte sie ab. Fremde Einträge lässt SchulSync unangetastet, die alte Automation erkennt es aber nicht.

Hast du **mehrere Google-Konten** im Browser, wähle bei der Anmeldung das richtige. Bei einem falschen Konto bricht der Assistent ab und speichert nichts.

## 7. Zuerst prüfen: `schulsync check`

> **Das ist der Test, der zählt.** Nur dieser Befehl sieht echte Konten. Führe ihn **vor** dem ersten Lauf und vor dem Autostart aus.

```powershell
schulsync check
```

`check` schreibt nichts bei Google, legt keine Datenbank an und ändert höchstens die Sperre einer Quelle (siehe unten). Er prüft der Reihe nach:

1. **Konfiguration** gültig, aktive Quellen.
2. **Gespeicherte Zugangsdaten** (nur Namen, nie Werte).
3. **Google:** Anmeldung gültig, Kalender „Schule“ erreichbar und beschreibbar (nicht der Hauptkalender), Aufgabenliste erreichbar.
4. **Je Schulplattform:** Erreichbarkeit, **Anmeldung**, und **je Datenkategorie, was wirklich gelesen werden kann**, mit der Anzahl der Einträge im Prüffenster.

So liest du die Ausgabe:

| Anzeige | Bedeutung |
|---|---|
| `[OK]` | Dieser Schritt hat funktioniert. |
| `[FEHLER]` | Dieser Schritt ist gescheitert. Darunter stehen Meldung, **Fehlercode**, **Tipp** und **Technische Details**. |
| Kategorie `OK (n Einträge …)` | Die Kategorie ließ sich vollständig lesen. **Vergleiche n mit dem, was du in Eduvidual bzw. WebUntis im Browser siehst.** Zeigt `check` 0, obwohl du Einträge siehst, ist ein Weg vermutlich leer oder falsch gefiltert. |
| Kategorie `UNVOLLSTÄNDIG` | Es kam nur ein Teil der Daten an (z. B. „Datum nicht erlaubt“, abgebrochene Seitenfolge). |
| Kategorie `FEHLGESCHLAGEN` | Die Kategorie ließ sich nicht lesen (z. B. fehlendes Recht, andere Antwortstruktur als erwartet). |
| Kategorie `ABGESCHALTET` | In `config.toml` ausgeschaltet; zählt nicht als fehlend. |

**Eduvidual: Zugang `auto`, `webservice` oder `web`.** Die Einstellung `method` in `config.toml` (Standard `auto`) bestimmt, wie SchulSync Eduvidual liest.
`check` nennt hinter „Zugang:“ den Weg, der bei dir wirklich benutzt wurde:

- **`webservice`** („Web-Service-Token (Moodle-App-Schnittstelle)“): bevorzugt. Liefert alle Aufgaben mit vollständigem Titel und deiner persönlichen Frist,
  auch Aufgaben ohne Frist (die dann als „ohne Datum“ gemeldet werden).
- **`web`** („Weboberfläche (Anmeldung wie im Browser)“): der Ersatzweg. Er liest die Fristen aus dem Eduvidual-Kalender. **Einschränkung:** Aufgaben
  **ohne Frist** sind dort unsichtbar. Der Bericht weist bei jedem Lauf darauf hin.
- **`auto`**: versucht zuerst `webservice` und nimmt `web` nur, wenn der erste Weg technisch nicht verfügbar ist. Lehnt Eduvidual
  **Benutzername oder Passwort ab**, bricht es sofort ab und probiert **nicht** noch einen zweiten Weg (Schutz vor Kontosperre). Ein Schritt „Zugangsart …
  nicht nutzbar, Ersatz verwendet“ ist dann normal.

**Was bei `[FEHLER]` oder `FEHLGESCHLAGEN` zu tun ist:**

1. Passwort falsch oder Konto gesperrt: `schulsync setup --eduvidual` bzw. `--webuntis`. Es gibt bewusst **nur einen Anmeldeversuch** pro Lauf.
2. **Google:** `schulsync setup --google`.
3. Kategorie „Kein Zugriff“: Die Schule hat deinem Schülerkonto dieses Recht nicht gegeben. Die anderen Kategorien laufen weiter. Das lässt sich nur bei der Schule ändern.
4. Anmeldung bei WebUntis `[OK]`, aber Hausaufgaben oder Prüfungen scheitern mit „Sitzung abgelaufen“ und dem Hinweis auf ein abweichendes Sitzungscookie: Das ist eine
   offene Annahme des Programms (siehe `docs/datenzugriff_webuntis.md`). Ändere nicht deine Zugangsdaten, sondern melde den Text (siehe unten).
5. „Unerwartete Antwortstruktur“: Die Plattform antwortet anders als dokumentiert. Das ist der beabsichtigte, erkennbare Fehler bei geänderten Seiten, kein stilles „leer“.
6. Meldet die Anmeldung, dass nur das Bildungsportal (Shibboleth) oder Zwei-Faktor möglich ist, kann SchulSync diese Quelle nicht lesen. Ein Browser-Adapter wäre der nächste
   Schritt, ist aber **nicht gebaut** (Abschnitt 15).

**Was dem Entwickler gemeldet werden sollte:** Kopiere bei jedem Fehler, der nicht einfach falsche Zugangsdaten sind, die **vollständigen Zeilen** unter `[FEHLER]` bzw. unter der
Kategorie (Meldung, **Fehlercode**, **Tipp**, **Technische Details**), den Zugang (`webservice`/`web`) und die Kategorie mit Status. Passwörter, Tokens und Cookies sind in dieser
Ausgabe bereits ausgeblendet. Melde außerdem, wenn eine Anzahl nicht zu deiner Browseransicht passt. Das sind echte Befunde über das reale System, keine Testfehler. Die Tests wurden nur mit
nachgebauten Antworten ausgeführt.

Exit-Code von `check`: 0 alles in Ordnung, 1 es gibt Probleme, 2 Konfigurationsfehler. Eine Quelle, deren Anmeldung wegen eines früheren Fehlers für den Autostart gesperrt war,
wird entsperrt, sobald `check` die Anmeldung erfolgreich bestätigt.

## 8. Vorschau: `schulsync preview`

```powershell
schulsync preview
```

Liest die Schulplattformen und zeigt als HTML-Seite (`vorschau.html`, öffnet sich im Browser), was in Google geschehen **würde**. Es wird **nichts bei
Google geschrieben**, **kein Erfolg verbucht** und **kein späterer Schreibvorgang unterdrückt**. Eine Vorschau ändert auch keine Sperre und ersetzt nie den Bericht
eines echten Laufs. Ist Google gerade nicht lesbar (z. B. Anmeldung abgelaufen), warnt die Vorschau, dass sie Google nicht vollständig lesen konnte.

## 9. Manueller Start: `schulsync run`

```powershell
schulsync run --force
```

Der manuelle Start gleicht sofort ab und kümmert sich nicht darum, ob heute schon ein Lauf erfolgreich war. Die Ausgabe nennt das Ergebnis; bei Problemen öffnet sich der Bericht.

| Befehl | Wirkung |
|---|---|
| `schulsync run` | Manueller Start. Wird von der Tagesbegrenzung **nie** gebremst. |
| `schulsync run --force` | Wie `run`; zusätzlich gilt `--force` auch für `run --auto` und hebt dort die Tagesbegrenzung auf. Die Empfehlung für den ersten Test. |
| `schulsync run --auto` | So startet der Autostart: höchstens ein erfolgreicher Lauf je Zeitfenster (00–06, 06–18, 18–24 Uhr), sonst endet er **still**. Bericht im Browser nur bei Problemen. |

Empfohlene Reihenfolge nach der Einrichtung: `schulsync check`, dann `schulsync preview`, dann `schulsync run --force`, danach Kalender und Aufgaben in Google ansehen
(Titel, Aufgabendatum, 24-Stunden-Erinnerung, Bereich „Persönliche Notizen“).

Weitere Befehle:

| Befehl | Wirkung |
|---|---|
| `schulsync status` | Lokaler Überblick **ohne Internet**: letzter Lauf, Stand je Quelle, gesperrte Quellen, ausstehende Schreibvorgänge, von dir gelöschte Einträge, Autostart-Zustand. |
| `schulsync restore` | Stellt von dir **bewusst gelöschte** Google-Einträge wieder her (Abschnitt 11). `schulsync restore <UID>` nur einzelne; die Kennungen zeigt `schulsync status`. |
| `schulsync report` | Öffnet den neuesten Bericht im Browser. |

Alle Befehle kennen `-v` (Protokollzeilen zusätzlich in der Konsole) und `--home ORDNER` (anderer Datenordner statt `%LOCALAPPDATA%\SchulSync`; alternativ die Umgebungsvariable `SCHULSYNC_HOME`).

## 10. Autostart nach der Windows-Anmeldung

### Einrichten, prüfen, entfernen

Führe den Befehl aus der installierten Umgebung aus (Abschnitt 3), in einem **normalen** PowerShell-Fenster (nicht als Administrator):

```powershell
schulsync autostart install
schulsync autostart status
schulsync autostart remove
```

`schulsync autostart install` legt in der **Windows-Aufgabenplanung** eine Aufgabe namens **SchulSync** an:

- **Anmeldetrigger** („Bei Anmeldung“) nur für **deinen** Benutzer, mit **etwa einer Minute Startverzögerung**,
- zusätzlich **täglich um 6:00 und 18:00 Uhr**; war der PC da aus oder im Ruhezustand, holt Windows den Lauf beim nächsten Einschalten nach,
- läuft **in deinem Benutzerkonto** in deiner angemeldeten Sitzung (damit sich bei Problemen ein Bericht in deinem Browser öffnen kann),
- **ohne erhöhte Rechte**, kein Passwort nötig,
- startet `pythonw.exe -m schulsync run --auto` aus der `.venv` (ohne Konsolenfenster),
- die Aufgabe wird nach einer Stunde von Windows beendet; ein Lauf dauert normalerweise nur Sekunden.

Es gibt **keine feste Uhrzeit**. Du siehst die Aufgabe in der Aufgabenplanung (Windows-Taste, „Aufgabenplanung“) in der Aufgabenplanungsbibliothek.
Verschiebst du den SchulSync-Ordner oder die `.venv`, führe `schulsync autostart install` erneut aus.

`schulsync autostart remove` entfernt **Aufgabe und Verknüpfung** (siehe unten). Das ist idempotent: Gibt es nichts, ist das kein Fehler.

**So prüfst du, dass es wirklich funktioniert** (das wurde nur mit Testdaten, nie auf einem echten Windows-Logon geprüft):

1. `schulsync autostart status` sollte „über die Windows-Aufgabenplanung eingerichtet“ melden.
2. Optional: `schtasks /Query /TN SchulSync /XML` zeigt die gespeicherte Aufgabe.
3. Melde dich bei Windows ab und wieder an. Etwa eine Minute später sollte in der Aufgabenplanung bei der Aufgabe „SchulSync“ das **Letzte Ergebnis 0** stehen,
   und `%LOCALAPPDATA%\SchulSync\logs\schulsync-JJJJ-MM-TT.log` enthält die Zeile „Befehl 'run' gestartet“.
4. Teste den Fehlerfall: Internet trennen, `schulsync run --auto --force`: der Bericht öffnet sich einmal.

### Ausweichlösung: Verknüpfung im Startup-Ordner

Erlaubt dein PC (z. B. ein verwaltetes Schulgerät) keine eigene Aufgabe in der Aufgabenplanung („Zugriff verweigert“), richtet `schulsync autostart install`
**automatisch** eine **Autostart-Verknüpfung** ein: `SchulSync.lnk` im Startup-Ordner deines Benutzers
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`, öffnen mit Windows-Taste + R und `shell:startup`). Die Ausgabe sagt, dass die Verknüpfung benutzt wurde.

Nachteil: Eine Verknüpfung kann den Start nicht verzögern. Das ist unkritisch, denn SchulSync wartet selbst bis zu zwei Minuten auf das Internet. Entfernen: `schulsync autostart remove`
(oder die Verknüpfung im Ordner `shell:startup` löschen).

Klappt auch das nicht, nennt der Befehl die Handarbeit: Windows-Taste + R, `shell:startup`, im geöffneten Ordner mit der rechten Maustaste **Neu**, **Verknüpfung**, auf
`pythonw.exe` aus der `.venv` zeigen und als Argumente `-m schulsync run --auto` eintragen.

Aufgabe und Verknüpfung werden nie zugleich benutzt, sonst startete SchulSync doppelt; `install` und `remove` räumen die jeweils andere Lösung auf.

## 11. Täglicher Ablauf und Regeln

**Ablauf:** Du meldest dich an, nach etwa einer Minute startet SchulSync. Es wartet bei Bedarf bis zu **zwei Minuten auf das Internet**, liest beide Quellen **unabhängig voneinander**, wiederholt
Fehler **wenige Male mit wachsender Wartezeit** (Standard: 4 Versuche, 15/60/180 Sekunden Pause) und schreibt die Änderungen nach Google.

- **Höchstens ein erfolgreicher automatischer Lauf je Zeitfenster** (00–06, 06–18 und 18–24 Uhr, Europe/Vienna, auch bei Sommer-/Winterzeit), **getrennt je Quelle**. Das gilt an **jedem** Tag
  einschließlich Montag und Wochenende. Jede weitere automatische Anmeldung im selben Zeitfenster endet **still**; die Läufe um 6 und 18 Uhr prüfen erneut auf Änderungen (verschobene Fristen und Tests, zurückgenommene Entfälle). Ein erfolgreicher manueller Lauf zählt ebenfalls.
- Ein **Teilfehler gilt nie als vollständiger Erfolg**: Eine fehlgeschlagene Quelle oder ein offener Google-Schreibvorgang wird beim nächsten Start nachgeholt, **ohne** bereits
  Erledigtes doppelt anzulegen.
- **Ohne Internet** nach der Anmeldung: Es wird gewartet und dann trotzdem versucht; was fehlt, kommt beim nächsten Start.
- **Nach mehreren Tagen Aus:** Der aktuelle Stand wird **einmal** geprüft. Es gibt keine Serie nachgeholter Tagesläufe.
- Eine **abgelehnte Anmeldung** wird **nicht endlos wiederholt**: Die Quelle wird für automatische Läufe gesperrt, bis ein manueller Lauf, `check` oder `setup` wieder klappt.
- Eine **Prozesssperre** verhindert parallele Läufe. Dauert ein Lauf zu lange (Standard 25 Minuten), wird er hart beendet und gemeldet; der Rest folgt beim nächsten Start.

### Eintragsarten

| Quelle / Art | Google Kalender „Schule“ | Google Tasks „Schule“ | Vorlauf |
|---|---|---|---|
| Eduvidual-Aufgabe / Abgabe | **kein Kalendereintrag** (nur als Task; wer beides will: `assignment_calendar = true`) | „Bearbeiten: Titel“ (wird beim Abgeben in Eduvidual **automatisch abgehakt**) | Aufgabendatum **1 Kalendertag** vor der Frist (die echte Frist steht im Text) |
| WebUntis-Hausaufgabe | „Abgabe: Titel“ am Abgabetermin | „Bearbeiten: Titel“ | **3 Kalendertage** vorher |
| Prüfung / Schularbeit | Prüfungstermin mit Start und Ende (Art laut Quelle, z. B. „Schularbeit: …“, sonst „Prüfung: …“) | „Lernen: Titel“ | **7 Kalendertage** vorher |
| Unterrichtsentfall | „Entfall: Titel“ mit exakter Start- und Endzeit | keine Aufgabe | – |

Nicht übernommen werden Supplierungen, reine Lehrkraft- oder Raumänderungen und reguläre Stunden. Der Stundenplan wird nur gelesen, um Entfälle zu erkennen. Anhänge, PDFs und Bilder werden nie ausgewertet.

### Regeln im Detail

- **Prüffenster (31 Tage):** Neue Termine werden von **heute 00:00** bis **heute + 31 Kalendertage 00:00** (Ende ausgeschlossen, Europe/Vienna) angelegt. Bereits abgelaufene Fristen und
  vollständig vergangene Ereignisse werden nicht neu angelegt. **Bekannte, noch offene Einträge** werden auch außerhalb des Fensters weiter auf Verschiebungen und Absagen geprüft.
- **Erinnerung:** Jeder Kalendereintrag bekommt eine Popup-Erinnerung **24 Stunden vorher**. Für **kurzfristig entdeckte** Termine gibt es **keine rückwirkende Benachrichtigung**:
  Liegt der Termin in weniger als 24 Stunden, ist der Erinnerungszeitpunkt schon vorbei. Ob und wie eine Erinnerung angezeigt wird, hängt außerdem von den
  **Benachrichtigungseinstellungen deiner Google-Kalender-App** ab (Berechtigung, „Nicht stören“, Akkusparmodus). Prüfe sie in der App.
- **Das Task-Datum ist der Arbeitsbeginn, nicht die Frist.** Google Tasks kennt nur ein Datum, keine Uhrzeit. Die **echte Frist mit Uhrzeit** steht im Text der Aufgabe und im Kalender.
  Lag der Arbeitsbeginn bei der Entdeckung schon zurück (Frist steht aber noch bevor), wird das Datum **einmalig auf heute** gesetzt. Überfällige Aufgaben werden **nicht täglich** verschoben;
  nur eine echte Änderung der Frist rechnet neu.
- **Uhrzeiten:** Ein Datum ohne Uhrzeit ergibt einen **ganztägigen** Termin. Eine Frist mit Uhrzeit wird als **einminütige Markierung** dargestellt (die Minute ist nur Darstellung, keine
  Fristverlängerung). Ist bei einer Prüfung nur der Start bekannt, steht „Endzeit unbekannt“ im Eintrag. Einträge **ohne Datum** werden übersprungen und im Bericht genannt.
- **Erledigt bleibt erledigt.** Eine abgehakte Aufgabe wird nie wieder geöffnet, zurückgesetzt oder verändert, auch wenn sich der Schultermin ändert.
- **Automatisch abgehakt wird nur bei Eduvidual:** Meldet Eduvidual die Abgabe als **eingereicht** („submitted“), wird die offene Aufgabe abgehakt. Ein Entwurf zählt nicht.
  Ist der Abgabestatus nicht lesbar, ändert sich nichts. Wird die Abgabe zur **Überarbeitung zurückgegeben** (neuer Versuch), entsteht eine **zweite** Aufgabe; der Kalendertermin bleibt derselbe.
  Ist die Abgabe schon eingereicht, wenn SchulSync sie zum ersten Mal sieht, wird keine Aufgabe angelegt.
- **Fristverschiebung:** Kalendereintrag und offene Aufgabe werden aktualisiert (derselbe Eintrag, keine neue Identität).
- **Absagen:** Nur bei **eindeutig bestätigter** Absage oder Rücknahme wird der Kalendereintrag mit „Abgesagt:“ gekennzeichnet (Erinnerung aus) und **nur die zugehörige offene Aufgabe**
  entfernt (mit den Notizen, die du in dieser Aufgabe geschrieben hast). **Erledigte Aufgaben bleiben.** Ein Eintrag, der nur aus einer Liste verschwindet, das Ende des Prüffensters, ein leeres
  Ergebnis oder ein Verbindungsfehler gelten **nie** als Absage. Wird eine Absage zurückgenommen, wird derselbe Kalendereintrag wiederverwendet; eine wegen der Absage entfernte Aufgabe darf wieder entstehen,
  eine erledigte oder von dir gelöschte nicht.
- **Bereich „Persönliche Notizen“:** In jedem Termin und jeder Aufgabe gibt es einen abgegrenzten Bereich **„Automatisch übernommen“** (wird bei Änderungen überschrieben) und einen Bereich
  **„Persönliche Notizen“**. Was du **außerhalb** des automatischen Bereichs schreibst, bleibt erhalten. Auch einen Ort, den du selbst in einen Termin einträgst, lässt SchulSync stehen.
  Ändere den Text zwischen den `===`-Zeilen nicht, er wird beim nächsten Abgleich ersetzt.
- **Was passiert, wenn du in Google etwas löschst:** Löschst du einen von SchulSync angelegten Termin oder eine Aufgabe **bewusst**, wird er **nicht bei jedem Lauf neu angelegt**. SchulSync merkt sich das
  und zeigt es in `schulsync status` und im Bericht. Mit `schulsync restore` (alle) oder `schulsync restore <UID>` (einzeln) stellst du es ausdrücklich wieder her.
- **Fremde Einträge** in deinem Kalender oder deiner Liste (ohne SchulSync-Kennzeichnung) werden nie verändert, übernommen oder gelöscht.
- **Fällt eine Quelle aus,** bleiben die Einträge der anderen unberührt. Ist ein **Google-Ziel** nicht erreichbar (Kalender oder Liste fehlt), werden nur die betroffenen Schreibvorgänge
  zurückgestellt und der Fehler gemeldet. Es wird **nie** auf den Hauptkalender ausgewichen.
- **Doppelte Einträge werden vermieden:** Gleiche Daten zweimal liefern nichts Neues. Ging bei einem Abbruch die Antwort von Google verloren, gleicht SchulSync vor dem erneuten Anlegen mit der eigenen
  Projektkennung ab. Löschst du die lokale Datenbank, erkennt SchulSync seine Einträge in Google wieder.

## 12. Fehlerbericht, Protokolle, Daten

**Wann öffnet sich der Bericht?** Bei einem **fehlgeschlagenen oder unvollständigen** Lauf öffnet sich automatisch ein **HTML-Bericht** im Standardbrowser. Bei Erfolg öffnet sich **nichts**.
Er erscheint **einmal pro Durchlauf**, nicht bei jedem Einzelversuch. **Derselbe Fehler am selben Tag** öffnet keinen zweiten Tab; die Berichtsdatei wird trotzdem aktualisiert. Einstellbar in
`config.toml` unter `[report]` mit `open_on = "problems"` (Standard), `"warnings"` oder `"never"`. Gelesen wird der neueste Bericht jederzeit mit `schulsync report`.

**Was steht drin?** Zeitpunkt mit Zeitzone und Gesamtstatus, Ergebnis je Quelle und Google-Ziel, Zahlen (neu, aktualisiert, unverändert, abgesagt, übersprungen), konkret betroffene Aufgaben und Termine,
bereits Übernommenes, letzter erfolgreicher Abruf (und ob die Daten veraltet sind) sowie **nächste Schritte** (z. B. Internet prüfen, Anmeldung erneuern). Der Bericht ist eine einzelne lokale Datei ohne
Skripte oder externe Inhalte; Texte aus den Schulplattformen werden maskiert.

**Wo liegt was?** Alles unter `%LOCALAPPDATA%\SchulSync` (oder dem Ordner aus `--home` bzw. `SCHULSYNC_HOME`):

| Datei / Ordner | Inhalt |
|---|---|
| `config.toml` | lesbare Einstellungen (Quellen, Kursauswahl, Ziel-Kennungen, Vorlaufzeiten, Wiederholungen, Berichte), **keine Geheimnisse** |
| `secrets.dat` | Passwörter, OAuth-Client und Google-Anmeldung, **mit Windows DPAPI geschützt** |
| `state.sqlite3` | lokaler Zustand (Zuordnung zu Google, Tageserfolge je Quelle, ausstehende Schreibvorgänge) |
| `reports\bericht-JJJJ-MM-TT.html` | Bericht des Tages; spätere Läufe desselben Tages überschreiben ihn |
| `reports\latest.html` | Kopie des neuesten Berichts |
| `reports\vorschau.html` | Ergebnis von `schulsync preview` |
| `logs\schulsync-JJJJ-MM-TT.log` | Protokoll pro Tag |

**Aufbewahrung:** Berichte und Protokolle werden nur **begrenzt** aufbewahrt, standardmäßig **30 Tage** (`keep_days` und `log_keep_days` in `config.toml`). Die Datenbank bleibt bestehen.

Eine kommentierte Vorlage mit allen Einstellungen ist `config.example.toml`. Ungültige Werte (z. B. ein Zeitlimit von 0 oder `http://` statt `https://`) meldet SchulSync als verständlichen
Konfigurationsfehler. Mit `courses = [...]` in `[eduvidual]` kannst du Kurse auswählen (Kurs-ID oder exakter Kursname; leer = alle aktuellen Kurse).

**Alles entfernen:** `schulsync autostart remove`, dann den Ordner `%LOCALAPPDATA%\SchulSync` und den SchulSync-Ordner löschen. In Google Kalender und Google Tasks bleiben die bereits angelegten Einträge
stehen; den Zugriff widerrufst du in deinem Google-Konto unter „Sicherheit“.

## 13. Datenschutz und Sicherheit

- **Passwörter und Google-Anmeldung** liegen in `secrets.dat`, geschützt mit der **Windows-Datenschutz-API (DPAPI)** für **dein Benutzerkonto**. Die Datei lässt sich nur unter deinem Windows-Konto auf diesem PC
  entschlüsseln. Nach einem Wechsel des Benutzers oder des PCs musst du `schulsync setup` wiederholen.
- **Keine Geheimnisse in Dateien oder Protokollen:** Weder `config.toml` noch Berichte, Protokolle oder die Konsole enthalten Passwörter, Tokens oder Cookies. Die Maskierung arbeitet nach Namen und Form (z. B.
  Parameter wie `token`, `sesskey`, `sig`); ein unbekannter Parametername kann sie umgehen. Die OAuth-Client-Datei, die du von Google herunterlädst, gib trotzdem nicht weiter.
- **Nur Lesezugriff auf die Schulplattformen:** Der Code sendet dort nur lesende Anfragen und die Anmeldung. Für Eduvidual und WebUntis ist das zusätzlich durch feste Listen erlaubter Funktionen abgesichert.
- **Google:** Es werden nur die drei Bereiche aus Abschnitt 4.3 angefragt. SchulSync verändert nur Einträge mit eigener Projektkennung.
- **Keine KI und keine Cloud-Dienste** außer deinen eigenen Google-Konto-Schnittstellen und den beiden Schulplattformen. Alles Übrige liegt lokal.

## 14. Fehlerbehebung

| Problem | Ursache und Lösung |
|---|---|
| **Anmeldung abgelehnt / Sperre** („Anmeldung abgelehnt seit …“ in `schulsync status`) | Passwort bei Eduvidual oder WebUntis falsch. SchulSync probiert genau **einmal** und sperrt die Quelle für automatische Läufe, damit dein Konto nicht gesperrt wird. Passwort korrigieren: `schulsync setup --eduvidual` bzw. `--webuntis`, dann `schulsync check` oder `schulsync run`: Die Sperre wird aufgehoben, sobald die Anmeldung klappt. Ist dein Konto bei der Schule bereits gesperrt, nur dort entsperren lassen. |
| **Google-Anmeldung abgelaufen oder widerrufen** | Im Test-Modus alle 7 Tage normal: Beim nächsten Lauf öffnet sich der Google-Login von selbst (Abschnitt 4.5). Von Hand: `schulsync setup --google`. Kommt „Zugriff blockiert“, fehlt deine Adresse unter **Testnutzer**. |
| **Kalender „Schule“ nicht gefunden** | Er wird nicht automatisch angelegt (Abschnitt 5). Kalender anlegen, Name genau „Schule“, dann `schulsync setup --google`. Mehrere Kalender gleichen Namens: umbenennen, sodass genau einer „Schule“ heißt. Der Hauptkalender wird nie benutzt. |
| **Mehrere Google-Konten / falsches Konto** | Die Einrichtung bricht mit „Du hast dich bei Google als … angemeldet“ ab und speichert nichts. Im Browser das Konto aus `config.toml` wählen und `schulsync setup --google` wiederholen. Willst du bewusst ein anderes Konto, trage es zuerst in `config.toml` unter `[google]` bei `account` ein. |
| **Bericht öffnet sich nicht** | `schulsync report` versucht es erneut. Klappt auch das nicht, steht der Pfad in der Ausgabe: `%LOCALAPPDATA%\SchulSync\reports\latest.html` per Doppelklick öffnen. Der Pfad steht auch im Protokoll unter `logs\`. Prüfe, ob ein Standardbrowser eingestellt ist. |
| **Aufgabenplanung verweigert den Zugriff** | Dein PC erlaubt keine eigenen Aufgaben. `schulsync autostart install` nimmt automatisch die Startup-Verknüpfung (Abschnitt 10). Prüfen mit `schulsync autostart status`. |
| **Der Autostart scheint nicht zu laufen** | `schulsync autostart status`; in der Aufgabenplanung bei „SchulSync“ das **Letzte Ergebnis** ansehen; Protokoll unter `logs\` lesen. Nach einem erfolgreichen Lauf beendet sich jeder weitere Start im selben Zeitfenster (0–6, 6–18, 18–24 Uhr) **still**: Das ist gewollt. Zum Erzwingen: `schulsync run --force`. |
| **„SchulSync läuft gerade, bitte kurz warten“** | Ein anderer Lauf, `check` oder die Einrichtung ist noch aktiv. Kurz warten und den Befehl wiederholen. Ein hängender Lauf wird nach dem Zeitlimit hart beendet. |
| **Alte Schulautomation / Überschneidungen / doppelte Einträge** | Läuft noch ein altes Skript, Plugin oder Zapier in denselben Kalender oder dieselbe Liste, entstehen doppelte Einträge. Schalte die alte Automation ab. SchulSync erkennt sie nicht und fasst fremde Einträge nicht an; doppelte Altlasten löschst du von Hand. |
| **Ein Eintrag fehlt oder ist weg** | `schulsync status` zeigt, ob du ihn gelöscht hast (dann `schulsync restore`), ob Schreibvorgänge noch ausstehen oder ob die Quelle gesperrt ist. Der Bericht (`schulsync report`) nennt Gründe, z. B. „Ohne Datum“ oder „Frist/Ereignis bereits vorbei“. |
| **Eduvidual-Aufgaben ohne Frist fehlen** | Über den Zugang `web` (Ersatzweg) sind sie unsichtbar; der Bericht sagt das. `schulsync check` zeigt, welcher Zugang benutzt wird. |
| **Keine Erinnerung gekommen** | Siehe Abschnitt 11: keine rückwirkende Benachrichtigung bei kurzfristig entdeckten Terminen; außerdem zählen die Benachrichtigungseinstellungen deiner Kalender-App. |
| **Konfigurationsfehler** („Unbekannte Einstellung …“, „muss … sein“) | `config.toml` im Datenordner öffnen und die genannte Zeile korrigieren; Vorlage: `config.example.toml`. Zahlen ohne Anführungszeichen, Texte in Anführungszeichen. |

## 15. Ehrliche Einschränkungen

- **Teilweise nur gegen Testdaten geprüft.** Nicht durch automatische Tests mit echten Konten abgedeckt sind: das Verhalten der echten Calendar- und Tasks-API bei gelöschten Einträgen,
  der echte Abgabestatus von Eduvidual, die tatsächlichen Antworten von Eduvidual und WebUntis, die Windows-Aufgabenplanung mit dem Anmeldetrigger, das Öffnen des Browsers aus der Aufgabenplanung,
  die DPAPI nach einem Benutzerwechsel und `scripts\install.ps1`. Bis `schulsync check` und ein erster echter Lauf bei dir funktionieren, ist die Anbindung **nicht bestätigt**.
- **Welcher Zugang geht, ist offen.** Eduvidual lässt sich über einen Web-Service-Token oder über die Weboberfläche lesen, WebUntis über JSON-RPC und REST. Ob die Schulen diese Wege für dein Konto
  freigeben, zeigt `check`. Das automatische Abhaken funktioniert nur über den Web-Service-Zugang (`webservice`); über `web` bleibt der Abgabestatus unbekannt. Mit nur Bildungsportal-Anmeldung (Shibboleth), Microsoft-Anmeldung oder Zwei-Faktor funktioniert die Anbindung per reinem HTTP nicht.
  **Playwright oder eine andere Browser-Automatisierung wird nicht verwendet und ist nicht gebaut.** Gibt es keinen lesbaren Weg, kann diese Quelle nicht gelesen werden und es gibt noch keinen Ersatz;
  der Bericht sagt das offen, statt „keine Einträge“ zu behaupten.
- **Nicht erkennbar:** Rücknahmen von Hausaufgaben und Prüfungen (die Plattformen melden keine), Absagen bei Eduvidual, Einträge, die WebUntis nicht als Entfall führt, Inhalte von PDFs, Bildern und
  Anhängen (werden bewusst nicht gelesen), Eduvidual-Aufgaben ohne Frist über den Zugang `web`.
- **Ungeprüfte Annahmen über die Plattformen** (z. B. Seitenaufteilung bei WebUntis, Stunden-IDs nach einem Neuimport, ob Titel abgeschnitten geliefert werden): Vergleiche die Anzahlen in `check` und die
  Vorschau mit deiner Browseransicht. Einzelheiten stehen in `docs/datenzugriff.md`.
- **Google-Besonderheiten ungeprüft:** wie die Oberfläche das Aufgabendatum und Beschreibungen mit Zeichen wie `<` anzeigt, ob eine geleerte erledigte Aufgabe zuverlässig als solche erkannt wird, ob
  eine Beschreibung nach manueller Bearbeitung wie erwartet gespeichert wird. Teste mit je einer eigenen Notiz in Termin und Aufgabe (Abschnitt 11) und siehe `docs/ANFORDERUNGEN_TRACE.md`.
- Ein einzelner Google-Schreibvorgang wird im Lauf nicht wiederholt; er folgt beim nächsten Start.
- Aufgabendaten haben nur ein Datum (Google Tasks), die echte Frist mit Uhrzeit steht im Text.
- Die Meldungen der Windows-Aufgabenplanung hängen von der Anzeigesprache ab; eine unbekannte Meldung wird als „unbekannt“ behandelt, nie als „nicht vorhanden“.

Welche Anforderung wodurch belegt ist und was noch ein Test mit echten Konten braucht, steht in `docs/ANFORDERUNGEN_TRACE.md`.

## 16. Entwicklung

Aufbau (`src/schulsync`): Quelladapter in `adapters/` (Eduvidual, WebUntis), Regeln und Normalisierung (`rules.py`, `textblocks.py`), Abgleich (`sync.py`), Google-Zugriffe (`google_*.py`),
Zustand (`state.py`, SQLite), Konfiguration (`config.py`), Berichte (`report.py`), Tageslauf (`runner.py`), Befehlszeile (`cli.py`), Autostart (`autostart.py`).

Tests:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

Die Tests laufen ohne Netz und ohne Konten (Uhr, Browser, Netzprüfung und Google sind ersetzt).

Weitere Dokumente:

- `docs/ANFORDERUNGEN.md`: die Anforderungen im Originaltext.
- `docs/ANFORDERUNGEN_TRACE.md`: Rückverfolgbarkeit, welche Anforderung durch welchen Test belegt ist.
- `docs/datenzugriff.md`: Überblick über den Datenzugriff, mit `docs/datenzugriff_eduvidual.md` und `docs/datenzugriff_webuntis.md`.
- `docs/DESIGN.md`, `docs/DESIGN_PHASE2.md`: Entwurf.
- `docs/ACCEPTANCE_NOTES.md`: Abnahme-Notizen mit allen Auslegungen.

## 17. Lizenz (Copyleft)

SchulSync steht unter der **GNU General Public License, Version 3 oder (nach deiner Wahl) jeder späteren Version** (GPL-3.0-or-later).
Den vollständigen Text findest du in [`LICENSE`](LICENSE).

Kurz gesagt (rechtlich gilt nur der Lizenztext):

- Du darfst SchulSync **benutzen, untersuchen, verändern und weitergeben**, auch kommerziell.
- **Copyleft:** Gibst du SchulSync oder eine veränderte Fassung weiter, muss das **unter derselben Lizenz und mit Quellcode** geschehen.
- Es gibt **keine Gewährleistung**. Die Nutzung geschieht auf eigene Verantwortung; beachte die Nutzungsbedingungen von Eduvidual, WebUntis und Google.

**Bitte veröffentliche nie** deine OAuth-Client-Datei (`client_secret_….json`), deine `config.toml`, `secrets.dat` oder die Datenbank aus
`%LOCALAPPDATA%\SchulSync`. Sie gehören nicht ins Repository; `.gitignore` schließt sie aus.

### Die fertige .exe bauen und veröffentlichen (für Entwickler)

Lokal bauen (erzeugt `dist\SchulSync-Setup.exe` und die Prüfsumme `dist\SchulSync-Setup.exe.sha256`):

```powershell
powershell -File scripts\build_exe.ps1
```

Veröffentlichen: Erhöhe `__version__` in `src/schulsync/__init__.py` (und `version` in `pyproject.toml`), committe, und pushe einen passenden Tag:

```powershell
git tag v1.0.1
git push origin v1.0.1
```

Der Workflow `.github/workflows/release.yml` führt die Tests aus, baut die .exe und legt auf GitHub eine Release mit `SchulSync-Setup.exe` und der Prüfsumme an.
`schulsync update` nutzt genau diese beiden Dateien. Ein Fork setzt die Umgebungsvariable `SCHULSYNC_REPO=besitzer/repo`.
