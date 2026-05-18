"""M02 Slice 2 — HealthKit workout runtime wiring tests.

Pure-function coverage of the route-side helpers that connect the
BE-3 contract (`api/ownchart/ingest/healthkit_workout.py`, pinned by
`test_healthkit_workout_shape.py`) to the live `/api/healthkit/sync`
ingest path:

  * `_build_workout_fact_payload(SyncSample, sync_mode=)` — validates
    the workout-required subset on a generic SyncSample, re-validates
    via the BE-3 HKWorkoutSample, and returns the (label, coded,
    raw_metadata.healthkit) tuple the live insert uses.
  * `_format_workout_label(coded, raw_meta)` — derives the human-
    readable label the Ask LLM sees.
  * `SyncSample` / `SyncRequest` — accept the new workout fields and
    `sync_mode` without rejecting other identifiers.
  * `_sync_healthkit_inner` source — branches on `HKWorkoutType`.

No DB, no LLM, no TestClient. The DB-side and request-flow checks
already live in `test_perimeter_external_ingest.py`; this file pins
the workout-specific wiring contract.

Two acceptance criteria from the M02 tracker §Slice 2:
  1. **Backfill / incremental parity:** the same sample fed through
     the helper twice (once each mode) produces identical storage
     output modulo `raw_metadata.healthkit.sync_mode`.
  2. **Distinct workout types don't collapse:** running, walking,
     cycling, swimming, skiing, rowing, strength_training, etc. each
     produce a distinct `coded_concepts.workout_activity_type`. The
     "69 miles" anti-pattern (different types summed under one
     distance) fails this contract by definition.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from ownchart.ingest.healthkit_workout import HKDevice, HKSource
from ownchart.routes.healthkit_sync import (
    SyncRequest,
    SyncSample,
    _build_workout_fact_payload,
    _format_workout_label,
    _sync_healthkit_inner,
)


# ---------------------------------------------------------------------------
# Helpers


def _running_sample(
    *,
    key: str = "wt-run-2026-01-15",
    distance_m: float | None = 8047.0,
    energy_kcal: float | None = 615.0,
    metadata: dict | None = None,
) -> SyncSample:
    """A canonical workout-bearing SyncSample. Tests override the
    bits they're varying."""
    return SyncSample(
        client_sample_key=key,
        start_at=datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 15, 10, 36, tzinfo=timezone.utc),
        workout_activity_type="running",
        workout_activity_type_raw=37,
        duration_s=2160.0,
        distance_m=distance_m,
        energy_kcal=energy_kcal,
        source=HKSource(
            name="Apple Watch",
            bundle_id="com.apple.health",
            version="11.4.0",
        ),
        device=HKDevice(
            name="Apple Watch",
            model="Watch7,1",
            manufacturer="Apple Inc.",
        ),
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Backfill / incremental parity — the BE-3 invariant
# carried into the live ingest path.


def test_backfill_and_incremental_produce_identical_storage_modulo_sync_mode():
    """The same workout sample fed through both modes must produce
    identical (label, coded_concepts, raw_metadata.healthkit) except
    that raw_metadata.healthkit.sync_mode reflects which mode it was."""
    sample = _running_sample()
    bf_label, bf_coded, bf_raw = _build_workout_fact_payload(
        sample, sync_mode="backfill",
    )
    inc_label, inc_coded, inc_raw = _build_workout_fact_payload(
        sample, sync_mode="incremental",
    )

    # Label is identical.
    assert bf_label == inc_label

    # coded_concepts is identical.
    assert bf_coded == inc_coded

    # raw_metadata.healthkit differs ONLY on sync_mode.
    assert bf_raw.pop("sync_mode") == "backfill"
    assert inc_raw.pop("sync_mode") == "incremental"
    assert bf_raw == inc_raw


def test_backfill_incremental_parity_when_only_duration_present():
    """Strength-training samples carry no distance/energy. Parity
    must still hold across modes — the missing-field handling is
    mode-agnostic."""
    sample = _running_sample(distance_m=None, energy_kcal=None)
    sample = sample.model_copy(
        update={"workout_activity_type": "strength_training"},
    )
    _, bf_coded, bf_raw = _build_workout_fact_payload(
        sample, sync_mode="backfill",
    )
    _, inc_coded, inc_raw = _build_workout_fact_payload(
        sample, sync_mode="incremental",
    )
    assert bf_coded == inc_coded
    assert "distance_m" not in bf_raw
    assert "energy_kcal" not in bf_raw
    assert bf_raw.pop("sync_mode") == "backfill"
    assert inc_raw.pop("sync_mode") == "incremental"
    assert bf_raw == inc_raw


# ---------------------------------------------------------------------------
# Distinct workout types don't collapse.


