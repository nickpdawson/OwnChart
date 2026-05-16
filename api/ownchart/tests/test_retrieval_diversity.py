"""Retrieval tier-diversity tests.

Doctrine reference: user-docs/SOURCE_AUTHORITY_DOCTRINE.md §
"Implementation direction" — retrieval should diversify across
source types for complex questions.

We test `_ensure_tier_diversity` in isolation against synthetic
fact lists. No DB, no LLM.
"""

from __future__ import annotations

import uuid

from ownchart.retrieval.topics import _ensure_tier_diversity


class _FakeFact:
    """Stand-in for ExtractedFact with just the fields the diversity
    helper uses. Keeps the test free of model/ORM dependencies."""

    def __init__(self, tier_label: str, seq: int):
        self.id = uuid.uuid4()
        self.tier_label = tier_label
        self.seq = seq

    def __repr__(self) -> str:
        return f"<F[{self.tier_label}#{self.seq}]>"


def _annotated(facts):
    """Mirror the shape `_annotate_with_tiers` produces."""
    return [(f, f.tier_label) for f in facts]


def test_higher_tier_facts_lead_each_round():
    # Three tiers, lots of facts. Top of the result must be one
    # primary_event, then one specialist_proximate, then one
    # self_reported_history — round 1 by authority rank.
    facts = (
        [_FakeFact("primary_event", i) for i in range(5)]
        + [_FakeFact("specialist_proximate", i) for i in range(5)]
        + [_FakeFact("self_reported_history", i) for i in range(5)]
    )
    out = _ensure_tier_diversity(_annotated(facts), limit=15)
    assert out[0].tier_label == "primary_event"
    assert out[1].tier_label == "specialist_proximate"
    assert out[2].tier_label == "self_reported_history"
    # Round 2 starts again at the top tier.
    assert out[3].tier_label == "primary_event"


def test_single_tier_does_not_block_other_tiers():
    """If a single tier has 40 facts and the user's question matches
    them all, the round-robin still surfaces something from each
    other tier present in the match. Without this, date-DESC ranking
    inside one tier could crowd out every higher-authority record
    that exists for the query (the ACL retrieval failure mode)."""
    facts = (
        [_FakeFact("self_reported_history", i) for i in range(40)]
        + [_FakeFact("primary_event", 1)]
        + [_FakeFact("specialist_proximate", 1)]
    )
    out = _ensure_tier_diversity(_annotated(facts), limit=10)
    tiers = [f.tier_label for f in out]
    assert "primary_event" in tiers
    assert "specialist_proximate" in tiers
    # Specifically: the primary_event lands FIRST, before any
    # self_reported_history result.
    assert tiers[0] == "primary_event"
    assert tiers[1] == "specialist_proximate"


def test_within_tier_preserves_input_order():
    """Within a single tier the helper is stable — input order is
    significance + date + source-name-match already, and we don't
    want diversity-injection to scramble it."""
    facts = [
        _FakeFact("primary_event", 0),
        _FakeFact("primary_event", 1),
        _FakeFact("primary_event", 2),
    ]
    out = _ensure_tier_diversity(_annotated(facts), limit=3)
    assert [f.seq for f in out] == [0, 1, 2]


def test_unknown_tier_does_not_block_real_tiers():
    """Facts with tier 'unknown' should appear AFTER known tiers
    in the same authority band (treated as ehr_summary)."""
    facts = (
        [_FakeFact("unknown", i) for i in range(3)]
        + [_FakeFact("primary_event", 0)]
    )
    out = _ensure_tier_diversity(_annotated(facts), limit=4)
    # primary_event must lead.
    assert out[0].tier_label == "primary_event"


def test_limit_respected():
    facts = [_FakeFact("primary_event", i) for i in range(100)]
    out = _ensure_tier_diversity(_annotated(facts), limit=7)
    assert len(out) == 7


def test_empty_input_returns_empty():
    assert _ensure_tier_diversity([], limit=10) == []
