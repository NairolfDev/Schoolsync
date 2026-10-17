"""Tests for textblocks.py (managed description blocks, clipping, German date formats, task marker).

Sample data is synthetic. Behaviour against the REAL Google Calendar UI (how it rewrites edited
descriptions) is only covered by synthetic HTML fixtures modelled on the documented fact that
Event.description "can contain HTML" (Calendar API reference, Events resource).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from schulsync import textblocks as tb
from schulsync.google_types import TASK_MARKER_PREFIX
from schulsync.timeutil import VIENNA

MANAGED = "Frist: Mi., 14.10.2026 23:59\nKurs: 4AHITS Softwareentwicklung\nQuelle: Eduvidual"
NOTE = "Mein eigener Hinweis: Bibliothek nutzen & früh anfangen"


def _block(text: str = MANAGED) -> str:
    return tb.render_managed_block(text)


# --------------------------------------------------------------------------- fresh template


@pytest.mark.parametrize("existing", [None, "", "   \n "])
def test_fresh_template_has_both_sections(existing):
    out = tb.merge_description(existing, MANAGED)
    assert tb.BEGIN_TEXT in out and tb.END_TEXT in out
    assert tb.PERSONAL_HEADER in out
    assert out.index(tb.END_TEXT) < out.index(tb.PERSONAL_HEADER)
    assert "Automatisch übernommen" in tb.BEGIN_TEXT
    assert "Persönliche Notizen" in tb.PERSONAL_HEADER
    assert MANAGED in out
    assert "<br>" not in out


def test_fresh_template_html_mode_uses_br_and_escapes():
    out = tb.merge_description(None, "A & B <x>\nzweite Zeile", html=True)
    assert "A &amp; B &lt;x&gt;<br>zweite Zeile" in out
    assert "\n" not in out


# --------------------------------------------------------------------------- update keeps personal text byte for byte


def test_update_replaces_only_block_and_keeps_surroundings_bytewise():
    before = "Vorher  mit\tTab und Doppel-Leerzeichen\r\n\r\n"
    after = "\n\nNachher: äöü ß 🙂   \n"
    existing = before + _block("alt\nalt2") + after
    out = tb.merge_description(existing, MANAGED)
    assert out == before + _block(MANAGED) + after


def test_personal_notes_after_fresh_template_survive_updates():
    first = tb.merge_description(None, MANAGED)
    typed = first + NOTE
    second = tb.merge_description(typed, MANAGED.replace("14.10.", "15.10."))
    assert second.endswith(NOTE)
    assert "15.10." in second and "14.10." not in second
    assert second.count(NOTE) == 1


def test_idempotent_plain():
    once = tb.merge_description(None, MANAGED)
    assert tb.merge_description(once, MANAGED) == once
    with_notes = once + NOTE
    assert tb.merge_description(with_notes, MANAGED) == with_notes


def test_idempotent_html():
    once = tb.merge_description("Notiz<br>zweite Zeile", MANAGED)
    assert tb.merge_description(once, MANAGED) == once
    assert tb.merge_description(once, "anders") != once


def test_idempotent_when_managed_text_itself_contains_br_literal():
    managed = "Beschreibung:\nzeile mit <br> darin"
    once = tb.merge_description("plain notiz", managed)
    assert "<br>" in once  # literal text from the school, not a line break
    assert tb.merge_description(once, managed) == once


# --------------------------------------------------------------------------- damaged / duplicate blocks


def test_begin_without_end_prepends_fresh_block_and_keeps_old_text():
    existing = f"{tb.BEGIN_TEXT}\nhalb kaputt\nmeine Notiz"
    out = tb.merge_description(existing, MANAGED)
    assert out.startswith(tb.BEGIN_TEXT)
    assert out.endswith(existing)  # old text kept entirely
    assert out.count(tb.BEGIN_TEXT) == 2 and out.count(tb.END_TEXT) == 1
    assert tb.merge_description(out, MANAGED) == out  # now stable


def test_end_before_begin_prepends_fresh_block_and_keeps_old_text():
    existing = f"{tb.END_TEXT}\nText\n{tb.BEGIN_TEXT}\nnoch Text"
    out = tb.merge_description(existing, MANAGED)
    assert out.endswith(existing)
    assert out.startswith(tb.BEGIN_TEXT)
    assert tb.merge_description(out, MANAGED) == out


def test_text_without_any_block_is_kept_below_a_new_block():
    out = tb.merge_description("Wichtig: nicht vergessen", MANAGED)
    assert out.startswith(tb.BEGIN_TEXT)
    assert out.endswith("Wichtig: nicht vergessen")
    assert tb.PERSONAL_HEADER in out


def test_existing_personal_header_is_not_duplicated_when_block_is_lost():
    existing = f"{tb.PERSONAL_HEADER}\nmeine Notiz"
    out = tb.merge_description(existing, MANAGED)
    assert out.count(tb.PERSONAL_HEADER) == 1
    assert out.endswith("meine Notiz")


def test_duplicate_blocks_first_replaced_others_kept_as_personal_text():
    b2 = _block("zweiter Block bleibt")
    existing = f"{_block('alt')}\nzwischen\n{b2}\nende"
    out = tb.merge_description(existing, MANAGED)
    assert out == f"{_block(MANAGED)}\nzwischen\n{b2}\nende"
    assert tb.merge_description(out, MANAGED) == out


def test_stray_begin_before_complete_block_does_not_eat_user_text():
    existing = f"{tb.BEGIN_TEXT}\nkaputt\nMEINE WICHTIGE NOTIZ\n{_block('alt')}\nende"
    out = tb.merge_description(existing, MANAGED)
    assert "MEINE WICHTIGE NOTIZ" in out
    assert out.endswith("\nende")
    assert "alt" not in out.replace("kaputt", "")


def test_marker_text_inside_managed_text_cannot_break_the_block():
    evil = f"Beschreibung:\n{tb.END_TEXT}\nboese Zeile\n{tb.BEGIN_TEXT}"
    out = tb.merge_description("Notiz", evil)
    assert out.count(tb.END_TEXT) == 1 and out.count(tb.BEGIN_TEXT) == 1
    assert "boese Zeile" in out
    assert tb.merge_description(out, evil) == out
    assert out.endswith("Notiz")


# --------------------------------------------------------------------------- HTML tolerance


def test_markers_wrapped_in_tags_and_entities_are_recognised_and_br_mode_used():
    # What Google's web UI may store after a manual edit: <br> for newlines, markers wrapped in <b>.
    existing = (
        "<b>=== Automatisch &uuml;bernommen (wird bei jedem Abgleich ersetzt) ===</b><br>"
        "Frist: alt<br>Quelle: Eduvidual<br>"
        "<b>=== Ende des automatisch &uuml;bernommenen Bereichs ===</b>"
        "<br><br>Meine <i>Notiz</i> &amp; mehr<br>"
    )
    out = tb.merge_description(existing, "Frist: neu & wichtig\nQuelle: Eduvidual")
    assert "Frist: alt" not in out
    assert "Frist: neu &amp; wichtig<br>Quelle: Eduvidual" in out
    assert out.endswith("<br><br>Meine <i>Notiz</i> &amp; mehr<br>")
    assert out.count(tb.END_TEXT) == 1
    assert out.startswith("<b>" + tb.BEGIN_TEXT)  # wrapper outside the markers is untouched
    assert tb.merge_description(out, "Frist: neu & wichtig\nQuelle: Eduvidual") == out


def test_markers_split_by_tags_between_words_are_found():
    existing = (
        "=== Automatisch<wbr> übernommen&nbsp;(wird bei jedem Abgleich ersetzt) ===<br>alt<br>"
        "=== Ende des automatisch übernommenen Bereichs ===<br>danach"
    )
    out = tb.merge_description(existing, "neu")
    assert "alt" not in out
    assert out.endswith("<br>danach")
    assert out.count("neu") == 1


def test_html_blob_with_div_structure_is_detected_as_html():
    out = tb.merge_description("<div>Notiz</div>", "a\nb")
    assert "a<br>b" in out and out.endswith("<div>Notiz</div>")


def test_explicit_plain_mode_for_task_notes_even_with_br_in_text():
    out = tb.merge_description("Notiz<br>x", "a\nb", html=False)
    assert "a\nb" in out


def test_explicit_html_mode_overrides_autodetect():
    out = tb.merge_description("nur plain", "a\nb", html=True)
    assert "a<br>b" in out


def test_html_mode_uses_uppercase_and_selfclosing_br_detection():
    out = tb.merge_description("x<BR/>y", "a\nb")
    assert "a<br>b" in out


# --------------------------------------------------------------------------- limit / clip


def test_clip_basic():
    assert tb.clip("abc", 10) == "abc"
    assert tb.clip("abc", 3) == "abc"
    out = tb.clip("x" * 100, 40)
    assert len(out) <= 40 and out.endswith("[… gekürzt]")
    assert "gekürzt" in tb.CLIP_MARKER


def test_clip_limit_smaller_than_marker_returns_marker():
    assert tb.clip("x" * 100, 3) == tb.CLIP_MARKER


def test_merge_clips_only_managed_part_never_personal_text():
    personal = "P" * 500
    big = "Zeile\n" * 1000
    out = tb.merge_description(f"{_block('alt')}\n{personal}", big, limit=800)
    assert len(out) <= 800
    assert out.endswith(personal)
    assert tb.BEGIN_TEXT in out and tb.END_TEXT in out
    assert "[… gekürzt]" in out
    assert tb.merge_description(out, big, limit=800) == out


def test_merge_limit_html_escaping_expansion_is_accounted_for():
    out = tb.merge_description("Notiz<br>x", "&" * 2000, limit=1000)
    assert len(out) <= 1000
    assert out.count(tb.END_TEXT) == 1


def test_merge_limit_if_personal_text_alone_exceeds_limit_markers_stay_intact():
    personal = "Q" * 2000
    out = tb.merge_description(personal, "managed text", limit=1000, keep_personal=True)
    assert out.endswith(personal)
    assert tb.BEGIN_TEXT in out and tb.END_TEXT in out


def test_merge_limit_personal_text_is_clipped_by_default_so_result_fits_api_limit():
    personal = "Q" * 2000
    out = tb.merge_description(personal, "managed text", limit=1000)
    assert len(out) <= 1000
    assert tb.BEGIN_TEXT in out and tb.END_TEXT in out and out.endswith(tb.CLIP_MARKER)
    assert tb.merge_description(out, "managed text", limit=1000) == out


def test_clip_keeps_task_marker_line():
    marker = tb.task_marker_line("eduvidual:assignment:42")
    managed = "Zeile\n" * 2000 + marker
    out = tb.merge_description(None, managed, limit=500, html=False)
    assert len(out) <= 500
    assert tb.find_task_marker(out) == "eduvidual:assignment:42"


def test_short_text_not_clipped_with_default_limit():
    out = tb.merge_description(None, MANAGED)
    assert "gekürzt" not in out


# --------------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime(2026, 10, 14, 23, 59, tzinfo=VIENNA), "Mi., 14.10.2026 23:59"),
        (date(2026, 10, 14), "Mi., 14.10.2026"),
        (date(2026, 10, 12), "Mo., 12.10.2026"),
        (date(2026, 10, 13), "Di., 13.10.2026"),
        (date(2026, 10, 15), "Do., 15.10.2026"),
        (date(2026, 10, 16), "Fr., 16.10.2026"),
        (date(2026, 10, 17), "Sa., 17.10.2026"),
        (date(2026, 10, 18), "So., 18.10.2026"),
        (datetime(2026, 3, 29, 3, 0, tzinfo=VIENNA), "So., 29.03.2026 03:00"),
        (datetime(2026, 1, 5, 0, 0, tzinfo=VIENNA), "Mo., 05.01.2026 00:00"),
    ],
)
def test_format_deadline_de(value, expected):
    assert tb.format_deadline_de(value) == expected


def test_format_deadline_de_converts_to_vienna():
    # 22:30 UTC on 14.10. is already 00:30 on 15.10. in Vienna (CEST).
    assert tb.format_deadline_de(datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)) == "Do., 15.10.2026 00:30"


def test_format_deadline_de_rejects_naive_datetime():
    with pytest.raises(ValueError):
        tb.format_deadline_de(datetime(2026, 10, 14, 8, 0))


# --------------------------------------------------------------------------- task marker


def test_task_marker_roundtrip():
    line = tb.task_marker_line("webuntis:homework:9981")
    assert line == "schulsync-id: webuntis:homework:9981"
    assert line.startswith(TASK_MARKER_PREFIX)
    notes = f"Text davor\n{line}\nText danach"
    assert tb.find_task_marker(notes) == "webuntis:homework:9981"


@pytest.mark.parametrize("notes", [None, "", "kein Marker", "schulsync-id:", "xschulsync-id: a\nnix"])
def test_find_task_marker_none(notes):
    assert tb.find_task_marker(notes) is None


def test_find_task_marker_tolerates_crlf_and_whitespace():
    assert tb.find_task_marker("a\r\n  schulsync-id:   eduvidual:assignment:7  \r\nb") == "eduvidual:assignment:7"


def test_find_task_marker_inside_merged_notes_with_personal_text():
    managed = "Frist: x\n" + tb.task_marker_line("eduvidual:assignment:7")
    notes = tb.merge_description("Eigene Notiz", managed, limit=8192, html=False)
    assert tb.find_task_marker(notes) == "eduvidual:assignment:7"
    assert notes.endswith("Eigene Notiz")
