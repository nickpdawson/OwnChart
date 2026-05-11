"""Native HealthKit sync parser (PR2).

Sibling of `ingest/auto_export.py` — both write ExtractedFact rows
that look the same downstream (Auto Export label format,
`extraction_method='native_healthkit'`, fact_type='observation' or
'medication' or 'symptom' or 'workout'). The native iOS app posts
typed batches per HK identifier; this module knows the per-identifier
strategy table and the value-formatting rules.

Per Nick (2026-05-10):
  - Metric aggregation must be supported.
  - Source-neutral dedupe keys (`agg-<id>-<date>` for aggregates;
    `sha256(identifier|start|end|value)` for raw).
  - Demo mode caps batches and tags SourceDocuments.
  - No raw high-volume facts by default — server REJECTS raw for
    high-volume identifiers (HR, SpO2, steps, energy, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Identifier registry
# ---------------------------------------------------------------------------
#
# Each entry: identifier (HK constant), scope (lane on the dashboard),
# allowed strategies, the preferred one, the canonical unit, and a
# label-template for the daily-aggregate (uses Python str.format()
# with `value` and `units`). For raw-strategy identifiers (workouts,
# body mass, etc.), no aggregate template — caller formats the label.


@dataclass(frozen=True)
class HKIdentifierSpec:
    identifier: str
    # Scope names match the iOS HKScope enum exactly so /capabilities
    # responses route into the right onboarding card without translation.
    # Allowed: activity | heart | body | sleep | workouts | nutrition |
    # mindfulness | symptoms | medications | reproductive | clinical
    scope: str
    strategies: tuple[str, ...]  # subset of ("daily_aggregate", "raw")
    preferred_strategy: str
    unit: str | None
    aggregate_label: str | None  # str.format template; None when raw-only


# Aligned 1:1 with OwnChartiOS/OwnChart/OwnChart/Health/HKTypeRegistry.swift.
# Keep this list in sync with that file when iOS adds identifiers; the
# server is authoritative (the iOS app overrides its static fallback
# with /capabilities) but the iOS UI breaks if a scope card has zero
# entries because the server omitted them.
_REGISTRY_LIST: tuple[HKIdentifierSpec, ...] = (
    # ── Activity ─────────────────────────────────────────────────────────
    HKIdentifierSpec("HKQuantityTypeIdentifierStepCount", "activity",
                     ("daily_aggregate",), "daily_aggregate", "count",
                     "Daily steps: {value:,.0f}"),
    HKIdentifierSpec("HKQuantityTypeIdentifierDistanceWalkingRunning", "activity",
                     ("daily_aggregate",), "daily_aggregate", "m",
                     "Walking + running: {value:,.0f} {units}"),
    HKIdentifierSpec("HKQuantityTypeIdentifierActiveEnergyBurned", "activity",
                     ("daily_aggregate",), "daily_aggregate", "kcal",
                     "Active energy: {value:,.0f} {units}"),
    HKIdentifierSpec("HKQuantityTypeIdentifierBasalEnergyBurned", "activity",
                     ("daily_aggregate",), "daily_aggregate", "kcal",
                     "Resting energy: {value:,.0f} {units}"),
    HKIdentifierSpec("HKQuantityTypeIdentifierAppleExerciseTime", "activity",
                     ("daily_aggregate",), "daily_aggregate", "min",
                     "Exercise time: {value:.0f} {units}"),
    HKIdentifierSpec("HKQuantityTypeIdentifierAppleStandTime", "activity",
                     ("daily_aggregate",), "daily_aggregate", "min",
                     "Stand time: {value:.0f} {units}"),
    HKIdentifierSpec("HKQuantityTypeIdentifierFlightsClimbed", "activity",
                     ("daily_aggregate",), "daily_aggregate", "count",
                     "Flights climbed: {value:.0f}"),

    # ── Heart & cardio ───────────────────────────────────────────────────
    # HR is the one truly high-volume metric (continuous on Apple Watch).
    # iOS lets it be either raw or aggregate; we accept both but the
    # demo-mode guard refuses raw HR (see _HIGH_VOLUME_DEMO_GUARD below).
    HKIdentifierSpec("HKQuantityTypeIdentifierHeartRate", "heart",
                     ("daily_aggregate", "raw"), "daily_aggregate", "count/min",
                     "Heart rate: avg {value:.0f} {units}"),
    HKIdentifierSpec("HKQuantityTypeIdentifierRestingHeartRate", "heart",
                     ("raw",), "raw", "count/min", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "heart",
                     ("raw",), "raw", "ms", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierWalkingHeartRateAverage", "heart",
                     ("raw",), "raw", "count/min", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierVO2Max", "heart",
                     ("raw",), "raw", "mL/kg/min", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierBloodPressureSystolic", "heart",
                     ("raw",), "raw", "mmHg", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierBloodPressureDiastolic", "heart",
                     ("raw",), "raw", "mmHg", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierOxygenSaturation", "heart",
                     ("raw",), "raw", "%", None),
    HKIdentifierSpec("HKElectrocardiogramType", "heart",
                     ("raw",), "raw", None, None),

    # ── Body ─────────────────────────────────────────────────────────────
    HKIdentifierSpec("HKQuantityTypeIdentifierBodyMass", "body",
                     ("raw",), "raw", "kg", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierBodyMassIndex", "body",
                     ("raw",), "raw", "count", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierBodyFatPercentage", "body",
                     ("raw",), "raw", "%", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierHeight", "body",
                     ("raw",), "raw", "m", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierLeanBodyMass", "body",
                     ("raw",), "raw", "kg", None),
    HKIdentifierSpec("HKQuantityTypeIdentifierWaistCircumference", "body",
                     ("raw",), "raw", "m", None),

    # ── Sleep ────────────────────────────────────────────────────────────
    HKIdentifierSpec("HKCategoryTypeIdentifierSleepAnalysis", "sleep",
                     ("raw",), "raw", None, None),

    # ── Workouts ─────────────────────────────────────────────────────────
    # iOS uses the bare HKWorkoutType / HKWorkoutRouteType (no Identifier
    # suffix) — match that exactly so the wire envelope's identifier
    # field round-trips from iOS → server → /capabilities → iOS.
    HKIdentifierSpec("HKWorkoutType", "workouts",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKWorkoutRouteType", "workouts",
                     ("raw",), "raw", None, None),

    # ── Nutrition ────────────────────────────────────────────────────────
    HKIdentifierSpec("HKQuantityTypeIdentifierDietaryEnergyConsumed", "nutrition",
                     ("daily_aggregate",), "daily_aggregate", "kcal",
                     "Calories in: {value:,.0f} {units}"),
    HKIdentifierSpec("HKQuantityTypeIdentifierDietaryWater", "nutrition",
                     ("daily_aggregate",), "daily_aggregate", "L",
                     "Water: {value:.2f} {units}"),

    # ── Mindfulness ──────────────────────────────────────────────────────
    HKIdentifierSpec("HKCategoryTypeIdentifierMindfulSession", "mindfulness",
                     ("raw",), "raw", None, None),

    # ── Symptoms ─────────────────────────────────────────────────────────
    HKIdentifierSpec("HKCategoryTypeIdentifierHeadache", "symptoms",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKCategoryTypeIdentifierCoughing", "symptoms",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKCategoryTypeIdentifierMoodChanges", "symptoms",
                     ("raw",), "raw", None, None),

    # ── Medications ──────────────────────────────────────────────────────
    # iOS 16+ user-annotated medication doses. Keeps the existing
    # HKClinicalTypeIdentifierMedicationRecord too for FHIR-derived
    # medications that arrive via Health Records (clinical scope).
    HKIdentifierSpec("HKUserAnnotatedMedicationDose", "medications",
                     ("raw",), "raw", None, None),

    # ── Reproductive ─────────────────────────────────────────────────────
    HKIdentifierSpec("HKCategoryTypeIdentifierMenstrualFlow", "reproductive",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKQuantityTypeIdentifierBasalBodyTemperature", "reproductive",
                     ("raw",), "raw", "degC", None),

    # ── Clinical Records ─────────────────────────────────────────────────
    # iOS Health.app's Health Records — different from manually-logged
    # data. Each is its own type-id and lands in its semantic fact_type
    # (see _scope_to_fact_type in routes/healthkit_sync.py).
    HKIdentifierSpec("HKClinicalTypeIdentifierAllergyRecord", "clinical",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKClinicalTypeIdentifierConditionRecord", "clinical",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKClinicalTypeIdentifierImmunizationRecord", "clinical",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKClinicalTypeIdentifierLabResultRecord", "clinical",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKClinicalTypeIdentifierMedicationRecord", "clinical",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKClinicalTypeIdentifierProcedureRecord", "clinical",
                     ("raw",), "raw", None, None),
    HKIdentifierSpec("HKClinicalTypeIdentifierVitalSignRecord", "clinical",
                     ("raw",), "raw", None, None),
)

HK_REGISTRY: dict[str, HKIdentifierSpec] = {s.identifier: s for s in _REGISTRY_LIST}


# Identifiers we refuse to accept as `strategy=raw` while `mode=demo`.
# Heart rate, step count, and the energy streams are continuous on
# Apple Watch and would flood the DB with millions of rows from a
# single backfill. Demo mode requires daily aggregates for these; full
# mode allows raw if the user explicitly opted in via the iOS settings.
_HIGH_VOLUME_DEMO_GUARD: frozenset[str] = frozenset({
    "HKQuantityTypeIdentifierHeartRate",
    "HKQuantityTypeIdentifierStepCount",
    "HKQuantityTypeIdentifierActiveEnergyBurned",
    "HKQuantityTypeIdentifierBasalEnergyBurned",
    "HKQuantityTypeIdentifierAppleExerciseTime",
})


def registry_for_capabilities() -> list[dict]:
    """Render the registry into the /capabilities response shape."""
    return [
        {
            "identifier": s.identifier,
            "scope": s.scope,
            "strategies": list(s.strategies),
            "preferred_strategy": s.preferred_strategy,
            "unit": s.unit,
        }
        for s in HK_REGISTRY.values()
    ]


# ---------------------------------------------------------------------------
# Strategy enforcement
# ---------------------------------------------------------------------------


class StrategyRejected(Exception):
    """Server-side enforcement: raw posts for aggregate-only identifiers
    are rejected even if the iOS client tries. Demo mode tightens this
    further to aggregate-only across the board."""


def enforce_strategy(
    identifier: str,
    strategy: Literal["daily_aggregate", "raw"],
    mode: Literal["demo", "full"],
) -> HKIdentifierSpec:
    spec = HK_REGISTRY.get(identifier)
    if spec is None:
        raise StrategyRejected(f"Unknown identifier {identifier}")
    if strategy not in spec.strategies:
        raise StrategyRejected(
            f"Identifier {identifier} does not support {strategy}; "
            f"use {spec.preferred_strategy}"
        )
    if mode == "demo" and strategy == "raw" and identifier in _HIGH_VOLUME_DEMO_GUARD:
        # The truly continuous metrics — HR, steps, energy — would
        # flood the DB if a backfill posted raw samples. In demo mode
        # we refuse them even though the registry technically allows
        # raw for HR. Resting HR / HRV / VO2 / SpO2 / BP are
        # heart-scoped but low-volume; raw is fine for those.
        raise StrategyRejected(
            f"Demo mode: refusing raw samples for {identifier}; "
            f"switch to daily_aggregate (or set mode=full)."
        )
    return spec


# ---------------------------------------------------------------------------
# Label formatting
# ---------------------------------------------------------------------------


def format_aggregate_label(
    spec: HKIdentifierSpec, value: float, units: str | None
) -> str:
    if spec.aggregate_label is None:
        # Unreachable when the route enforces strategy → spec, but be
        # defensive.
        return f"{spec.identifier}: {value} {units or ''}".strip()
    return spec.aggregate_label.format(value=value, units=(units or spec.unit or ""))


def format_raw_label(
    spec: HKIdentifierSpec,
    value: float | None,
    units: str | None,
    display_text: str | None,
) -> str:
    """Label for a raw sample.

    Workouts / sleep / medications / symptoms get their displayText
    if the iOS app sent one (e.g. "Running 5.2 km", "Lisinopril 10mg").
    Quantitative raw types (BodyMass) get the metric-name template.
    """
    if display_text and display_text.strip():
        return display_text.strip()[:512]
    if value is not None:
        return f"{spec.identifier}: {value:.2f} {units or spec.unit or ''}".strip()
    return spec.identifier
