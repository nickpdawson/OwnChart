"""HealthKit workout wire + storage shape (Beta 1 M02 spec, pinned by tests).

PM-approved direction from
`Working Docs/PM_DECISION_NOTE_2026_05_17_BETA1_M01.md` §3. This module
is the contract that BE-3 pins via pure-function tests; M02
implementation will wire it into the existing `/api/healthkit/sync`
route. Until that wiring happens this module is unused at runtime —
its job today is to be the authoritative shape definition.

Wire shape (what iOS sends, what backend accepts):

  {
    "client_sample_key": "<sha256 or agg-id-date>",
    "start_at": "...",
    "end_at": "...",
    "workout_activity_type": "running",         # stable identifier
    "workout_activity_type_raw": 37,            # Apple raw value
    "duration_s": 2160.5,
    "distance_m": 8047.0,                       # optional
    "energy_kcal": 615.0,                       # optional
    "source": {                                 # nested source shape
      "name": "Apple Watch",
      "bundle_id": "com.apple.health",
      "version": "11.4.0"
    },
    "device": {                                 # optional device
      "name": "Apple Watch",
      "model": "Watch7,1",
      "manufacturer": "Apple Inc."
    },
    "metadata": { ... }                         # arbitrary sample-level
  }

Storage shape (what an ExtractedFact carries):

  coded_concepts = {
    "healthkit_identifier": "HKWorkoutType",
    "workout_activity_type": "running",
    "workout_activity_type_raw": 37,
    "source_bundle_id": "com.apple.health",
  }

  raw_metadata.healthkit = {
    "duration_s": 2160.5,
    "distance_m": 8047.0,
    "energy_kcal": 615.0,
    "source": {"name": ..., "bundle_id": ..., "version": ...},
    "device": {"name": ..., "model": ..., "manufacturer": ...},
    "sync_mode": "backfill" | "incremental",
  }

Two invariants this module enforces:
  1. Mode-agnostic: a sample fed through the transformer twice
     (representing backfill and incremental sync) produces identical
     storage output. Mode lands only in `raw_metadata.healthkit.sync_mode`.
  2. Workout types don't collapse: running, walking, cycling, skiing,
     rowing, strength, etc. each produce a distinct
     `coded_concepts.workout_activity_type` value. The "69 miles"
     anti-pattern (different workout types summed under one distance)
     fails this contract.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Wire shape (Pydantic — what iOS posts)


class HKSource(BaseModel):
    """Nested source on every HK sample. iOS reads this from the
    Apple `HKSource` of the originating object."""
    name: str = Field(..., max_length=255)
    bundle_id: str | None = Field(default=None, max_length=255)
    version: str | None = Field(default=None, max_length=64)


class HKDevice(BaseModel):
    """Optional device on a HK sample. Present for watch-captured
    samples, absent for many iPhone-only ones."""
    name: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=128)
    manufacturer: str | None = Field(default=None, max_length=128)


class HKWorkoutSample(BaseModel):
    """The PM-approved wire shape for a single HK workout sample.

    NOT yet wired into `/api/healthkit/sync` — that's M02. This
    Pydantic model is the contract M02 will implement.
    """
    client_sample_key: str = Field(..., max_length=128)
    start_at: str  # ISO datetime; matched to existing SyncSample tolerance
    end_at: str
    workout_activity_type: str = Field(..., max_length=64)
    workout_activity_type_raw: int | None = None
    duration_s: float = Field(..., ge=0)
    distance_m: float | None = Field(default=None, ge=0)
    energy_kcal: float | None = Field(default=None, ge=0)
    source: HKSource
    device: HKDevice | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Storage transformer (pure function — backfill and incremental both call it)


SyncMode = Literal["backfill", "incremental"]

# Stable workout activity types iOS sends. The Apple raw enum lives in
# HKWorkoutActivityType. This list is illustrative, not exhaustive —
# new types pass through unchanged. The contract is "iOS sends the
# stable string; backend trusts it."
_KNOWN_WORKOUT_TYPES: frozenset[str] = frozenset({
    "running", "walking", "cycling", "swimming", "downhill_skiing",
    "cross_country_skiing", "rowing", "strength_training",
    "functional_strength_training", "hiking", "yoga", "elliptical",
    "stair_climbing", "high_intensity_interval_training",
    "mixed_cardio", "core_training", "flexibility", "pilates",
    "tennis", "basketball", "soccer", "golf",
})


def workout_sample_to_fact_shape(
    sample: HKWorkoutSample | dict[str, Any],
    *,
    sync_mode: SyncMode,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Transform a wire-shape workout sample into the storage shape.

    Returns ``(coded_concepts, raw_metadata_healthkit)`` — two dicts
    the M02 sync path will persist on ``ExtractedFact``. Both keys
    documented above.

    Pure: same input → same output. ``sync_mode`` is recorded into
    ``raw_metadata.healthkit.sync_mode`` but does NOT affect anything
    else. That's the mode-agnostic invariant.

    Accepts either a parsed ``HKWorkoutSample`` or the equivalent
    dict — useful for testing the contract without going through
    Pydantic validation first.
    """
    if isinstance(sample, HKWorkoutSample):
        s: HKWorkoutSample = sample
    else:
        s = HKWorkoutSample.model_validate(sample)

    coded_concepts: dict[str, Any] = {
        "healthkit_identifier": "HKWorkoutType",
        "workout_activity_type": s.workout_activity_type,
    }
    if s.workout_activity_type_raw is not None:
        coded_concepts["workout_activity_type_raw"] = s.workout_activity_type_raw
    if s.source.bundle_id:
        coded_concepts["source_bundle_id"] = s.source.bundle_id

    raw_meta: dict[str, Any] = {
        "duration_s": s.duration_s,
        "source": s.source.model_dump(exclude_none=True),
        "sync_mode": sync_mode,
    }
    if s.distance_m is not None:
        raw_meta["distance_m"] = s.distance_m
    if s.energy_kcal is not None:
        raw_meta["energy_kcal"] = s.energy_kcal
    if s.device is not None:
        device_dump = s.device.model_dump(exclude_none=True)
        if device_dump:
            raw_meta["device"] = device_dump
    if s.metadata:
        raw_meta["sample_metadata"] = s.metadata

    return coded_concepts, raw_meta


def is_known_workout_type(activity_type: str) -> bool:
    """Helper for callers that want to flag unknown activity types
    for human review (without rejecting them — iOS / Apple may add
    new types faster than the registry tracks)."""
    return activity_type in _KNOWN_WORKOUT_TYPES
