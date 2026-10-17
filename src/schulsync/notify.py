"""Windows-Benachrichtigung (Toast) bei Problemen, zusätzlich zum HTML-Bericht.

Ohne zusätzliche Pakete: Windows PowerShell 5.1 zeigt den Toast. Der Text wird als Umgebungsvariable übergeben, nie in den
Befehlstext (keine Einschleusung). Best effort: Scheitert die Anzeige (z. B. "Nicht stören"), bleibt der Bericht der Weg.
Der Text enthält nie Zugangsdaten, nur eine feste deutsche Meldung.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

# AppUserModelID von Windows PowerShell: Toasts dieser ID zeigt Windows ohne eigene Registrierung an.
_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
_SCRIPT = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$t = [System.Security.SecurityElement]::Escape($env:SCHULSYNC_TOAST_TITLE)
$b = [System.Security.SecurityElement]::Escape($env:SCHULSYNC_TOAST_TEXT)
$x.LoadXml("<toast><visual><binding template='ToastText02'><text id='1'>$t</text><text id='2'>$b</text></binding></visual></toast>")
$n = [Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show($n)
""" % _APP_ID


def toast(title: str, text: str) -> bool:
    """Zeigt eine Windows-Benachrichtigung. True, wenn PowerShell sie ohne Fehler abgesetzt hat."""
    if os.name != "nt":
        return False
    env = {**os.environ, "SCHULSYNC_TOAST_TITLE": title[:100], "SCHULSYNC_TOAST_TEXT": text[:250]}
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _SCRIPT],
            env=env, capture_output=True, timeout=20, creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Benachrichtigung konnte nicht angezeigt werden: %s", type(exc).__name__)
        return False
    return done.returncode == 0
