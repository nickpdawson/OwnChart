"""HK-2 — the daily_aggregate medication dose-event PERSIST lane.

Context (2026-07-12, confirmed on device): iOS build 47+ posts a
day-aggregated medication batch to ``/api/healthkit/sync`` with
identifier ``HKMedicationDoseEventTypeIdentifierMedicationDoseEvent``,
``strategy="daily_aggregate"``, and the dedicated wire fields
(``medication_display_name`` / ``medication_display_key`` /
``dose_count`` / ``scheduled_count`` / ``as_needed_count``) plus a
stable ``client_sample_key`` (hash of display_key + local day).

Before this change the server DISCARDED those fields (original review
MED-9) and 400'd on registries lacking the identifier. Now the lane
persists ONE ``fact_type='medication'`` ExtractedFact per
(record, medication, local day), carrying the display name as the
label and the dose counters in ``raw_metadata.medication_dose``.

Pure-function + source-level coverage (no DB, no HTTP client):

  1. ``_build_medication_dose_fact_payload`` — the payload builder maps
     the wire fields to (label, coded_concepts, raw_metadata); distinct
     medications produce distinct payloads; missing
     ``medication_display_key`` raises 422.
  2. ``enforce_strategy`` accepts ``daily_aggregate`` for the medication
     identifier in full mode, REFUSES it in demo mode (PHI wall), and
     still accepts ``raw`` in both modes (the raw skip-guard, not a
     reject, is what stops old-client persistence).
  3. Source-level pins on ``_sync_healthkit_inner``: the persist branch
     is gated on ``daily_aggregate`` + the dose-event identifier, calls
     the builder, lands as ``fact_type='medication'`` /
     ``extraction_method='patient_self_report'`` via the shared helpers,
     and dedups on the per-record composite unique index
     (``person_record_id`` + ``client_sample_key`` — migration 0048), so
     re-posts collapse per-record while two records with the same key
     both persist.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from ownchart.ingest.healthkit import (
    HK_REGISTRY,
    MEDICATION_DOSE_EVENT_IDENTIFIER,
    StrategyRejected,
    enforce_strategy,
)
from ownchart.routes.healthkit_sync import (
    SyncSample,
    _build_medication_dose_fact_payload,
    _extraction_method_for,
    _fact_type_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dose_sample(
    *,
    client_sample_key: str,
    medication_display_key: str,
    medication_display_name: str | None,
    dose_count: int = 2,
    scheduled_count: int = 2,
    as_needed_count: int = 0,
    source_bundle_id: str | None = "com.apple.Health",
    hk_uuid: str | None = None,
) -> SyncSample:
    day_start = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)
    day_end = datetime(2026, 7, 10, 23, 59, tzinfo=timezone.utc)
    return SyncSample(
        client_sample_key=client_sample_key,
        start_at=day_start,
        end_at=day_end,
        source_name="Apple Health",
        source_bundle_id=source_bundle_id,
        hk_uuid=hk_uuid,
        medication_display_name=medication_display_name,
        medication_display_key=medication_display_key,
        dose_count=dose_count,
        scheduled_count=scheduled_count,
        as_needed_count=as_needed_count,
    )


# ---------------------------------------------------------------------------
# 1. Payload builder
# ---------------------------------------------------------------------------


def test_payload_carries_display_name_as_label_and_coded_key():
    """The display name (PHI, intended to be stored) becomes the fact
    label — matching ingest/auto_export.py::_emit_medication which
    stores the raw drug name. The coded medication reference
    (display_key) lands in coded_concepts, NOT the label."""
    sample = _dose_sample(
        client_sample_key="med-abc",
        medication_display_key="med:metformin:500mg:tablet",
        medication_display_name="Metformin 500 mg tablet",
    )
    label, coded, raw_med = _build_medication_dose_fact_payload(sample)
    assert label == "Metformin 500 mg tablet"
    assert coded["hkmedicationdose"] == ["med:metformin:500mg:tablet"]
    # Apple provenance rides alongside, as on the generic sample path.
    assert coded["source_bundle_id"] == ["com.apple.Health"]


def test_payload_carries_dose_counts_in_raw_metadata():
    """The three controlled counters land in raw_metadata (numeric /
    structured payload), not coded_concepts (list-of-strings retrieval
    bag)."""
    sample = _dose_sample(
        client_sample_key="med-abc",
        medication_display_key="med:metformin",
        medication_display_name="Metformin",
        dose_count=3,
        scheduled_count=2,
        as_needed_count=1,
    )
    _, coded, raw_med = _build_medication_dose_fact_payload(sample)
    assert raw_med["dose_count"] == 3
    assert raw_med["scheduled_count"] == 2
    assert raw_med["as_needed_count"] == 1
    assert raw_med["medication_display_key"] == "med:metformin"
    # Counts must NOT leak into coded_concepts.
    assert "dose_count" not in coded


def test_two_distinct_medications_produce_distinct_payloads():
    """Same day, same identifier, different medications → distinct
    labels + distinct coded keys. No collapse, no overwrite."""
    a = _dose_sample(
        client_sample_key="med-a",
        medication_display_key="med:metformin",
        medication_display_name="Metformin",
    )
    b = _dose_sample(
        client_sample_key="med-b",
        medication_display_key="med:lisinopril",
        medication_display_name="Lisinopril",
    )
    la, ca, _ = _build_medication_dose_fact_payload(a)
    lb, cb, _ = _build_medication_dose_fact_payload(b)
    assert la != lb
    assert ca["hkmedicationdose"] != cb["hkmedicationdose"]


def test_payload_falls_back_to_display_key_when_name_absent():
    """A fact must never be labelled with an empty string. If the
    display name is missing, the coded key is used as the label."""
    sample = _dose_sample(
        client_sample_key="med-abc",
        medication_display_key="med:metformin",
        medication_display_name=None,
    )
    label, coded, _ = _build_medication_dose_fact_payload(sample)
    assert label == "med:metformin"
    assert coded["hkmedicationdose"] == ["med:metformin"]


def test_payload_missing_display_key_raises_422():
    """medication_display_key is the coded identity — a medication row
    without it is meaningless, so the builder rejects with 422 naming
    the offending client_sample_key."""
    sample = _dose_sample(
        client_sample_key="med-broken",
        medication_display_key="   ",  # whitespace-only → treated empty
        medication_display_name="Mystery Drug",
    )
    with pytest.raises(HTTPException) as exc_info:
        _build_medication_dose_fact_payload(sample)
    assert exc_info.value.status_code == 422
    assert "med-broken" in exc_info.value.detail


def test_payload_omits_optional_provenance_when_absent():
    """source_bundle_id / hk_uuid are optional — the coded bag must not
    carry empty keys when iOS doesn't send them."""
    sample = _dose_sample(
        client_sample_key="med-abc",
        medication_display_key="med:metformin",
        medication_display_name="Metformin",
        source_bundle_id=None,
        hk_uuid=None,
    )
    _, coded, _ = _build_medication_dose_fact_payload(sample)
    assert coded == {"hkmedicationdose": ["med:metformin"]}


