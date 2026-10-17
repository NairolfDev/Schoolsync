SYNTHETIC, NOT RECORDED.
Alle Dateien hier sind von Hand nach den dokumentierten Strukturen (siehe Modul-Docstring von
src/schulsync/adapters/webuntis.py, Quellen PYW/SCH/STG) erfundene Beispieldaten mit Fantasienamen.
Es wurde nie gegen ein echtes WebUntis-Konto aufgezeichnet. Referenzzeitpunkt der Tests: 2026-10-14 08:00 Europe/Vienna.

jsonrpc_*.json              JSON-RPC (authenticate, getSchoolyears, getTimetable) nach python-webuntis/SchoolUtils
rest_homeworks.json         /api/homeworks/lessons  (data.homeworks + data.lessons)
rest_exams.json             /api/exams, Form 1 (data.exams, examDate + startTime/endTime)
rest_exams_iso_bare.json    /api/exams, Form 2 (bare "exams", ISO startDateTime/endDateTime) nach stundenglas
rest_app_data.json          /api/rest/view/v1/app/data
rest_timetable_entries.json REST-Stundenplan (format=3) mit CANCELLED/CHANGED/REGULAR/EVENT
rest_timetable_regular_again.json  dieselbe Stunde wie 701002, jetzt ausdruecklich REGULAR (Entfall zurueckgenommen)
