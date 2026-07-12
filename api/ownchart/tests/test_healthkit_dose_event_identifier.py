"""Identifier-governance tests for the system-emitted HealthKit
medication-dose stream (PM 2026-06-20 directive).

Two related identifier strings exist:

- Canonical (Apple, going forward):
    HKMedicationDoseEventTypeIdentifierMedicationDoseEvent

- Legacy (referenced in an earlier OwnChart doc round; never an
  actual Apple emit):
    HKCategoryTypeIdentifierMedicationDoseEvent

Posture pinned here:

  1. The canonical constant is exported and uses the correct string.
  2. The legacy string is accepted only as a read-only alias —
     ``normalize_identifier()`` maps legacy → canonical, never the
     other way. ``LEGACY_IDENTIFIER_ALIASES`` carries the legacy
     mapping.
  3. Ingest is intentionally NOT wired into HK_REGISTRY yet (PM:
     coordinate the normalized event payload with iOS before
     mapping it into facts/timeline). Both identifiers are absent
     from HK_REGISTRY.
  4. No tracked source file emits the legacy string — outbound
     surfaces (``/capabilities``, exports, ModelRun audit
     metadata, telemetry) are forbidden from carrying it. The
     audit grep below catches a future regression that writes the
     legacy string into a code path.
  5. Dose events stay excluded from the Pictal export. The Pictal
     mapper has no ``medication_dose_event`` fact_type key, AND
     the body-signal exclusion (``extraction_method=
     native_healthkit``) will catch any dose-event fact that lands
     under a different fact_type — defense in depth.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. Constants


def test_canonical_identifier_string_matches_apple_spelling():
    """The canonical identifier must be the exact Apple string. A
    typo here would cascade into all downstream payload acceptance."""
    from ownchart.ingest.healthkit import MEDICATION_DOSE_EVENT_IDENTIFIER
    assert MEDICATION_DOSE_EVENT_IDENTIFIER == (
        "HKMedicationDoseEventTypeIdentifierMedicationDoseEvent"
    )


def test_legacy_identifier_alias_maps_to_canonical():
    """The legacy string is accepted as a read-only alias only.
    Pin the mapping so a future cleanup can't accidentally drop it
    while there could still be historical payloads in flight."""
    from ownchart.ingest.healthkit import (
        LEGACY_IDENTIFIER_ALIASES,
        MEDICATION_DOSE_EVENT_IDENTIFIER,
    )
    assert (
        LEGACY_IDENTIFIER_ALIASES.get(
            "HKCategoryTypeIdentifierMedicationDoseEvent"
        )
        == MEDICATION_DOSE_EVENT_IDENTIFIER
    )


def test_alias_is_one_directional():
    """The map is legacy → canonical only. Nothing should let
    callers go canonical → legacy: the canonical name is the only
    string that leaves the system going forward."""
    from ownchart.ingest.healthkit import (
        LEGACY_IDENTIFIER_ALIASES,
        MEDICATION_DOSE_EVENT_IDENTIFIER,
    )
    values = set(LEGACY_IDENTIFIER_ALIASES.values())
    keys = set(LEGACY_IDENTIFIER_ALIASES.keys())
    # The canonical string must not also be a key in the map (an
    # alias to something else). And no canonical-side string may
    # map back to a legacy alias.
    assert MEDICATION_DOSE_EVENT_IDENTIFIER not in keys
    assert not (keys & values), (
        "alias keys and values overlap — map is no longer one-directional"
    )


# ---------------------------------------------------------------------------
# 2. normalize_identifier behavior


def test_normalize_identifier_maps_legacy_to_canonical():
    from ownchart.ingest.healthkit import (
        MEDICATION_DOSE_EVENT_IDENTIFIER,
        normalize_identifier,
    )
    assert (
        normalize_identifier("HKCategoryTypeIdentifierMedicationDoseEvent")
        == MEDICATION_DOSE_EVENT_IDENTIFIER
    )


def test_normalize_identifier_passthrough_for_canonical():
    """Canonical in → canonical out. No double-rewriting."""
    from ownchart.ingest.healthkit import (
        MEDICATION_DOSE_EVENT_IDENTIFIER,
        normalize_identifier,
    )
    assert (
        normalize_identifier(MEDICATION_DOSE_EVENT_IDENTIFIER)
        == MEDICATION_DOSE_EVENT_IDENTIFIER
    )


def test_normalize_identifier_passthrough_for_unknown():
    """Unknown identifiers are returned unchanged — the route layer
    decides whether to accept them via HK_REGISTRY membership."""
    from ownchart.ingest.healthkit import normalize_identifier
    assert (
        normalize_identifier("HKQuantityTypeIdentifierStepCount")
        == "HKQuantityTypeIdentifierStepCount"
    )
    assert normalize_identifier("not-an-hk-id") == "not-an-hk-id"


# ---------------------------------------------------------------------------
# 3. Registry posture — ingest NOT wired yet


def test_canonical_dose_event_identifier_present_in_HK_REGISTRY():
    """PM 2026-06-20 (build 45 directive): iOS now ships the explicit
    medication-data flow (`MedicationAccessFlow`) that pushes the
    canonical identifier through `/api/healthkit/sync`. The server
    must recognize it in `HK_REGISTRY` so the route stops 422-ing on
    the new shape. Scope of acceptance is identifier + wire shape;
    full ExtractedFact mapping with
    `extraction_method='native_healthkit_medication_dose'` lands in a
    follow-up release."""
    from ownchart.ingest.healthkit import (
        HK_REGISTRY,
        MEDICATION_DOSE_EVENT_IDENTIFIER,
    )
    assert MEDICATION_DOSE_EVENT_IDENTIFIER in HK_REGISTRY
    spec = HK_REGISTRY[MEDICATION_DOSE_EVENT_IDENTIFIER]
    assert spec.scope == "medications"
    assert "raw" in spec.strategies
    assert spec.preferred_strategy == "raw"


def test_legacy_dose_event_identifiers_absent_from_HK_REGISTRY():
    """Legacy spellings live ONLY in `LEGACY_IDENTIFIER_ALIASES` —
    they're accepted on inbound (so older iOS builds don't 422) but
    never emitted on outbound surfaces (`/capabilities`,
    `HK_REGISTRY.values()`, exports, telemetry). Pinning their
    absence here catches a future contributor accidentally adding
    a row that would shadow the canonical identifier."""
    from ownchart.ingest.healthkit import HK_REGISTRY
    assert "HKCategoryTypeIdentifierMedicationDoseEvent" not in HK_REGISTRY
    assert "HKUserAnnotatedMedicationDose" not in HK_REGISTRY


# ---------------------------------------------------------------------------
# 4. Audit — no tracked Python source emits the legacy string.
#
# This is the load-bearing rule: the legacy identifier may appear in
# *constants* and *aliases* (so we can accept inbound payloads under
# the wrong name), but it must never be emitted on an outbound
# surface — /capabilities, telemetry, audit, exports, etc. The
# `LEGACY_IDENTIFIER_ALIASES` declaration + the comment in
# healthkit.py are the two allowed mentions; everything else is
# considered an outbound emission and is forbidden.


def _tracked_python_files() -> list[Path]:
    api_root = Path(__file__).resolve().parents[2]  # api/ownchart/
    out: list[Path] = []
    # Identifier-governance test files intentionally reference the
    # legacy string as literal in assertions / parametrize values.
    # Skip them from the audit grep along with this file itself.
    _GOVERNANCE_TEST_SELF_SKIPS = {
        "test_healthkit_dose_event_identifier.py",
        "test_healthkit_medication_dose_wire.py",
        # HK-1 raw dose-event guard test references the legacy string as
        # a literal assertion input (proving normalize_identifier catches
        # it); it's a governance test, not an outbound emission.
        "test_healthkit_dose_event_raw_guard.py",
    }
    for p in api_root.rglob("*.py"):
        # Skip the venv if it somehow lives under ownchart/ + __pycache__.
        if "__pycache__" in p.parts or ".venv" in p.parts:
            continue
        if p.name in _GOVERNANCE_TEST_SELF_SKIPS:
            continue
        out.append(p)
    return out


_LEGACY = "HKCategoryTypeIdentifierMedicationDoseEvent"


def test_legacy_identifier_appears_only_in_governance_block():
    """Audit pass. The legacy identifier may appear only in
    ``ingest/healthkit.py`` — where it lives as a constant for the
    alias map. Anywhere else is an outbound emission and is
    forbidden."""
    offenders: list[str] = []
    for p in _tracked_python_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        if _LEGACY not in text:
            continue
        if p.name == "healthkit.py" and p.parent.name == "ingest":
            continue
        offenders.append(str(p))
    assert not offenders, (
        "legacy dose-event identifier emitted outside the governance "
        f"block in ingest/healthkit.py: {offenders}"
    )


# ---------------------------------------------------------------------------
# 5. Pictal exclusion — dose events stay out of the export.


def test_pictal_section_map_has_no_dose_event_key():
    """Pictal v1.0 has no dose-event section; the mapper must have
    no fact_type key that maps dose events into a Pictal bucket.
    If/when dose-event ingest lands, a future contributor must NOT
    add a key here without PM sign-off — Pictal explicitly excludes
    high-volume body-signal streams."""
    from ownchart.exports.mappers import _PICTAL_SECTION_FOR_FACT_TYPE
    for key in _PICTAL_SECTION_FOR_FACT_TYPE.keys():
        assert "dose" not in key.lower(), (
            f"Pictal section map carries a dose-event-shaped key "
            f"{key!r} — dose events must stay out of Pictal"
        )


def test_pictal_body_signal_wall_covers_native_healthkit():
    """Defense in depth: if a future dose-event fact lands under a
    Pictal-mapped fact_type (e.g. 'medication'), the body-signal
    wall (extraction_method=native_healthkit) must still exclude it.
    Pin that wall here so a future cleanup doesn't soften it."""
    from ownchart.exports.mappers import _BODY_SIGNAL_EXTRACTION_METHODS
    assert "native_healthkit" in _BODY_SIGNAL_EXTRACTION_METHODS, (
        "Pictal body-signal wall must include native_healthkit so "
        "dose-event facts ingested via the native path stay out of "
        "Pictal even if a future contributor maps the fact_type"
    )


# ---------------------------------------------------------------------------
# 6. Module-shape sanity


def test_governance_block_exports_are_public():
    """The constants + helper are part of the module's public
    surface — downstream code (future ingest, audit telemetry,
    docs generators) imports them by name."""
    from ownchart.ingest import healthkit as hk
    for name in (
        "MEDICATION_DOSE_EVENT_IDENTIFIER",
        "LEGACY_IDENTIFIER_ALIASES",
        "normalize_identifier",
    ):
        assert hasattr(hk, name), (
            f"ingest.healthkit must export {name!r} as part of its "
            f"public dose-event governance surface"
        )
