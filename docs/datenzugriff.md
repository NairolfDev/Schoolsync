# Datenzugriff auf Eduvidual und WebUntis – Überblick

Genaue Berichte: [Eduvidual](datenzugriff_eduvidual.md) · [WebUntis](datenzugriff_webuntis.md)

| Aussage | Stand |
|---|---|
| Beide Plattformen sind ohne Anmeldung per HTTPS erreichbar (Stand 04.10.2026) | **real geprüft** |
| Eduvidual: Webservices und mobiler Webservice aktiv, `login/token.php` vorhanden, Login-Formular mit `logintoken`, kein Captcha, keine MFA | **real geprüft** (öffentliche Konfiguration) |
| WebUntis: Login-Formular verfügbar, JSON-RPC aktiv, `/api/homeworks/lessons`, `/api/exams`, `/api/token/new` verlangen eine Sitzung | **real geprüft** (ohne Anmeldung) |
| Anmeldung mit Benutzername/Passwort, Antwortformen, Rechte der Schülerrolle, Vollständigkeit der Listen | **nicht geprüft** – nur aus Dokumentation/Quellcode abgeleitet, getestet gegen synthetische Daten |

Die echte Prüfung ist `schulsync check` auf Ihrem PC. Es zeigt je Quelle und Kategorie, was wirklich funktioniert.
Ein Browser-Adapter (Playwright) ist **nicht** eingebaut; er wäre der nächste Schritt, falls `check` zeigt, dass ein Weg real nicht geht.
