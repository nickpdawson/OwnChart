"""BE-3 — HealthKit workout wire + storage shape (pinned for Beta 1 M02).

Source: `Working Docs/PM_DECISION_NOTE_2026_05_17_BETA1_M01.md` §3.

These tests pin the PM-approved contract for workout HealthKit
samples. They do NOT exercise the live `/api/healthkit/sync` route —
that wiring lands in M02. The contract pinned here is what M02 must
implement against.

Pure-function. No DB, no LLM, no Apple SDK. Imports the new
`ingest/healthkit_workout` module (added 2026-05-17).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ownchart.ingest.healthkit_workout import (
    HKWorkoutSample,
    HKSource,
    HKDevice,
    is_known_workout_type,
    workout_sample_to_fact_shape,
)


# ---------------------------------------------------------------------------
# Fixture: a canonical running workout in the new wire shape.

def _running_sample(**overrides) -> dict:
    base = {
        "client_sample_key": "sha256:abc123",
        "start_at": "2026-04-15T07:30:00-07:00",
        "end_at": "2026-04-15T08:06:00-07:00",
        "workout_activity_type": "running",
        "workout_activity_type_raw": 37,
        "duration_s": 2160.0,
        "distance_m": 8047.0,
        "energy_kcal": 615.0,
        "source": {
            "name": "Apple Watch",
            "bundle_id": "com.apple.health",
            "version": "11.4.0",
        },
        "device": {
            "name": "Apple Watch",
            "model": "Watch7,1",
            "manufacturer": "Apple Inc.",
        },
        "metadata": {"weather_temperature": "62F"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Wire-shape validation (Pydantic)


def test_canonical_running_sample_parses():
    """The PM-approved wire shape parses cleanly."""
    s = HKWorkoutSample.model_validate(_running_sample())
    assert s.workout_activity_type == "running"
    assert s.workout_activity_type_raw == 37
    assert s.duration_s == 2160.0
    assert s.distance_m == 8047.0
    assert s.energy_kcal == 615.0
    assert s.source.name == "Apple Watch"
    assert s.source.bundle_id == "com.apple.health"
    assert s.device is not None
    assert s.device.manufacturer == "Apple Inc."
    assert s.metadata == {"weather_temperature": "62F"}


def test_nested_source_is_required():
    """The wire shape requires the nested `source` object — flat
    `source_name` / `source_bundle_id` (today's alpha shape) is no
    longer the contract."""
    bad = _running_sample()
    del bad["source"]
    with pytest.raises(ValidationError):
        HKWorkoutSample.model_validate(bad)


def test_optional_fields_omit_cleanly():
    """Distance, energy, device, metadata are all optional. A
    strength-training workout with no distance must still parse."""
    sample = {
        "client_sample_key": "sha256:strength-1",
        "start_at": "2026-04-15T18:00:00-07:00",
        "end_at": "2026-04-15T18:45:00-07:00",
        "workout_activity_type": "strength_training",
        "workout_activity_type_raw": 50,
        "duration_s": 2700.0,
        "source": {"name": "iPhone"},
    }
    s = HKWorkoutSample.model_validate(sample)
    assert s.distance_m is None
    assert s.energy_kcal is None
    assert s.device is None


def test_negative_duration_rejected():
    """Defensive: duration_s, distance_m, energy_kcal must be ≥ 0."""
    bad = _running_sample(duration_s=-1.0)
    with pytest.raises(ValidationError):
        HKWorkoutSample.model_validate(bad)


def test_oversized_source_name_rejected():
    """Length caps prevent JSONB-stuffing attacks via long iOS bundle
    names."""
    bad = _running_sample(source={"name": "x" * 5000, "bundle_id": "y"})
    with pytest.raises(ValidationError):
        HKWorkoutSample.model_validate(bad)


# ---------------------------------------------------------------------------
# Storage-shape transformer


def test_transformer_emits_expected_coded_concepts():
    """ExtractedFact.coded_concepts carries the queryable semantic
    keys: healthkit_identifier, workout_activity_type, raw value,
    and source bundle id."""
    coded, _ = workout_sample_to_fact_shape(
        _running_sample(), sync_mode="incremental",
    )
    assert coded["healthkit_identifier"] == "HKWorkoutType"
    assert coded["workout_activity_type"] == "running"
    assert coded["workout_activity_type_raw"] == 37
    assert coded["source_bundle_id"] == "com.apple.health"


def test_transformer_emits_expected_raw_metadata_healthkit():
    """raw_metadata.healthkit carries numeric/device/source/mode."""
    _, raw = workout_sample_to_fact_shape(
        _running_sample(), sync_mode="incremental",
    )
    assert raw["duration_s"] == 2160.0
    assert raw["distance_m"] == 8047.0
    assert raw["energy_kcal"] == 615.0
    assert raw["source"]["name"] == "Apple Watch"
    assert raw["source"]["bundle_id"] == "com.apple.health"
    assert raw["device"]["manufacturer"] == "Apple Inc."
    assert raw["sync_mode"] == "incremental"
    assert raw["sample_metadata"] == {"weather_temperature": "62F"}


def test_transformer_omits_absent_optionals():
    """Strength training: no distance, no energy. Storage must NOT
    fabricate them; absence is information."""
    sample = {
        "client_sample_key": "sha256:strength-1",
        "start_at": "2026-04-15T18:00:00-07:00",
        "end_at": "2026-04-15T18:45:00-07:00",
        "workout_activity_type": "strength_training",
        "duration_s": 2700.0,
        "source": {"name": "iPhone"},
    }
    coded, raw = workout_sample_to_fact_shape(sample, sync_mode="backfill")
    assert "distance_m" not in raw
    assert "energy_kcal" not in raw
    assert "device" not in raw
    assert coded["workout_activity_type"] == "strength_training"
    assert "source_bundle_id" not in coded  # source had no bundle_id


# ---------------------------------------------------------------------------
# Mode-agnostic invariant — backfill and incremental produce the same
# shape modulo the sync_mode field. PM §3 acceptance criterion.


def test_backfill_and_incremental_produce_identical_shape():
    """Feeding the same sample through both sync modes produces
    storage output that differs ONLY in `raw_metadata.healthkit.sync_mode`.
    Everything else (coded_concepts, numeric payload, source, device,
    metadata) is byte-identical."""
    backfill_c, backfill_r = workout_sample_to_fact_shape(
        _running_sample(), sync_mode="backfill",
    )
    incr_c, incr_r = workout_sample_to_fact_shape(
        _running_sample(), sync_mode="incremental",
    )
    # coded_concepts is mode-agnostic.
    assert backfill_c == incr_c
    # raw_metadata.healthkit differs ONLY on sync_mode.
    bf = dict(backfill_r); inc = dict(incr_r)
    assert bf.pop("sync_mode") == "backfill"
    assert inc.pop("sync_mode") == "incremental"
    assert bf == inc


# ---------------------------------------------------------------------------
# Anti-pattern: workout types must NOT collapse. The "69 miles" bug
# the PM note flagged is what these guard against — running, walking,
# cycling, skiing, rowing all distinct under the contract.


def test_running_vs_cycling_distinct_keys():
    """Same distance, different activity type → different coded_concepts.
    The retrieval layer can therefore answer 'how much did I RUN?' vs
    'how much did I CYCLE?' separately."""
    run_c, run_r = workout_sample_to_fact_shape(
        _running_sample(workout_activity_type="running", distance_m=8000.0),
        sync_mode="incremental",
    )
    cyc_c, cyc_r = workout_sample_to_fact_shape(
        _running_sample(workout_activity_type="cycling", distance_m=8000.0),
        sync_mode="incremental",
    )
    assert run_c["workout_activity_type"] != cyc_c["workout_activity_type"]
    # Distance lands on both, but the activity type prevents a naive sum.
    assert run_r["distance_m"] == cyc_r["distance_m"] == 8000.0


def test_known_workout_types_all_distinct():
    """The retrieval layer relies on `workout_activity_type` being a
    stable identifier. The known set must produce distinct values."""
    types = [
        "running", "walking", "cycling", "swimming",
        "downhill_skiing", "cross_country_skiing", "rowing",
        "strength_training", "hiking", "yoga",
    ]
    seen = set()
    for t in types:
        coded, _ = workout_sample_to_fact_shape(
            _running_sample(workout_activity_type=t),
            sync_mode="incremental",
        )
        seen.add(coded["workout_activity_type"])
    assert len(seen) == len(types), "workout types must not collapse"


def test_unknown_workout_type_passes_through():
    """Apple ships new HKWorkoutActivityType values faster than we can
    catalog them. The transformer must NOT reject an unknown stable
    string — pass it through; `is_known_workout_type` lets callers
    flag for review."""
    coded, _ = workout_sample_to_fact_shape(
        _running_sample(workout_activity_type="paddleboarding"),
        sync_mode="incremental",
    )
    assert coded["workout_activity_type"] == "paddleboarding"
    assert is_known_workout_type("paddleboarding") is False
    assert is_known_workout_type("running") is True


# ---------------------------------------------------------------------------
# Source / device passthrough fidelity


def test_apple_watch_source_carried_through():
    """When the source is Apple Watch (the canonical iPhone+Watch
    paired case), `source.name`, `source.bundle_id`, and the device
    block all survive into raw_metadata.healthkit."""
    _, raw = workout_sample_to_fact_shape(
        _running_sample(), sync_mode="incremental",
    )
    assert raw["source"]["name"] == "Apple Watch"
    assert raw["device"]["name"] == "Apple Watch"
    assert raw["device"]["model"] == "Watch7,1"
    assert raw["device"]["manufacturer"] == "Apple Inc."


def test_third_party_app_source_carried_through():
    """A workout originating from Strava (or any other third-party app
    that writes to HealthKit) keeps its bundle id, so retrieval can
    distinguish 'logged by Strava' from 'logged on the watch.'"""
    sample = _running_sample(source={
        "name": "Strava",
        "bundle_id": "com.strava.stravaride",
        "version": "353.0",
    })
    coded, raw = workout_sample_to_fact_shape(sample, sync_mode="incremental")
    assert coded["source_bundle_id"] == "com.strava.stravaride"
    assert raw["source"]["name"] == "Strava"
    assert raw["source"]["version"] == "353.0"


# ---------------------------------------------------------------------------
# Storage layout location — coded_concepts and raw_metadata.healthkit
# are DISJOINT. No key appears in both buckets.


def test_storage_buckets_are_disjoint():
    """A reader of `coded_concepts` should never find numeric payload
    there; a reader of `raw_metadata.healthkit` should never find
    `workout_activity_type` there. Disjointness keeps query plans
    predictable."""
    coded, raw = workout_sample_to_fact_shape(
        _running_sample(), sync_mode="incremental",
    )
    overlap = set(coded.keys()) & set(raw.keys())
    assert not overlap, f"buckets overlap on {overlap}"