_DISTINCT_TYPES = [
    ("running", 37),
    ("walking", 52),
    ("cycling", 13),
    ("swimming", 46),
    ("downhill_skiing", 19),
    ("cross_country_skiing", 14),
    ("rowing", 35),
    ("strength_training", 50),
    ("hiking", 22),
    ("yoga", 57),
]


@pytest.mark.parametrize("activity, raw_code", _DISTINCT_TYPES)
def test_distinct_workout_types_each_carry_their_own_activity_type(
    activity: str, raw_code: int,
):
    """Each named activity_type must round-trip through the helper
    intact — no normalization, no collapse, no "all workouts are
    running"."""
    sample = _running_sample()
    sample = sample.model_copy(
        update={
            "workout_activity_type": activity,
            "workout_activity_type_raw": raw_code,
        }
    )
    _, coded, _ = _build_workout_fact_payload(sample, sync_mode="incremental")
    assert coded["workout_activity_type"] == activity
    assert coded["workout_activity_type_raw"] == raw_code


def test_ten_workout_types_produce_ten_distinct_coded_concepts():
    """Aggregate check: feeding ten distinct types yields ten
    distinct `(workout_activity_type, workout_activity_type_raw)`
    pairs. The "69 miles" anti-pattern (distinct types summed under
    one bucket) fails this contract."""
    activity_pairs = set()
    for activity, raw_code in _DISTINCT_TYPES:
        sample = _running_sample(key=f"wt-{activity}").model_copy(
            update={
                "workout_activity_type": activity,
                "workout_activity_type_raw": raw_code,
            }
        )
        _, coded, _ = _build_workout_fact_payload(
            sample, sync_mode="incremental",
        )
        activity_pairs.add(
            (coded["workout_activity_type"], coded["workout_activity_type_raw"])
        )
    assert len(activity_pairs) == len(_DISTINCT_TYPES)


# ---------------------------------------------------------------------------
# Field round-trip — type/duration/distance/energy/source/device


def test_coded_concepts_carries_identifying_fields():
    """coded_concepts is the small, retrieval-keyed bag. It must carry
    healthkit_identifier + workout_activity_type + optional raw enum
    + source_bundle_id (when present), and NOT the numeric payload."""
    sample = _running_sample()
    _, coded, _ = _build_workout_fact_payload(sample, sync_mode="incremental")
    assert coded["healthkit_identifier"] == "HKWorkoutType"
    assert coded["workout_activity_type"] == "running"
    assert coded["workout_activity_type_raw"] == 37
    assert coded["source_bundle_id"] == "com.apple.health"
    # Numeric payload doesn't leak into coded_concepts.
    assert "duration_s" not in coded
    assert "distance_m" not in coded
    assert "energy_kcal" not in coded


def test_raw_metadata_healthkit_carries_numeric_payload_and_source_device():
    """raw_metadata.healthkit holds the numeric data + nested source
    and device + sync_mode + sample-level metadata. This is what the
    live route writes under `raw_metadata.healthkit` on ExtractedFact."""
    sample = _running_sample(metadata={"weather": "sunny", "hr_avg": 152})
    _, _, raw = _build_workout_fact_payload(sample, sync_mode="incremental")
    assert raw["duration_s"] == 2160.0
    assert raw["distance_m"] == 8047.0
    assert raw["energy_kcal"] == 615.0
    assert raw["source"] == {
        "name": "Apple Watch",
        "bundle_id": "com.apple.health",
        "version": "11.4.0",
    }
    assert raw["device"] == {
        "name": "Apple Watch",
        "model": "Watch7,1",
        "manufacturer": "Apple Inc.",
    }
    assert raw["sync_mode"] == "incremental"
    assert raw["sample_metadata"] == {"weather": "sunny", "hr_avg": 152}


def test_raw_metadata_omits_absent_optional_fields_cleanly():
    """When distance/energy/device/metadata are absent, the keys
    don't appear (they're not stored as null) — matches BE-3."""
    sample = _running_sample(
        distance_m=None, energy_kcal=None, metadata=None,
    )
    sample = sample.model_copy(update={"device": None})
    _, _, raw = _build_workout_fact_payload(sample, sync_mode="backfill")
    assert "distance_m" not in raw
    assert "energy_kcal" not in raw
    assert "device" not in raw
    assert "sample_metadata" not in raw
    # Required keys still present.
    assert raw["duration_s"] == 2160.0
    assert raw["source"]["name"] == "Apple Watch"
    assert raw["sync_mode"] == "backfill"


# ---------------------------------------------------------------------------
# Label formatter


def test_label_includes_duration_distance_energy_when_all_present():
    sample = _running_sample()
    label, _, _ = _build_workout_fact_payload(sample, sync_mode="incremental")
    assert label == "Running — 36 min, 8.0 km, 615 kcal"