# ---------------------------------------------------------------------------
# 2. Strategy enforcement — accept full, refuse demo, raw still passes
# ---------------------------------------------------------------------------


def test_enforce_accepts_daily_aggregate_medication_in_full_mode():
    spec = enforce_strategy(
        MEDICATION_DOSE_EVENT_IDENTIFIER, "daily_aggregate", "full"
    )
    assert spec.scope == "medications"


def test_enforce_refuses_daily_aggregate_medication_in_demo_mode():
    """PHI wall: the medication daily_aggregate lane persists real
    medication names as labels; on the SHARED demo account that would
    leak PHI. Server refuses it in demo mode (belt-and-suspenders behind
    the client-side pause)."""
    with pytest.raises(StrategyRejected, match="medication display names are PHI"):
        enforce_strategy(
            MEDICATION_DOSE_EVENT_IDENTIFIER, "daily_aggregate", "demo"
        )


@pytest.mark.parametrize("mode", ["demo", "full"])
def test_enforce_still_accepts_raw_dose_events_both_modes(mode):
    """The demo PHI wall is scoped to daily_aggregate ONLY. Raw dose
    events must still pass enforcement in BOTH modes so the 8be7f7a
    raw-skip guard (skipped-200, no retry loop) handles them — a 422
    here would make build-≤46 clients retry-loop."""
    spec = enforce_strategy(MEDICATION_DOSE_EVENT_IDENTIFIER, "raw", mode)
    assert spec.identifier == MEDICATION_DOSE_EVENT_IDENTIFIER


