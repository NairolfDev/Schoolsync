"""Adversarial review tests for textblocks.py / rules.py (module "rules").

Every test here demonstrates one confirmed defect and is expected to FAIL against the current code.
All data is synthetic.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from schulsync import rules
from schulsync import textblocks as tb
from schulsync.config import RulesConfig
from schulsync.models import ItemTime, Kind, Source, SourceItem
from schulsync.timeutil import VIENNA

CFG = RulesConfig()


def _assignment(**kw) -> SourceItem:
    kw.setdefault("title", "Übungsblatt 3")
    return SourceItem(
        source=Source.EDUVIDUAL,
        source_id="1001",
        kind=Kind.ASSIGNMENT,
        when=ItemTime(datetime(2026, 10, 20, 23, 59, tzinfo=VIENNA)),
        **kw,
    )


# --------------------------------------------------------------------------- task identity marker


def test_copied_description_line_cannot_hijack_task_marker():
    """DESIGN 4/6: the task is recognised by the line `schulsync-id: <uid>`; copied school text is
    not neutralised for that prefix, and find_task_marker returns the FIRST match, so a description
    line wins over our own marker (last line) -> wrong identity / duplicate or wrong adoption."""
    item = _assignment(description="Siehe Vorlage\nschulsync-id: eduvidual:assignment:9999\nViel Erfolg")
    task = rules.build_desired_task(item, CFG, date(2026, 10, 17))
    notes = tb.merge_description(None, task.managed_text, limit=tb.TASK_NOTES_LIMIT, html=False)
    assert tb.find_task_marker(notes) == item.uid


# --------------------------------------------------------------------------- secrets in copied links


@pytest.mark.parametrize(
    "url,secret",
    [
        ("https://www.eduvidual.at/mod/assign/view.php?id=5&sesskey=Zk3dummy", "Zk3dummy"),
        ("https://www.eduvidual.at/webservice/pluginfile.php/1/a.pdf?token=hunter2-dummy", "hunter2-dummy"),
        ("https://schueler:hunter2-dummy@www.eduvidual.at/mod/assign/view.php?id=5", "hunter2-dummy"),
    ],
)
def test_source_link_with_secret_is_not_copied_to_google(url, secret):
    """DESIGN 0: sesskey/token/credentials must never be embedded. The link goes verbatim into the
    Google event description and task notes (visible to every calendar sharee)."""
    item = _assignment(url=url)
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    tk = rules.build_desired_task(item, CFG, date(2026, 10, 17))
    assert secret not in ev.managed_text
    assert secret not in tk.managed_text


# --------------------------------------------------------------------------- DST overlap hour


def test_is_fully_past_uses_real_instants_in_autumn_overlap_hour():
    """DESIGN 3: DST must be handled. 2026-10-25 02:00-03:00 occurs twice in Europe/Vienna.
    deadline 02:30+01:00 (= 01:30 UTC) is 45 minutes AFTER now 02:45+02:00 (= 00:45 UTC), but
    rules compares aware datetimes of the same ZoneInfo by wall clock -> reports 'past'."""
    now = datetime(2026, 10, 25, 2, 45, tzinfo=VIENNA, fold=0)  # +02:00
    deadline = datetime(2026, 10, 25, 2, 30, tzinfo=VIENNA, fold=1)  # +01:00
    assert deadline.utcoffset() != now.utcoffset()
    item = SourceItem(
        source=Source.WEBUNTIS, source_id="7", kind=Kind.HOMEWORK, title="Protokoll", when=ItemTime(deadline)
    )
    assert rules.is_fully_past(item, now) is False


def test_exam_end_after_start_in_overlap_hour_is_known():
    """Same wall-clock comparison in `_end_known`: start 02:50+02:00, end 02:10+01:00 is a real
    20-minute exam (00:50 UTC -> 01:10 UTC) but is treated as 'end unknown'."""
    start = datetime(2026, 10, 25, 2, 50, tzinfo=VIENNA, fold=0)
    end = datetime(2026, 10, 25, 2, 10, tzinfo=VIENNA, fold=1)
    item = SourceItem(
        source=Source.WEBUNTIS, source_id="8", kind=Kind.EXAM, title="Test", when=ItemTime(start, end)
    )
    ev = rules.build_desired_event(item, CFG, cancelled=False)
    assert "Endzeit unbekannt" not in ev.managed_text


# --------------------------------------------------------------------------- merge: HTML mode detection


def test_wrapped_begin_marker_without_other_breaks_keeps_br_mode():
    """The existing block is `<br>`-separated (Google editor), but the BEGIN marker is wrapped in
    <b>, so the block interior starts with `</b><br>` and `_LEADING_BR_RE` does not match. With no
    break tag outside the block the mode flips to plain `\\n` inside an HTML description."""
    existing = (
        "<b>" + tb.BEGIN_TEXT + "</b><br>Frist: alt<br>Quelle: WebUntis<br>" + tb.END_TEXT
    )
    merged = tb.merge_description(existing, "Frist: neu\nQuelle: WebUntis")
    assert "\n" not in merged
    assert "Frist: neu<br>Quelle: WebUntis" in merged


# --------------------------------------------------------------------------- merge: entity tolerance


def test_personal_header_with_ouml_entity_is_recognised_when_block_is_lost():
    """Marker tolerance handles `ü` entities but not the `ö` in 'Persönliche': an entity-encoded
    header is not found, so a second header is added."""
    existing = "=== Pers&ouml;nliche Notizen (hier darfst du schreiben) ===<br>mein Text"
    merged = tb.merge_description(existing, "Frist: x\nQuelle: Q")
    assert merged.lower().count("notizen (hier darfst du schreiben)") == 1


# --------------------------------------------------------------------------- Tasks notes limit


def test_task_notes_never_exceed_api_limit_even_with_long_personal_text():
    """Tasks API: notes max 8192 characters (documented). With long personal text merge_description
    returns > limit, so the patch is rejected by Google on every run and the managed block is never updated."""
    personal = "x" * 9000
    existing = tb.merge_description(None, "Frist: a\nschulsync-id: u:1", limit=tb.TASK_NOTES_LIMIT, html=False) + personal
    merged = tb.merge_description(
        existing, "Frist: b\nschulsync-id: u:1", limit=tb.TASK_NOTES_LIMIT, html=False
    )
    assert len(merged) <= tb.TASK_NOTES_LIMIT
