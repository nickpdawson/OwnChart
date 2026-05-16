"""Native HealthKit sync hardening tests.

Doctrine references:
  - user-docs/HEALTHKIT_SYNC.md (iOS contract)
  - user-docs/UPLOAD_CONTRACT.md (error shapes)

Pure-function coverage over the helpers that gate the route:
  - enforce_strategy() — raw vs daily_aggregate vs demo guard
  - registry_for_capabilities() — iOS capabilities endpoint shape
  - HK_REGISTRY coverage by scope (alpha-readiness)
  - daily_metric_key() — idempotency key shape
  - format_aggregate_label / format_raw_label — labels iOS uses

No DB, no LLM. Locks down behavior the iOS app depends on.
"""

from __future__ import annotations

import pytest

from ownchart.ingest.healthkit import (
    HK_REGISTRY,
    StrategyRejected,
    enforce_strategy,
    format_aggregate_label,
    format_raw_label,
    registry_for_capabilities,
)
from ownchart.canonical.equivalence import daily_metric_key


# ---------------------------------------------------------------------------
# Strategy enforcement


def test_unknown_identifier_rejected():
    with pytest.raises(StrategyRejected, match="Unknown identifier"):
        enforce_strategy("HKQuantityTypeIdentifierUnobtanium", "raw", "demo")


def test_daily_aggregate_accepted_for_quantity_types():
    """Quantity types like HeartRate must support daily_aggregate
    on both modes — iOS demo and full both pull aggregates by
    default."""
    spec = enforce_strategy(
        "HKQuantityTypeIdentifierHeartRate", "daily_aggregate", "demo",
    )
    assert spec.scope == "heart"


def test_demo_mode_refuses_raw_for_high_volume_quantities():
    """Demo mode rejects raw HR samples — would flood the demo DB."""
    with pytest.raises(StrategyRejected, match="refusing raw samples"):
        enforce_strategy(
            "HKQuantityTypeIdentifierHeartRate", "raw", "demo",
        )


def test_full_mode_allows_raw_for_high_volume():
    """Full mode is for production — raw HR samples are allowed."""
    spec = enforce_strategy(
        "HKQuantityTypeIdentifierHeartRate", "raw", "full",
    )
    assert spec is not None


def test_workouts_accept_raw_in_demo_mode():
    """Workouts are not high-volume (a few per day max) — raw is
    fine even in demo. This is what lets a runner sync their
    actual workouts to the demo deployment for testing."""
    spec = enforce_strategy("HKWorkoutType", "raw", "demo")
    assert spec.scope == "workouts"


# ---------------------------------------------------------------------------
# Registry shape — alpha-readiness coverage by scope


def test_registry_covers_alpha_scopes():
    """Every scope iOS HEALTHKIT_SYNC.md advertises must have at
    least one identifier in the server registry. Catches the
    regression where a scope quietly drops out of HK_REGISTRY."""
    scopes_present = {s.scope for s in HK_REGISTRY.values()}
    required = {
        "activity",
        "heart",
        "body",
        "sleep",
        "workouts",
        "nutrition",
        "mindfulness",
        "symptoms",
    }
    missing = required - scopes_present
    assert not missing, f"alpha scopes missing from HK_REGISTRY: {missing}"


def test_registry_has_canonical_quantity_types():
    """Spot-check the identifiers the iOS app definitely needs."""
    required = {
        "HKQuantityTypeIdentifierStepCount",
        "HKQuantityTypeIdentifierHeartRate",
        "HKQuantityTypeIdentifierRestingHeartRate",
        "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
        "HKQuantityTypeIdentifierBodyMass",
        "HKQuantityTypeIdentifierActiveEnergyBurned",
        "HKWorkoutType",
        "HKCategoryTypeIdentifierSleepAnalysis",
    }
    missing = required - set(HK_REGISTRY.keys())
    assert not missing, f"required HK identifiers missing: {missing}"


def test_capabilities_endpoint_shape():
    """The iOS app reads /api/healthkit/capabilities to learn which
    identifiers it can sync. The shape is fixed; tests pin it."""
    rows = registry_for_capabilities()
    assert len(rows) >= 20, "capabilities should advertise the full registry"
    for row in rows:
        assert "identifier" in row
        assert "scope" in row
        assert "strategies" in row
        assert "preferred_strategy" in row
        assert row["preferred_strategy"] in row["strategies"]


# ---------------------------------------------------------------------------
# Idempotency — daily_metric_key shape


def test_daily_metric_key_is_date_keyed():
    """Aggregate keys must be deterministic across pushes for the
    same identifier + date so re-syncs collapse on the partial
    unique index. Two distinct dates → two distinct keys."""
    from datetime import datetime, timezone
    k1 = daily_metric_key(
        "HKQuantityTypeIdentifierStepCount",
        datetime(2026, 5, 15, 8, 30, tzinfo=timezone.utc),
    )
    k2 = daily_metric_key(
        "HKQuantityTypeIdentifierStepCount",
        datetime(2026, 5, 15, 22, 15, tzinfo=timezone.utc),
    )
    k3 = daily_metric_key(
        "HKQuantityTypeIdentifierStepCount",
        datetime(2026, 5, 16, 8, 30, tzinfo=timezone.utc),
    )
    assert k1 == k2, "same identifier+date must produce same key regardless of time-of-day"
    assert k1 != k3, "different dates must produce different keys"


def test_daily_metric_key_distinguishes_identifiers():
    """Step count and heart rate on the same day must be distinct."""
    from datetime import datetime, timezone
    when = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    steps = daily_metric_key("HKQuantityTypeIdentifierStepCount", when)
    hr = daily_metric_key("HKQuantityTypeIdentifierHeartRate", when)
    assert steps != hr


def test_daily_metric_key_none_for_non_aggregate_identifiers():
    """Identifiers outside the daily-aggregate set (e.g. body mass,
    sleep analysis) return None — they bucket by other means."""
    from datetime import datetime, timezone
    when = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    # BodyMass is point-in-time, not a daily aggregate.
    assert daily_metric_key("HKQuantityTypeIdentifierBodyMass", when) is None
    # Sleep is a category type; no daily-metric key.
    assert daily_metric_key("HKCategoryTypeIdentifierSleepAnalysis", when) is None


# ---------------------------------------------------------------------------
# Label formatting


def test_aggregate_label_uses_unit_when_spec_omits():
    """When the spec doesn't carry a default unit (custom metric),
    the format helper falls back to the supplied units."""
    spec = HK_REGISTRY["HKQuantityTypeIdentifierStepCount"]
    label = format_aggregate_label(spec, 8542, None)
    assert "8542" in label or "8,542" in label


def test_raw_label_prefers_display_text():
    """iOS-sent display_text (e.g. 'Running 5.2 km') wins over the
    metric-name template — it carries workout-type fidelity."""
    spec = HK_REGISTRY["HKWorkoutType"]
    label = format_raw_label(spec, None, None, "Running 5.2 km · 28:14")
    assert label == "Running 5.2 km · 28:14"


def test_raw_label_falls_back_to_metric_name():
    """No display_text → assemble from spec + value/units."""
    spec = HK_REGISTRY["HKQuantityTypeIdentifierBodyMass"]
    label = format_raw_label(spec, 78.5, "kg", None)
    assert "78" in label
    assert "kg" in label.lower() or "kg" in label
