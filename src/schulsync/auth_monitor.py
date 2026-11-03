"""Silent Google credential checks independently of the school synchronisation."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

from .errors import GoogleAuthError, NetworkError, SchulSyncError
from .factories import build_google_services
from .google_auth import SECRET_CLIENT, bundled_client_json, load_credentials, save_credentials
from .google_client import call_api

log = logging.getLogger(__name__)
_PROMPT_INTERVAL = timedelta(hours=6)


def _verify(creds, cfg):
    """Do not replace working account configuration with a different Google account."""
    calendar, tasks = build_google_services(creds, cfg)
    primary = call_api(calendar.calendarList().get(calendarId='primary'), op='Google-Konto prüfen', retries=1)
    account = str(primary.get('id') or '').strip()
    if not account or (cfg.google.account and account.casefold() != cfg.google.account.strip().casefold()):
        raise GoogleAuthError('Das gewählte Google-Konto stimmt nicht mit der Einrichtung überein.')
    if cfg.google.calendar_id:
        call_api(calendar.calendarList().get(calendarId=cfg.google.calendar_id), op='Kalender prüfen', retries=1)
    if cfg.google.tasklist_id:
        call_api(tasks.tasklists().get(tasklist=cfg.google.tasklist_id), op='Aufgabenliste prüfen', retries=1)


def _mark_prompt(path, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.google-prompt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'last_prompt': now.isoformat()}, handle)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(paths, secrets, cfg, authorize, *, now=None):
    """Check credentials; never confuse network errors with revoked authorization."""
    now = now or datetime.now(timezone.utc)
    marker = paths.home / 'google-auth-prompt.json'
    try:
        load_credentials(secrets, timeout=cfg.retry.read_timeout)
        marker.unlink(missing_ok=True)
        return 0
    except NetworkError:
        return 0
    except GoogleAuthError as exc:
        if exc.detail != 'invalid_grant':
            log.warning('Google-Anmeldung benötigt Einrichtung oder Reparatur.')
            return 1
    try:
        if not cfg.google.calendar_id.strip() or not cfg.google.tasklist_id.strip():
            log.warning('Google-Ziele fehlen; bitte Einrichtung abschließen.')
            return 1
        client = secrets.get(SECRET_CLIENT) or bundled_client_json()
        if not client:
            log.warning('Google-Anmeldung: OAuth-Client fehlt.')
            return 1
        try:
            previous = datetime.fromisoformat(json.loads(marker.read_text(encoding='utf-8'))['last_prompt'])
            if now.astimezone(timezone.utc) - previous.astimezone(timezone.utc) < _PROMPT_INTERVAL:
                return 0
        except (FileNotFoundError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
        _mark_prompt(marker, now)
        creds = authorize(client)
        _verify(creds, cfg)
        save_credentials(creds, secrets)
        marker.unlink(missing_ok=True)
        return 0
    except (SchulSyncError, OSError):
        log.warning('Google-Anmeldung konnte nicht abgeschlossen werden.')
        return 1
