"""Gemeinsame Test-Fixtures. Modulspezifische Fixtures gehören in die jeweilige Testdatei."""

from __future__ import annotations

from datetime import datetime

import pytest

from schulsync.config import Paths
from schulsync.timeutil import VIENNA


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "SchulSync")
    p.ensure()
    return p


@pytest.fixture
def now() -> datetime:
    """Fester Referenzzeitpunkt: Mittwoch, 14.10.2026 08:00 Europe/Vienna (Sommerzeit endet 25.10.2026)."""
    return datetime(2026, 10, 14, 8, 0, tzinfo=VIENNA)


@pytest.fixture(autouse=True)
def _no_bundled_client_json(monkeypatch):
    """Tests must not pick up a real client_secret*.json lying in the project folder."""
    from schulsync import factories, google_auth

    monkeypatch.setattr(google_auth, "bundled_client_json", lambda: None)
    monkeypatch.setattr(factories, "bundled_client_json", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_toasts(monkeypatch):
    """Tests must never pop up a real Windows notification."""
    from schulsync import notify

    monkeypatch.setattr(notify, "toast", lambda title, text: True)
