"""Wire-shape acceptance tests for the iOS 26+ medication-dose stream
(PM 2026-06-20, cb9c1f7 + follow-on).

Scope:

  1. Two same-day events with distinct iOS dedupe keys AND distinct
     ``medication_display_key`` values validate and preserve every
     load-bearing field on the wire: canonical identifier, event time
     (start_at / end_at), Apple source provenance (source +
     source_bundle_id), coded medication reference
     (medication_display_key), and controlled status counts
     (dose_count / scheduled_count / as_needed_count).

  2. The two legacy identifiers (``HKCategoryTypeIdentifierMedicationDoseEvent``
     and ``HKUserAnnotatedMedicationDose``) both normalize at the
     route's inbound boundary to the canonical identifier. The
     ``normalize_identifier`` helper is wired into
     ``_sync_healthkit_inner`` BEFORE the ``HK_REGISTRY`` membership
     check so inbound payloads with either legacy string are accepted
     (not 400'd).

  3. Neither legacy identifier appears on outbound surfaces. The
     ``/capabilities`` response built from
     ``registry_for_capabilities()`` advertises only the canonical
     identifier in the ``medications`` scope going forward; the two
     legacy strings are absent.

  4. Pictal export exclusion is intact (defense-in-depth pin alongside
     the test_healthkit_dose_event_identifier.py audit).

No DB, no LLM, no live HTTP. Pure-function pins over the wire shape
and the route's normalization wiring.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from ownchart.ingest.healthkit import (
    HK_REGISTRY,
    LEGACY_IDENTIFIER_ALIASES,
    MEDICATION_DOSE_EVENT_IDENTIFIER,
    normalize_identifier,
    registry_for_capabilities,
)
from ownchart.routes.healthkit_sync import SyncRequest, SyncSample


# ---------------------------------------------------------------------------
# Helpers


def _dose_sample(
    *,
    client_sample_key: str,
    medication_display_key: str,
    medication_display_name: str,
    dose_count: int,
    scheduled_count: int,
    as_needed_count: int,
    start_at: datetime,
    end_at: datetime,
    source_name: str = "Apple Health",
    source_bundle_id: str = "com.apple.Health",
    hk_uuid: str | None = None,
) -> SyncSample:
    """Canonical dose-event SyncSample shape. The wire fields cb9c1f7
    added populate only for the canonical medication-dose identifier;
    everything else stays None (workouts / aggregates / etc. set
    different subsets)."""
    return SyncSample(
        client_sample_key=client_sample_key,
        start_at=start_at,
        end_at=end_at,
        # Apple provenance — stored on the fact for traceability.
        source_name=source_name,
        source_bundle_id=source_bundle_id,
        hk_uuid=hk_uuid,
        # Medication-dose wire fields.
        medication_display_name=medication_display_name,
        medication_display_key=medication_display_key,
        dose_count=dose_count,
        scheduled_count=scheduled_count,
        as_needed_count=as_needed_count,
    )


def _two_same_day_dose_samples() -> tuple[SyncSample, SyncSample]:
    """Two distinct medications, same local day, same identifier.
    Each one is a (local-day, medication) tuple per the iOS contract
    in cb9c1f7. Both must validate and be carried separately by
    SyncRequest — no collapse, no overwrite."""
    day_start = datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)
    day_end = datetime(2026, 6, 18, 23, 59, tzinfo=timezone.utc)
    a = _dose_sample(
        client_sample_key="dose-2026-06-18-metformin",
        medication_display_key="med:metformin:500mg:tablet",
        medication_display_name="Metformin 500 mg tablet",
        dose_count=2,
        scheduled_count=2,
        as_needed_count=0,
        start_at=day_start,
        end_at=day_end,
        hk_uuid="11111111-aaaa-bbbb-cccc-000000000001",
    )
    b = _dose_sample(
        client_sample_key="dose-2026-06-18-lisinopril",
        medication_display_key="med:lisinopril:10mg:tablet",
        medication_display_name="Lisinopril 10 mg tablet",
        dose_count=1,
        scheduled_count=1,
        as_needed_count=0,
        start_at=day_start,
        end_at=day_end,
        hk_uuid="22222222-aaaa-bbbb-cccc-000000000002",
    )
    return a, b


# ---------------------------------------------------------------------------
# 1. Two same-day events with distinct keys


def test_sync_request_accepts_two_same_day_dose_samples():
    """SyncRequest must accept both samples in one payload — they
    share day + identifier but differ on ``client_sample_key`` and
    ``medication_display_key``. Neither field collides; both rows
    survive into ``body.samples``."""
    a, b = _two_same_day_dose_samples()
    req = SyncRequest(
        device_id="device-abc",
        identifier=MEDICATION_DOSE_EVENT_IDENTIFIER,
        strategy="raw",
        sync_mode="incremental",
        samples=[a, b],
    )
    assert len(req.samples) == 2
    keys = {s.client_sample_key for s in req.samples}
    assert keys == {
        "dose-2026-06-18-metformin",
        "dose-2026-06-18-lisinopril",
    }
    med_keys = {s.medication_display_key for s in req.samples}
    assert med_keys == {
        "med:metformin:500mg:tablet",
        "med:lisinopril:10mg:tablet",
    }


def test_canonical_identifier_preserved_on_request_envelope():
    """The wire identifier round-trips verbatim — ``SyncRequest.identifier``
    is the source of truth for which HK stream this batch belongs to.
    Two same-day samples share one envelope identifier; that's the
    canonical Apple string going forward."""
    a, b = _two_same_day_dose_samples()
    req = SyncRequest(
        device_id="device-abc",
        identifier=MEDICATION_DOSE_EVENT_IDENTIFIER,
        strategy="raw",
        samples=[a, b],
    )
    assert req.identifier == (
        "HKMedicationDoseEventTypeIdentifierMedicationDoseEvent"
    )


def test_event_time_preserved_verbatim():
    """Apple's event start / end timestamps must round-trip without
    being collapsed to noon-of-the-day or recomputed. PM directive:
    'Preserve Apple provenance and event time.'"""
    a, b = _two_same_day_dose_samples()
    assert a.start_at == datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)
    assert a.end_at == datetime(2026, 6, 18, 23, 59, tzinfo=timezone.utc)
    assert b.start_at == a.start_at  # same local day
    assert b.end_at == a.end_at
    # Two separate samples; the timestamps are equal but the medication
    # identity differs. No collapse.
    assert a.medication_display_key != b.medication_display_key


def test_apple_source_provenance_preserved_verbatim():
    """``source_name`` + ``source_bundle_id`` carry Apple's
    HKSourceRevision identity. Both must survive on the wire so the
    fact downstream can attribute the dose to Apple Health and never
    mis-attribute it to a third-party app that shares HK access."""
    a, b = _two_same_day_dose_samples()
    for sample in (a, b):
        assert sample.source_name == "Apple Health"
        assert sample.source_bundle_id == "com.apple.Health"


def test_coded_medication_reference_distinct_across_samples():
    """The medication identity comes from ``medication_display_key``
    (the coded reference, not free text). PM directive: 'do not infer
    medication names from display text.' Distinct medications produce
    distinct coded keys; ``medication_display_name`` is PHI display
    only and never the source of identity."""
    a, b = _two_same_day_dose_samples()
    assert a.medication_display_key != b.medication_display_key
    # Both keys carry the controlled-vocabulary shape: the prefix is
    # the medication namespace; subsequent segments are stable across
    # a re-pair, unlike display_name which depends on locale.
    assert a.medication_display_key.startswith("med:")
    assert b.medication_display_key.startswith("med:")


def test_controlled_status_counts_preserved():
    """``dose_count`` (aggregate), ``scheduled_count`` (taken/skipped
    against a regimen slot), and ``as_needed_count`` (PRN) are the
    three controlled counters cb9c1f7 added. Pin that all three
    survive on the wire and add up consistently."""
    a, b = _two_same_day_dose_samples()
    # Metformin: 2 doses, both scheduled (b.i.d.), 0 PRN.
    assert a.dose_count == 2
    assert a.scheduled_count == 2
    assert a.as_needed_count == 0
    # Lisinopril: 1 dose, scheduled (q.d.), 0 PRN.
    assert b.dose_count == 1
    assert b.scheduled_count == 1
    assert b.as_needed_count == 0
    # Aggregate must be at least scheduled + as_needed for a sane row.
    for s in (a, b):
        assert s.dose_count >= (s.scheduled_count + s.as_needed_count)


def test_non_negative_count_constraint_enforced_by_schema():
    """Pydantic ``ge=0`` on the three count fields must reject
    negative values at the schema boundary — they have no semantic
    meaning and would otherwise propagate to downstream summation."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _dose_sample(
            client_sample_key="bad-1",
            medication_display_key="med:x:1mg",
            medication_display_name="X",
            dose_count=-1,
            scheduled_count=0,
            as_needed_count=0,
            start_at=datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 6, 18, 23, 59, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# 2. Historical aliases — normalize at the route's inbound boundary


