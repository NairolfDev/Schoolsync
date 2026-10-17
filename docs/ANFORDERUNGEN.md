# SchulSync – Anforderungen (Originaltext des Auftraggebers, unverändert)

Du bist ein erfahrener Python-Entwickler. Entwickle für mich ein vollständiges, wartbares Windows-Programm namens „SchulSync“. Setze das Projekt um, teste es und liefere eine einfache deutsche Einrichtungsanleitung. Dieser Text enthält die bereits abgestimmten Anforderungen. Stelle nur Rückfragen, wenn für einen konkreten Umsetzungsschritt tatsächlich Informationen fehlen. Technische Routineentscheidungen triffst und dokumentierst du selbst.

Das Programm soll meine Schulplattformen nach festen Regeln auslesen und meine Termine und Aufgaben synchronisieren. Der laufende Betrieb muss vollständig ohne KI, Sprachmodelle, KI-APIs, Bildinterpretation oder inhaltliche Zusammenfassungen funktionieren. Der Coding-Agent darf selbstverständlich beim Entwickeln helfen. Inhalte werden kopiert und Daten mit fest definierten Regeln verarbeitet.

**1. Windows-Start und täglicher Ablauf**

- Das Programm startet automatisch nach meiner Windows-Anmeldung, also wenn ich meinen PC einschalte und mich anmelde. Es läuft im Benutzerkontext, damit es bei Fehlern einen Bericht in meinem Browser öffnen kann.
- An jedem Wochentag einschließlich Montag höchstens ein erfolgreicher automatischer Tagesdurchlauf. Maßgeblich ist der Kalendertag in Europe/Vienna, einschließlich Sommer-/Winterzeit.
- Keine feste Uhrzeit wie 06:00 Uhr. Kein Server oder dauerhaft laufender Hintergrunddienst erforderlich. Bleibt der PC ohne neue Anmeldung über Nacht eingeschaltet, ist kein zusätzlicher zeitgesteuerter Lauf vorgeschrieben.
- Bevorzugt Windows-Aufgabenplanung mit Anmeldetrigger für meinen Benutzer, kurzer Startverzögerung und ohne erhöhte Rechte. Liefere einfache Installation und Entfernung dieses Autostarts. Falls das auf dem Zielsystem nicht möglich ist, ist eine dokumentierte Benutzer-Autostart-Verknüpfung eine Alternative.
- Speichere Tageserfolge getrennt je Quelle und halte noch ausstehende Google-Schreibvorgänge fest. Ein Teilfehler darf nicht als vollständiger Erfolg gelten.
- Nach einem erfolgreichen Lauf beendet sich ein erneuter automatischer Start am selben Tag still. Fehlgeschlagene Teile dürfen erneut versucht werden; bereits erfolgreiche Vorgänge erzeugen keine weiteren Einträge.
- Berücksichtige noch fehlendes Internet unmittelbar nach der Anmeldung. Verwende Zeitlimits und wenige begrenzte Wiederholungen mit zunehmender Wartezeit. Dauerhafte Anmeldefehler nicht endlos wiederholen.
- Verhindere parallele Durchläufe durch eine Prozesssperre. Biete zusätzlich einen manuellen Start an, der die Tagesbegrenzung ausdrücklich umgehen kann.
- Nach mehreren ausgeschalteten Tagen den aktuellen Datenbestand einmal prüfen; keine Serie fiktiver nachträglicher Tagesläufe durchführen.