def test_label_omits_missing_distance_and_energy():
    sample = _running_sample(distance_m=None, energy_kcal=None)
    sample = sample.model_copy(
        update={"workout_activity_type": "strength_training"},
    )
    label, _, _ = _build_workout_fact_payload(sample, sync_mode="incremental")
    assert label == "Strength training — 36 min"


def test_label_capitalizes_and_humanizes_activity_type():
    coded = {"workout_activity_type": "high_intensity_interval_training"}
    raw = {"duration_s": 1200.0, "sync_mode": "incremental"}
    assert (
        _format_workout_label(coded, raw)
        == "High intensity interval training — 20 min"
    )


def test_label_falls_back_to_workout_when_activity_missing():
    """Defense in depth: even if coded somehow lacks activity_type,
    the label is non-empty."""
    coded: dict = {}
    raw = {"duration_s": 600.0, "sync_mode": "incremental"}
    assert _format_workout_label(coded, raw) == "Workout — 10 min"


# ---------------------------------------------------------------------------
# Validation — missing required workout fields → 422


def test_missing_workout_activity_type_raises_422():
    sample = _running_sample().model_copy(update={"workout_activity_type": None})
    with pytest.raises(HTTPException) as exc:
        _build_workout_fact_payload(sample, sync_mode="incremental")
    assert exc.value.status_code == 422
    assert "workout_activity_type" in str(exc.value.detail)


def test_missing_duration_raises_422():
    sample = _running_sample().model_copy(update={"duration_s": None})
    with pytest.raises(HTTPException) as exc:
        _build_workout_fact_payload(sample, sync_mode="incremental")
    assert exc.value.status_code == 422
    assert "duration_s" in str(exc.value.detail)


def test_missing_source_raises_422():
    sample = _running_sample().model_copy(update={"source": None})
    with pytest.raises(HTTPException) as exc:
        _build_workout_fact_payload(sample, sync_mode="incremental")
    assert exc.value.status_code == 422
    assert "source" in str(exc.value.detail)


def test_validation_error_names_the_offending_client_sample_key():
    """When validation fails the iOS app needs to know WHICH sample
    misbehaved so it can drop it from the outbox instead of replaying
    a poisoned batch forever."""
    sample = _running_sample(key="known-bad-sample").model_copy(
        update={"duration_s": None},
    )
    with pytest.raises(HTTPException) as exc:
        _build_workout_fact_payload(sample, sync_mode="incremental")
    assert "known-bad-sample" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# SyncSample / SyncRequest accept new fields without rejecting old shapes


def test_sync_sample_accepts_non_workout_payload_unchanged():
    """A pre-Slice-2 sample (steps aggregate, no workout fields)
    still validates — the optional workout fields default None."""
    s = SyncSample(
        client_sample_key="agg-steps-2026-01-15",
        start_at=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 15, 23, 59, 59, tzinfo=timezone.utc),
        value=8421.0,
        source_name="Apple Watch",
        source_bundle_id="com.apple.health",
    )
    assert s.workout_activity_type is None
    assert s.duration_s is None
    assert s.source is None  # nested source is None for non-workout
    assert s.metadata is None


def test_sync_request_default_sync_mode_is_incremental():
    """Old iOS builds that don't send sync_mode at all must still
    parse cleanly. Default must be 'incremental' (the conservative
    choice — backfill is the explicit signal)."""
    r = SyncRequest(
        device_id="dev-1",
        identifier="HKQuantityTypeIdentifierStepCount",
        strategy="daily_aggregate",
        unit="count",
        samples=[],
    )
    assert r.sync_mode == "incremental"


def test_sync_request_accepts_backfill_sync_mode():
    r = SyncRequest(
        device_id="dev-1",
        identifier="HKWorkoutType",
        strategy="raw",
        sync_mode="backfill",
        samples=[],
    )
    assert r.sync_mode == "backfill"


# ---------------------------------------------------------------------------
# Static inspection: workout branch wired into the route


def test_sync_inner_branches_on_hkworkouttype():
    """_sync_healthkit_inner must contain a literal HKWorkoutType
    branch that calls _build_workout_fact_payload. This catches a
    revert that silently drops Slice 2 wiring (the non-workout
    fallback would still produce 'something' from each sample, so
    only a source-level assertion will notice the regression)."""
    src = inspect.getsource(_sync_healthkit_inner)
    assert 'body.identifier == "HKWorkoutType"' in src
    assert "_build_workout_fact_payload" in src
    # The workout branch must persist raw_metadata.healthkit, not
    # leave it null.
    assert '"healthkit":' in src
    # Anchor type must distinguish workouts so the timeline lane can
    # find them.
    assert '"healthkit_workout"' in src
