"""Patient corrections reach every AI evidence path (audit 2026-11-10, item 5).

Pure tests over retrieval/effective.py plus the three prompt formatters.
No DB: facts and assertions are SimpleNamespace fixtures with the fields
the projection reads.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from ownchart.retrieval import effective as eff
from ownchart.llm.conversations import _evidence_block
from ownchart.routes.ask import _format_context
from ownchart.routes.topics import _format_assertions_block, _format_facts_block


def _fact(**over):
    base = dict(
        id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
        fact_type="procedure",
        label="Right knee arthroscopy",
        display_label=None,
        description="Arthroscopy of the right knee",
        date_start=datetime(2024, 3, 1, tzinfo=timezone.utc),
        date_end=None,
        date_precision="day",
        confidence=80,
        review_state="extracted",
        significance="major",
        date_provenance=None,
        historical_status=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _assert(kind, when, **over):
    base = dict(
        id=uuid.uuid4(),
        assertion_type=kind,
        created_at=datetime(2026, 1, when, tzinfo=timezone.utc),
        canonical_label=None,
        canonical_description=None,
        canonical_date_start=None,
        canonical_date_end=None,
        reason=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Merge semantics


def test_no_assertions_keeps_original_and_is_source_backed():
    f = _fact()
    ef = eff.apply_assertions(f, [])
    assert ef.label == f.label and ef.date_start == f.date_start
    assert not ef.corrected and ef.claim_class == "source_backed"
    assert ef.correction_note() is None


def test_correction_overrides_label_and_keeps_original():
    f = _fact()
    ef = eff.apply_assertions(f, [
        _assert("correct", 5, canonical_label="Left knee arthroscopy", reason="wrong side"),
    ])
    assert ef.label == "Left knee arthroscopy"
    assert ef.display_label == "Left knee arthroscopy"
    assert ef.original_label == "Right knee arthroscopy"
    assert ef.claim_class == "user_canonical"
    note = ef.correction_note()
    assert "Right knee arthroscopy" in note and "Left knee arthroscopy" in note
    assert "wrong side" in note


def test_later_date_only_correction_does_not_erase_earlier_label_fix():
    f = _fact()
    new_date = datetime(2024, 4, 9, tzinfo=timezone.utc)
    ef = eff.apply_assertions(f, [
        # deliberately out of order: the merge must sort by created_at
        _assert("correct", 9, canonical_date_start=new_date),
        _assert("correct", 2, canonical_label="Left knee arthroscopy"),
    ])
    assert ef.label == "Left knee arthroscopy"
    assert ef.date_start == new_date
    assert ef.label_changed and ef.date_changed


def test_annotation_after_correction_does_not_erase_it():
    f = _fact()
    ef = eff.apply_assertions(f, [
        _assert("correct", 1, canonical_label="Left knee arthroscopy"),
        _assert("annotate", 7, reason="PT started two weeks later"),
    ])
    assert ef.label == "Left knee arthroscopy"
    assert ef.annotations == ["PT started two weeks later"]


def test_reject_and_confirm_flags():
    f = _fact()
    assert eff.apply_assertions(f, [_assert("reject", 1)]).rejected
    c = eff.apply_assertions(f, [_assert("confirm", 1)])
    assert c.confirmed and c.claim_class == "user_canonical"
    assert "confirmed" in c.correction_note()


def test_latest_correction_wins_for_the_same_field():
    f = _fact()
    ef = eff.apply_assertions(f, [
        _assert("correct", 1, canonical_label="A"),
        _assert("correct", 3, canonical_label="B"),
        _assert("correct", 2, canonical_label="C"),
    ])
    assert ef.label == "B"


# ---------------------------------------------------------------------------
# Formatters send the corrected values, and name the original


def _corrected_pair():
    f = _fact()
    ef = eff.apply_assertions(f, [
        _assert("correct", 5, canonical_label="Left knee arthroscopy",
                canonical_date_start=datetime(2024, 4, 9, tzinfo=timezone.utc)),
    ])
    return f, ef


def test_chat_evidence_block_uses_correction():
    f, ef = _corrected_pair()
    block = _evidence_block([f], {}, {f.id: ef})
    assert "label: Left knee arthroscopy" in block
    assert "date=2024-04-09" in block
    assert "class=user_canonical" in block
    assert "original_label: Right knee arthroscopy" in block
    assert "correction:" in block


def test_chat_evidence_block_without_effective_map_is_unchanged_shape():
    f = _fact()
    block = _evidence_block([f], {})
    assert "label: Right knee arthroscopy" in block
    assert "class=source_backed" in block
    assert "correction:" not in block


def test_legacy_ask_context_uses_correction():
    f, ef = _corrected_pair()
    block = _format_context([ef])
    assert "label: Left knee arthroscopy" in block
    assert "date=2024-04-09" in block
    assert "original_label: Right knee arthroscopy" in block


def test_topic_brief_block_and_assertions_summary_use_correction():
    f, ef = _corrected_pair()
    block = _format_facts_block([ef])
    assert "label: Left knee arthroscopy" in block
    assert "date=2024-04-09" in block
    assert "original_label: Right knee arthroscopy" in block
    summary = _format_assertions_block([ef, eff.apply_assertions(_fact(), [])])
    assert summary.startswith("1 fact(s) carry the patient's corrections")
    assert "nothing extra" not in summary


def test_assertions_summary_is_honest_when_nothing_was_corrected():
    s = _format_assertions_block([eff.apply_assertions(_fact(), [])])
    assert "has not corrected" in s