Microsoft beschreibt den dafür geeigneten Anmeldetrigger hier: [LogonTrigger](https://learn.microsoft.com/en-us/windows/win32/taskschd/logontrigger).

**2. Quellen und Umfang**

Eduvidual:
- Adresse: https://www.eduvidual.at/
- Anmeldung mit Benutzername und Passwort.
- Aufgaben und Abgaben aus meinen aktuellen, für mich sichtbaren Kursen übernehmen. Alle aktuellen Kurse sind die Voreinstellung; eine konfigurierbare Kursauswahl vorsehen.
- Die Zeitleiste ist ein Ausgangspunkt. Bei Bedarf Details öffnen, um vollständige Titel, Beschreibungen, Fristen und Quellkennungen zu lesen.

WebUntis:
- Anmeldeadresse: https://htbla-grieskirchen.webuntis.com/WebUntis/?school=htbla-grieskirchen#/basic/login
- Schule: HTBLA Grieskirchen; Schulkennung laut URL: htbla-grieskirchen.
- Anmeldung mit Benutzername und Passwort; meinen persönlichen Schülerzugang verwenden.
- Hausaufgaben, Prüfungen, Schularbeiten und Unterrichtsentfälle übernehmen.
- Supplierungen, reine Lehrkraft-/Raumänderungen und reguläre Unterrichtsstunden nicht als eigene Termine übernehmen. Der Stundenplan darf gelesen werden, soweit er zur Ermittlung der gewünschten Daten notwendig ist.

Prüfe neue Termine im Fenster von heute 00:00 Uhr einschließlich bis heute plus 21 Kalendertage 00:00 Uhr ausschließlich, jeweils Europe/Vienna. Bereits abgelaufene Abgabefristen und vollständig vergangene Ereignisse nicht neu anlegen. Bekannte offene Einträge müssen bei Bedarf auch außerhalb dieses Fensters weiter auf konkrete Änderungen oder Absagen geprüft werden, damit eine verschobene Frist nicht ungeprüft veraltet stehen bleibt.

**3. Tatsächlichen Datenzugriff zuerst prüfen**

Für die Schulplattformen ist noch keine funktionsfähige Programmanbindung nachgewiesen. Erfinde deshalb keine API-Endpunkte, Feldnamen, Zugangstokens oder angeblich verfügbaren Exportfunktionen.

Prüfe mit dem vorhandenen Schülerzugang, welche strukturierten Schnittstellen oder Kalenderexporte wirklich zugänglich sind und welche der geforderten Kategorien sie abdecken. Ein Kalenderexport genügt nur für die Daten, die er tatsächlich vollständig liefert. Fehlende Kategorien dürfen nicht stillschweigend entfallen.

Wenn erforderlich, nutze eine deterministische Browser-Automation, beispielsweise Playwright, die konkrete Seiten und Felder ausliest. Keine KI und keine Auswertung von Screenshots. Schulanwendungen ausschließlich lesen; dort keine Abgaben, Häkchen oder anderen Inhalte verändern.

Beachte Pagination, alle relevanten Datumsbereiche und Detailansichten. Unterscheide eine echte leere Ergebnisliste von Loginseiten, fehlenden Rechten, Ladefehlern und unvollständigen Antworten. Geänderte Seitenstrukturen müssen einen erkennbaren Fehler auslösen.

**4. Google-Ziele und Anmeldung**

- Google-Konto: <eigene Google-Adresse>.
- Kalender ausschließlich „Schule“. Bestehenden Kalender bei der Einrichtung eindeutig auswählen und dessen ID speichern.
- Echte abhakbare Aufgaben in einer Google-Tasks-Liste „Schule“. Vorhandene passende Liste verwenden oder bei der Einrichtung anlegen, falls keine vorhanden ist; ihre ID speichern.
- Google Calendar API und Google Tasks API direkt verwenden, mit regulärer einmaliger OAuth-Einrichtung und späterer Token-Erneuerung.
- Kein Kalender-Plugin und keine Abhängigkeit von einer geöffneten Chat-Anwendung.
- Niemals automatisch auf den Hauptkalender oder eine andere Aufgabenliste ausweichen. Ist ein Ziel nicht erreichbar, die betroffenen Schreibvorgänge zurückstellen und den Fehler berichten.
- Dokumentiere Einrichtung von Google-Cloud-Projekt, Calendar API, Tasks API und Desktop-OAuth-Zugang verständlich. Verwende nur die erforderlichen Berechtigungen.
- Beachte, dass Refresh-Tokens bei externer OAuth-App im Status „Testing“ für diese Berechtigungen nach sieben Tagen ablaufen können. Beschreibe eine für den persönlichen Dauerbetrieb geeignete Einrichtung und behandle erforderliche Neuanmeldungen verständlich. Versprich keine unbegrenzt gültige Anmeldung. [Google OAuth-Dokumentation](https://developers.google.com/identity/protocols/oauth2#expiration)

**5. Exakte Kalender- und Aufgabenregeln**

| Quelle/Art | Google Kalender „Schule“ | Google Tasks „Schule“ |
|---|---|---|
| Eduvidual-Aufgabe/Abgabe | Tatsächliche Abgabefrist | „Bearbeiten: ORIGINALTITEL“, Aufgabendatum drei Kalendertage vor der Abgabe |
| WebUntis-Hausaufgabe | Tatsächlicher Abgabetermin | „Bearbeiten: ORIGINALTITEL“, Aufgabendatum drei Kalendertage vorher |
| Prüfung/Schularbeit | Tatsächlicher Prüfungstermin | „Lernen: ORIGINALTITEL“, Aufgabendatum sieben Kalendertage vorher |
| Unterrichtsentfall | Exakte Start- und Endzeit der ausgefallenen Stunde | Keine Task |

Für alle übernommenen Kalenderkategorien eine Popup-Erinnerung 24 Stunden vor dem Termin konfigurieren. Bei kurzfristig entdeckten Terminen keine rückwirkende Benachrichtigung versprechen. Die Anzeige hängt außerdem von den Benachrichtigungseinstellungen meiner Google-Kalender-App ab.

Eine Task wird bei Entdeckung angelegt und trägt das errechnete Vorlaufdatum. Dieses Datum bezeichnet den Arbeitsbeginn und ersetzt nicht die tatsächliche Abgabefrist. Die echte Frist mit vorhandener Uhrzeit steht ausdrücklich im Aufgabentext und im Kalender. Verwende die tatsächlichen Möglichkeiten der Tasks API: Sie unterstützt hier ein Fälligkeitsdatum, aber keine Fälligkeitsuhrzeit. Erfinde kein zusätzliches Startzeit- oder Erinnerungsfeld. [Tasks-Dokumentation](https://developers.google.com/tasks/reference/rest/v1/tasks)

Weitere Regeln:
- Für Vorläufe Kalendertage subtrahieren, nicht pauschal 72 oder 168 Stunden über eine Zeitumstellung hinweg.
- Liegt der errechnete Arbeitsbeginn bei der ersten Entdeckung bereits in der Vergangenheit, während die Abgabe/Prüfung noch bevorsteht, das Task-Datum einmalig auf heute setzen.
- Bestehende überfällige Tasks nicht jeden Tag auf heute verschieben. Nur eine echte Änderung der zugrunde liegenden Frist rechtfertigt eine Neuberechnung.
- Bei Friständerungen Kalender und offene Task entsprechend aktualisieren; liegt der neue Vorlauf bereits zurück, den Änderungszeitpunkt nach obiger Regel verwenden.
- Bereits vollständig vergangene Fristen erzeugen keine neuen Lern-/Bearbeitungsaufgaben.
- Titel, Fach/Kurs, Beschreibung, verfügbare Raum-/Lehrkraftangaben und passende Quelllinks unverfälscht übernehmen. Feste Kategoriepräfixe wie „Abgabe:“ und „Entfall:“ sind erlaubt.
- Keine Inhalte aus PDFs, Bildern oder Anhängen interpretieren. Vorhandene sichere Links dürfen übernommen werden.
- Abgeschnittene Titel nach Möglichkeit aus der Detailansicht vervollständigen. Nicht rekonstruierbare Angaben nicht erfinden, sondern als unvollständig melden.
- Datum ohne Uhrzeit: ganztägiger Kalendertermin. Kein Datum: Eintrag überspringen und im Bericht aufführen.
- Abgaben mit genauer Uhrzeit als kurze, einminütige Fristmarkierung darstellen; die Minute ist nur Darstellungsdauer und keine Verlängerung der Abgabefrist.
- Prüfungen und Entfälle mit den tatsächlichen Start-/Endzeiten übernehmen. Falls nur eine Startzeit sicher bekannt ist, eine einminütige Markierung verwenden und „Endzeit unbekannt“ ausdrücklich vermerken.
- Ganztägige Termine mit korrektem exklusivem Enddatum speichern. Zeitzonen und Zeitumstellungen durchgängig berücksichtigen.

**6. Erledigung, Absagen und persönliche Änderungen**

- Ich hake Google Tasks ausschließlich selbst ab. Keine automatische Erledigung anhand eines Abgabestatus in Eduvidual oder WebUntis.
- Einmal erledigte Tasks bleiben erledigt, auch bei späteren Änderungen am Schultermin. Keine neue offene Kopie und kein automatisches Zurücksetzen.
- Bei eindeutig bestätigter Absage oder Rücknahme: bestehenden Kalendertermin mit „Abgesagt:“ kennzeichnen und dessen Erinnerung deaktivieren; ausschließlich die zugehörige offene Task entfernen. Erledigte Tasks erhalten.
- Ein Unterrichtsentfall ist selbst ein gewünschter Eintrag. Verschwindet eine Unterrichtsentfall-Meldung nur aus einer Liste, ist das noch kein Nachweis einer Rücknahme.
- Fehlende Einträge in einer Liste, Ende des Vorschaufensters, Abgabestatus und Verbindungsfehler gelten nicht als bestätigte Löschung/Absage.
- Bei ausdrücklich bestätigter Reaktivierung denselben Kalendereintrag wiederverwenden. Eine wegen Absage entfernte offene Task darf passend neu entstehen; eine zuvor erledigte oder von mir bewusst gelöschte Task bleibt respektiert.
- Titel, Zeit und automatisch kopierte Angaben dürfen bei Quelländerungen überschrieben werden. Persönliche Notizen und Erledigtstatus müssen erhalten bleiben.
- Verwende klar abgegrenzte Bereiche „Automatisch übernommen“ und „Persönliche Notizen“. Lies den aktuellen Google-Stand vor Änderungen und bewahre Text außerhalb des verwalteten Bereichs.
- Bewusst von mir gelöschte eigene Google-Einträge nicht bei jedem Lauf kommentarlos neu erstellen. Den Zustand nachvollziehbar festhalten und eine explizite Wiederherstellung ermöglichen.

**7. Synchronisation und Datenspeicherung**

Verwende eine lokale SQLite-Datenbank. Speichere mindestens Quellsystem, stabile Quellkennung, Ereignisart, Ziel-IDs, relevante Inhaltsänderungen, Synchronisations-/Absagestatus und letzten erfolgreichen Abruf.

- Einträge anhand stabiler Quellkennungen wiedererkennen, nicht nur anhand von Titel und Datum. Eine Terminverschiebung darf keine neue Identität erzeugen.
- Fehlen stabile Kennungen, eine nachvollziehbare konservative Zuordnungsstrategie verwenden und Unsicherheiten melden.
- Eigene Kalendertermine zusätzlich mit privaten Projekteigenschaften kennzeichnen. Google dokumentiert diese unter [Extended properties](https://developers.google.com/workspace/calendar/api/guides/extended-properties).
- Bei Tasks eine mit der API kompatible Kennzeichnung im verwalteten Notizbereich plus lokale Zuordnung verwenden; keine nicht vorhandenen benutzerdefinierten API-Felder erfinden.
- Ausschließlich eindeutig diesem Projekt zugehörige Einträge verändern.
- Gleiche Inhalte innerhalb einer Quelle nicht doppelt anlegen. Quellenübergreifend nur bei eindeutiger Identität oder konfigurierter Zuordnung zusammenführen. Ähnliche Titel allein genügen nicht.
- Eduvidual und WebUntis unabhängig verarbeiten. Der Fehler einer Quelle darf weder den Erfolg der anderen verhindern noch deren Termine entfernen.
- Auch Teilabbrüche und unklare Schreibantworten behandeln: Wenn Google einen Eintrag erstellt hat, aber die Antwort verloren geht, vor einem erneuten Anlegen anhand der Projektkennung abgleichen.
- Solange ein Schreibausgang nicht geklärt ist, keinen zweiten Eintrag auf Verdacht erzeugen. Dies ist insbesondere bei Tasks zu berücksichtigen.
- Auch erledigte, ausgeblendete und gelöschte Task-Zustände bei der Zuordnung berücksichtigen, soweit von der API bereitgestellt.
- Keine alten Termine allein deshalb löschen, weil sie außerhalb des aktuellen Suchfensters liegen.

Die früher erwähnten zwölf Einträge sind nicht Teil einer erforderlichen Migration. Bestehende fremde Einträge unangetastet lassen. Falls eine alte Schulautomation noch läuft, auf mögliche Überschneidungen hinweisen; unbekannte Automationen nicht pauschal verändern.

**8. HTML-Fehlerbericht**

Bei einem endgültig fehlgeschlagenen oder unvollständigen Lauf eine lokale, eigenständig öffnungsfähige HTML-Datei erzeugen und automatisch im Standardbrowser öffnen. Eine Windows-Benachrichtigung oder bloße Konsolenausgabe genügt nicht.

Der Bericht muss auf Deutsch verständlich zeigen:
- Zeitpunkt mit Zeitzone und Gesamtstatus.
- Ergebnis je Quelle und Google-Ziel.
- Anzahl neuer, aktualisierter, unveränderter, abgesagter und übersprungener Einträge.
- Konkret fehlgeschlagene Schritte und betroffene Aufgaben/Termine.
- Welche Daten bereits erfolgreich übernommen wurden.
- Letzten erfolgreichen Abruf und gegebenenfalls veraltete Daten.
- Sinnvolle nächste Schritte, etwa Internet prüfen oder Anmeldung erneuern.

Im Erfolgsfall keine Browserseite öffnen. Pro Durchlauf Fehler gesammelt nach Abschluss der begrenzten Wiederholungen anzeigen, nicht bei jedem Einzelversuch einen Tab öffnen. Wiederholte identische Fehler am selben Tag sollen keine Tab-Flut erzeugen; den gespeicherten Bericht trotzdem aktualisieren.

Berichte lokal und ohne externe Skripte, Fonts oder sonstige Pflichtverbindungen erzeugen. Fremde Texte HTML-escapen, Links validieren und keine fremden Skripte/HTML-Inhalte ausführen. Passwörter, Tokens, Cookies und geheime URL-Parameter dürfen weder im Bericht noch in Logs erscheinen. Falls das Öffnen scheitert, den Bericht erhalten und seinen Speicherort protokollieren. Berichte und Logs begrenzt aufbewahren.

**9. Umsetzung und Einrichtung**

Halte die Lösung für einen einzelnen Windows-PC einfach:
- Unterstützte aktuelle Python-Version; saubere Abhängigkeiten.
- Getrennte Module für Quelladapter, Normalisierung, Synchronisation, Google-Zugriffe, Zustand, Konfiguration und Berichte.
- SQLite sowie korrekte Zeitzonenbehandlung; benötigte Zeitzonendaten auch unter Windows mitliefern.
- Offizielle Google-Clientbibliotheken. HTTP-Bibliothek und Browser-Automation nur nach dem tatsächlichen Bedarf.
- Passwörter und OAuth-Tokens geschützt benutzerbezogen speichern, beispielsweise über Windows Credential Manager oder DPAPI. Keine Geheimnisse im Quellcode, Repository oder Beispieldateien.
- Lesbare Konfiguration für Quellen, Ziel-IDs, Vorlaufzeiten und Wiederholungen; Zugangsdaten erst bei der lokalen Einrichtung erfassen.
- Keine kostenpflichtige Infrastruktur und keine KI-Dienste voraussetzen.

Liefere Funktionen zum Einrichten, normalen automatischen Start, manuellen Start, Prüfen der Verbindung und für eine Vorschau ohne Google-Schreibzugriffe. Eine Vorschau darf keine Synchronisation als erfolgreich verbuchen und keine später erforderlichen Schreibvorgänge unterdrücken.

Liefere vollständigen Quellcode, Abhängigkeiten, Beispielkonfiguration ohne Geheimnisse, Tests, Autostart-Einrichtung/-Entfernung und eine deutsche README. Ein kleines lokales Setup genügt; eine umfangreiche Bedienoberfläche ist nicht verlangt.

**10. Tests und Fertigstellung**

Prüfe die wesentlichen Verhaltensregeln mit aussagekräftigen Tests und realistischen, anonymisierten Beispieldaten:
- Zweimal dieselben Daten erzeugen keine Duplikate.
- Mehrere Windows-Anmeldungen am selben Tag und erneuter Versuch nach Fehler.
- Teilfehler je Quelle/Ziel sowie Prozessabbruch nach einem Google-Schreibvorgang.
- Fristverschiebung, Absage und Reaktivierung.
- Erledigte Tasks und persönliche Notizen bleiben erhalten.
- 3-/7-Tage-Vorlauf, verspätete Entdeckung und unveränderte überfällige Tasks.
- Zeitumstellung, fehlende Uhrzeiten und Grenzen des 21-Tage-Fensters.
- Unvollständiger Abruf führt nicht zu Löschungen.
- Kalender „Schule“ nicht verfügbar: kein Ausweichen auf den Hauptkalender.
- HTML-Bericht öffnet im Benutzerkontext, maskiert fremdes HTML und enthält keine Geheimnisse.
- Vorschau schreibt weder bei Google noch einen falschen Erfolgsstatus.

Arbeite bis zur funktionsfähigen Umsetzung, soweit die nötigen Zugänge vorhanden sind. Prüfe zuerst die tatsächlichen Datenzugriffe und baue danach die Synchronisation. Nutze bei fehlenden Zugängen austauschbare Adapter und Testdaten für die unabhängigen Teile; melde genau, welche lokale Einrichtung oder reale Prüfung noch fehlt. Gib Platzhalter oder ungetestete Integrationen niemals als fertige Anbindung aus.

Zum Abschluss erkläre kurz, was funktioniert, wie es getestet wurde, wie ich es einrichte und starte und welche echten Einschränkungen gegebenenfalls verbleiben.
