"""Tests for the demo calendar seed generator.

Live-DB seeding is exercised on container startup; here we pin the
pure generator: it produces a deterministic, well-formed,
within-window event set with the perimeter stamp on every row.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from ownchart.core.demo_calendar_seed import (
    _HISTORY_DAYS,
    _generate_events,
)


@pytest.fixture
def anchor() -> date:
    return date(2026, 5, 26)


@pytest.fixture
def ids():
    return uuid.uuid4(), uuid.uuid4()  # source_id, record_id


def test_generator_is_deterministic(anchor, ids):
    source_id, record_id = ids
    a = _generate_events(source_id, record_id, anchor)
    b = _generate_events(source_id, record_id, anchor)
    assert len(a) == len(b)
    assert [(e.external_id, e.start_at) for e in a] == [
        (e.external_id, e.start_at) for e in b
    ]


def test_generator_covers_two_year_window(anchor, ids):
    source_id, record_id = ids
    events = _generate_events(source_id, record_id, anchor)
    assert events, "should produce non-empty calendar"

    starts = [e.start_at.date() for e in events]
    window_start = anchor - timedelta(days=_HISTORY_DAYS)
    window_end = anchor
    assert min(starts) >= window_start, (
        f"earliest event {min(starts)} predates window start {window_start}"
    )
    assert max(starts) <= window_end, (
        f"latest event {max(starts)} exceeds window end {window_end}"
    )
    # Plausible density: should cover at least 18 of the 24 months.
    months_seen = {(d.year, d.month) for d in starts}
    assert len(months_seen) >= 18, f"only {len(months_seen)} months represented"


def test_every_event_stamps_perimeter(anchor, ids):
    source_id, record_id = ids
    events = _generate_events(source_id, record_id, anchor)
    for e in events:
        assert e.person_record_id == record_id, (
            "every demo calendar event must stamp the record perimeter"
        )
        assert e.calendar_source_id == source_id
        assert e.privacy_mode_applied == "full_details"
        assert e.tombstoned_at is None
        assert e.raw_metadata and e.raw_metadata.get("demo_seed") is True


def test_external_ids_are_unique(anchor, ids):
    source_id, record_id = ids
    events = _generate_events(source_id, record_id, anchor)
    keys = [e.external_id for e in events]
    assert len(keys) == len(set(keys)), "external_id must be unique within a source"


def test_event_count_in_expected_band(anchor, ids):
    source_id, record_id = ids
    events = _generate_events(source_id, record_id, anchor)
    # Loose band — generator targets ~600-1500 events over 2 years
    # depending on travel block lengths + RNG one-off draws.
    assert 400 <= len(events) <= 2000, f"unexpected event count {len(events)}"


def test_category_mix_includes_required_buckets(anchor, ids):
    source_id, record_id = ids
    events = _generate_events(source_id, record_id, anchor)
    categories = {e.raw_metadata["category"] for e in events}
    # Nick's brief: work, travel, vacations, life events.
    assert "work" in categories
    assert any(c.startswith("travel") or c in ("travel", "work-travel", "life-travel") for c in categories)
    assert "holiday" in categories  # winter holiday / Thanksgiving block
    assert "life" in categories
