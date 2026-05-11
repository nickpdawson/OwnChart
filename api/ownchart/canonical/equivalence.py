"""Cross-source equivalence keys for ExtractedFact (docs/07 Priority 5).

The same real-world event can show up multiple times — Auto Export
and native HealthKit both reporting daily steps for 2026-05-10, two
EHRs reporting the same flu shot, a CCDA + a scanned operative note
describing the same surgery. Without a canonicalization layer the
user sees the same thing twice.

This module is the V1 source-neutral key generator: pure functions,
no DB, no surprises. The route / worker that owns the ingest path
calls these helpers at insert time and writes the result to
`ExtractedFact.equivalence_key`. Same key across rows = same event.

V1 covers the highest-overlap case: **daily aggregate metrics**.
Workouts, sleep sessions, and body-metric measurements need fuzzier
rules (overlap windows, value tolerances) and ship in Phase 2.
Medications never collapse — per docs/07 §610-625 a prescription, a
fill, a schedule, and a logged dose are different facts that belong
in a "medication era," not flattened.

Single-tenant V1: keys deliberately omit user_id. When multi-tenant
lands the key shape extends to `user-{uid}|...`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


# ---------------------------------------------------------------------------
# Auto Export name → canonical HK identifier
# ---------------------------------------------------------------------------
#
# Auto Export emits lowercase metric names (`step_count`, `heart_rate`).
# Native HealthKit uses camelCase identifiers (`HKQuantityTypeIdentifierStepCount`).
# To collapse them we map both to the same canonical form — we use the
# native HK identifier as the canonical, since `ingest/auto_export.py`
# already records the HK identifier as the second tuple element of
# `_DAILY_METRICS`.
#
# Keep this mirror small and explicit. If we add a new daily-aggregate
# metric to Auto Export, this dict gets the entry too. Drift here =
# silent dedupe failure.

AUTO_EXPORT_TO_HK_IDENTIFIER: dict[str, str] = {
    "step_count":               "HKQuantityTypeIdentifierStepCount",
    "active_energy":            "HKQuantityTypeIdentifierActiveEnergyBurned",
    "basal_energy_burned":      "HKQuantityTypeIdentifierBasalEnergyBurned",
    "apple_exercise_time":      "HKQuantityTypeIdentifierAppleExerciseTime",
    "apple_stand_time":         "HKQuantityTypeIdentifierAppleStandTime",
    "flights_climbed":          "HKQuantityTypeIdentifierFlightsClimbed",
    "walking_running_distance": "HKQuantityTypeIdentifierDistanceWalkingRunning",
    "heart_rate":               "HKQuantityTypeIdentifierHeartRate",
    # Note: resting_heart_rate, HRV, VO2 are raw on the iOS side — no
    # daily-aggregate equivalence pairing today. Add here only when both
    # sides emit daily aggregates.
}


# Identifiers that produce a daily-aggregate equivalence key. Other HK
# identifiers (workouts, sleep, body mass) get NULL for V1 — fuzzier
# matching rules are Phase 2.
_DAILY_AGGREGATE_HK_IDENTIFIERS: frozenset[str] = frozenset({
    "HKQuantityTypeIdentifierStepCount",
    "HKQuantityTypeIdentifierActiveEnergyBurned",
    "HKQuantityTypeIdentifierBasalEnergyBurned",
    "HKQuantityTypeIdentifierAppleExerciseTime",
    "HKQuantityTypeIdentifierAppleStandTime",
    "HKQuantityTypeIdentifierFlightsClimbed",
    "HKQuantityTypeIdentifierDistanceWalkingRunning",
    "HKQuantityTypeIdentifierHeartRate",
    "HKQuantityTypeIdentifierDietaryEnergyConsumed",
    "HKQuantityTypeIdentifierDietaryWater",
})


def _local_date(dt: datetime) -> date:
    """Coerce to the UTC calendar date. We bucket by UTC day so a
    sample at 23:30 PT (06:30 UTC next day) lands consistently across
    sources — the cost is users in deep negative offsets get one
    bucket-shift edge case at midnight, acceptable for V1."""
    return dt.astimezone(timezone.utc).date()


def daily_metric_key(hk_identifier: str, when: datetime) -> str | None:
    """Return the equivalence key for a daily-aggregate metric, or
    None if this identifier doesn't get a key in V1.

    Format: ``daily-metric|{hk_identifier}|{YYYY-MM-DD}``.

    Two facts with the same `(hk_identifier, UTC_date)` pair are
    considered the same real-world day's metric — even if they came
    from different sources (Auto Export, native HK, future Garmin).
    """
    if hk_identifier not in _DAILY_AGGREGATE_HK_IDENTIFIERS:
        return None
    return f"daily-metric|{hk_identifier}|{_local_date(when).isoformat()}"


def daily_metric_key_from_auto_export(name: str, when: datetime) -> str | None:
    """Helper for the Auto Export path — translates the Auto Export
    `name` to the canonical HK identifier first, then defers to
    daily_metric_key. Returns None for names without a daily-aggregate
    pairing (e.g. resting_heart_rate, sleep_analysis)."""
    hk_id = AUTO_EXPORT_TO_HK_IDENTIFIER.get(name)
    if hk_id is None:
        return None
    return daily_metric_key(hk_id, when)