# ---------------------------------------------------------------------------
# 3. Helper routing — medication scope → medication lane
# ---------------------------------------------------------------------------


def test_medication_scope_maps_to_medication_fact_type_and_self_report():
    """The persist branch reuses _fact_type_for / _extraction_method_for.
    The medication scope must land fact_type='medication' and
    extraction_method='patient_self_report' — the same clinical lane as
    Auto Export medications, NOT the wearable lane."""
    spec = HK_REGISTRY[MEDICATION_DOSE_EVENT_IDENTIFIER]
    assert _fact_type_for(spec) == "medication"
    assert _extraction_method_for(spec) == "patient_self_report"


# ---------------------------------------------------------------------------
# 4. Route wiring — persist branch + per-record dedup
# ---------------------------------------------------------------------------


def test_route_has_gated_persist_branch_for_daily_aggregate_doses():
    """`_sync_healthkit_inner` must gate the persist branch on the
    daily_aggregate strategy + the dose-event identifier, and route it
    through the payload builder. Source-level pin so a revert that drops
    the branch (falling back to the generic raw/aggregate label path,
    which would mislabel the fact with the raw identifier string) trips
    immediately."""
    from ownchart.routes.healthkit_sync import _sync_healthkit_inner

    src = inspect.getsource(_sync_healthkit_inner)
    assert "persist_dose_event_aggregate" in src
    assert 'body.strategy == "daily_aggregate"' in src
    assert "is_medication_dose_event_identifier(body.identifier)" in src
    assert "_build_medication_dose_fact_payload(s)" in src
    assert 'anchor_type_value = "healthkit_medication_dose"' in src


def test_persist_and_skip_branches_are_strategy_exclusive():
    """The raw skip-guard and the daily_aggregate persist branch gate on
    OPPOSITE strategies for the same identifier. Pin that the skip guard
    is raw-only and the persist branch is daily_aggregate-only so they
    can never both fire (or both miss) for one request."""
    from ownchart.routes.healthkit_sync import _sync_healthkit_inner

    src = inspect.getsource(_sync_healthkit_inner)
    skip_idx = src.index("skip_dose_event_raw = (")
    persist_idx = src.index("persist_dose_event_aggregate = (")
    # Each definition's strategy predicate sits within a few lines of it.
    skip_block = src[skip_idx:skip_idx + 160]
    persist_block = src[persist_idx:persist_idx + 200]
    assert 'body.strategy == "raw"' in skip_block
    assert 'body.strategy == "daily_aggregate"' in persist_block


def test_insert_dedups_on_per_record_composite_index():
    """Dedup + per-record isolation come from the shared insert's
    ON CONFLICT DO NOTHING over the (person_record_id, client_sample_key)
    composite partial unique index (migration 0048). Re-posting the same
    batch collapses within a record; the SAME client_sample_key on a
    DIFFERENT person_record is a distinct row. Pin the index elements so
    a refactor can't silently revert to the old global single-column
    index (cross-record data loss, CRIT-1)."""
    from ownchart.routes.healthkit_sync import _sync_healthkit_inner

    src = inspect.getsource(_sync_healthkit_inner)
    assert 'index_elements=["person_record_id", "client_sample_key"]' in src
    # The fact row is stamped with the active record id so isolation is
    # real, not just index-level.
    assert "person_record_id=ctx.active_record_id" in src
    assert "client_sample_key=s.client_sample_key" in src