@pytest.mark.parametrize(
    "legacy_identifier",
    [
        "HKCategoryTypeIdentifierMedicationDoseEvent",  # earlier doc-round string
        "HKUserAnnotatedMedicationDose",                # iOS 16+ surface
    ],
)
def test_both_historical_aliases_normalize_to_canonical(legacy_identifier):
    """Both historical spellings collapse to the canonical identifier
    at the inbound normalization step. Old iOS builds + old persisted
    payloads keep working without 400-ing on ``Unknown identifier``."""
    assert legacy_identifier in LEGACY_IDENTIFIER_ALIASES
    assert (
        normalize_identifier(legacy_identifier)
        == MEDICATION_DOSE_EVENT_IDENTIFIER
    )


def test_route_normalizes_identifier_before_registry_check():
    """The route handler must call ``normalize_identifier`` BEFORE the
    ``HK_REGISTRY`` membership check. Without that ordering, an
    inbound payload with a legacy identifier would fail the registry
    check (legacy strings are not in HK_REGISTRY by design) and 400
    before normalization had a chance to run. Static-source pin."""
    from ownchart.routes.healthkit_sync import _sync_healthkit_inner
    src = inspect.getsource(_sync_healthkit_inner)
    # The line must exist in this exact form so future refactors that
    # drop or reorder it trip immediately.
    assert "body.identifier = normalize_identifier(body.identifier)" in src
    # And the registry membership check must follow it textually.
    normalize_idx = src.index(
        "body.identifier = normalize_identifier(body.identifier)"
    )
    registry_idx = src.index("body.identifier not in HK_REGISTRY")
    assert normalize_idx < registry_idx, (
        "normalize_identifier must run before the HK_REGISTRY "
        "membership check; otherwise legacy-aliased payloads 400"
    )


