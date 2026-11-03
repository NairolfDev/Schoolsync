"""Persist consecutive error series independently of the sync database."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

_CODES = frozenset({
    'error', 'transient', 'network', 'outcome_unknown', 'auth', 'google_auth',
    'access_denied', 'not_found', 'request_rejected', 'conflict', 'schema',
    'incomplete', 'target_unavailable', 'config', 'route_unavailable',
    'eduvidual_error', 'session_expired', 'internal', 'rpc_error', 'unexpected',
    'timeout', 'run_timeout', 'run_failed', 'failed', 'uncertain', 'deferred',
})
_SERVICES = frozenset({'webuntis', 'eduvidual', 'calendar', 'tasks', 'program'})


def normalize_error(service: str, code: str) -> str:
    return f"{service if service in _SERVICES else 'program'}:{code if code in _CODES else 'other'}"


def read(paths) -> dict:
    try:
        data = json.loads((paths.home / 'retry_state.json').read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('counts'), dict):
            raise ValueError('invalid retry state')
        counts = {k: v for k, v in data['counts'].items() if isinstance(k, str) and isinstance(v, int) and v > 0}
        return {'counts': counts, 'next_retry': data.get('next_retry'),
                'last_success': data.get('last_success'), 'notified': data.get('notified', []),
                'in_progress': data.get('in_progress')}
    except FileNotFoundError:
        return {'counts': {}, 'next_retry': None, 'last_success': None, 'notified': [], 'in_progress': None}


def _write(paths, data: dict) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.retry-', dir=paths.home)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=True, sort_keys=True)
        os.replace(name, paths.home / 'retry_state.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def record(paths, keys: set[str], now: datetime) -> dict:
    data = read(paths)
    data['in_progress'] = None
    data['counts'] = {key: data['counts'].get(key, 0) + 1 for key in sorted(keys)}
    data['next_retry'] = (now.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(now.tzinfo).isoformat() if keys else None
    if not keys:
        data['last_success'] = now.isoformat()
    data['notified'] = sorted(set(data['notified']) & keys)
    _write(paths, data)
    return data


def mark_notified(paths, keys: set[str]) -> None:
    data = read(paths)
    data['notified'] = sorted(set(data['notified']) | (keys & data['counts'].keys()))
    _write(paths, data)


def begin(paths, now: datetime) -> None:
    data = read(paths)
    data['in_progress'] = now.isoformat()
    _write(paths, data)


def clear_progress(paths) -> None:
    data = read(paths)
    data['in_progress'] = None
    _write(paths, data)