def test_aliases_are_inbound_only_not_in_HK_REGISTRY():
    """Aliases live in LEGACY_IDENTIFIER_ALIASES, not HK_REGISTRY.
    HK_REGISTRY drives the outbound ``/capabilities`` advertisement;
    if a legacy string were also in HK_REGISTRY, it would leak to
    outbound."""
    for legacy in LEGACY_IDENTIFIER_ALIASES.keys():
        assert legacy not in HK_REGISTRY, (
            f"alias {legacy!r} must not also be in HK_REGISTRY — "
            f"that would emit the legacy string on /capabilities"
        )


# ---------------------------------------------------------------------------
# 3. /capabilities advertises only the canonical identifier


def test_capabilities_advertises_canonical_identifier():
    """The iOS app reads ``/capabilities`` to learn which streams the
    server accepts. The canonical medication-dose identifier MUST be
    advertised so iOS builds know to push the new stream."""
    rows = registry_for_capabilities()
    identifiers = {r["identifier"] for r in rows}
    assert MEDICATION_DOSE_EVENT_IDENTIFIER in identifiers


def test_capabilities_does_not_advertise_legacy_dose_identifiers():
    """Neither legacy spelling may appear in ``/capabilities`` —
    advertising them would invite iOS builds to keep emitting them
    going forward, defeating the alias-collapse posture."""
    rows = registry_for_capabilities()
    identifiers = {r["identifier"] for r in rows}
    for legacy in LEGACY_IDENTIFIER_ALIASES.keys():
        assert legacy not in identifiers, (
            f"/capabilities must not advertise legacy identifier "
            f"{legacy!r}"
        )


def test_capabilities_medication_dose_row_scope_and_strategy():
    """The advertised row for the canonical identifier must carry the
    correct scope (``medications``) and strategies. iOS uses these to
    decide which onboarding card to surface and which lane to upload on.
    The canonical (build 47+) lane is ``daily_aggregate`` — one
    day-folded sample per (medication, day) — so that's the preferred
    strategy; ``raw`` stays advertised so build-≤46 clients keep hitting
    the skip guard rather than a 400."""
    rows = registry_for_capabilities()
    by_id = {r["identifier"]: r for r in rows}
    row = by_id[MEDICATION_DOSE_EVENT_IDENTIFIER]
    assert row["scope"] == "medications"
    assert "daily_aggregate" in row["strategies"]
    assert "raw" in row["strategies"]
    assert row["preferred_strategy"] == "daily_aggregate"


# ---------------------------------------------------------------------------
# 4. Pictal exclusion intact (companion to the audit in
# test_healthkit_dose_event_identifier.py)


def test_pictal_body_signal_wall_still_includes_native_healthkit():
    """The Pictal mapper's body-signal wall is the final defense
    keeping dose-event facts out of Pictal once mapping lands.
    ``native_healthkit`` must remain in
    ``_BODY_SIGNAL_EXTRACTION_METHODS``; a future cleanup that
    softens this would silently start dumping dose-event rows into
    the Pictal export."""
    from ownchart.exports.mappers import _BODY_SIGNAL_EXTRACTION_METHODS
    assert "native_healthkit" in _BODY_SIGNAL_EXTRACTION_METHODS


def test_pictal_section_map_still_has_no_dose_event_key():
    """No fact_type key in the Pictal section map may carry a
    dose-event shape. This is the structural pin alongside the
    extraction-method wall above."""
    from ownchart.exports.mappers import _PICTAL_SECTION_FOR_FACT_TYPE
    for key in _PICTAL_SECTION_FOR_FACT_TYPE.keys():
        assert "dose" not in key.lower(), (
            f"Pictal section map carries a dose-shaped key {key!r}"
        )
