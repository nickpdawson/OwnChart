"""HK-1 backend guard — raw medication dose-events must NOT persist.

Context (2026-07-11): iOS builds ≤46 (already in the field) post
medication dose events one-per-dose through the generic
``strategy='raw'`` lane of ``/api/healthkit/sync`` with identifier
``HKMedicationDoseEventTypeIdentifierMedicationDoseEvent``. Dose events
are per-object-authorization PHI and must not become raw per-dose
``ExtractedFact`` rows. Build 47+ routes medication data through the
day-keyed aggregate lane (Health Auto Export ``ae-med-*`` in
``ingest/auto_export.py``), but old clients keep re-sending, so the
server has to recognize + drop these.

Chosen response semantics: the server acknowledges with a 200 and
counts the samples under ``skipped`` (never a 4xx). A 4xx would make
the old client treat the batch as failed and retry-loop the same
samples forever; a 200 + advanced HK anchor makes the client mark
them synced and stop.

Pure-function coverage — no DB, no HTTP client:
  1. ``is_medication_dose_event_identifier`` — robust prefix match,
     legacy aliases normalized in, unrelated identifiers unaffected.
  2. Source inspection of ``_sync_healthkit_inner`` — the guard skips
     (``continue``) before the ``ExtractedFact`` insert and reports
     via ``skipped``, and does NOT raise an HTTPException for the
     dose-event identifier (no retry loop).
  3. ``SyncResponse`` carries a ``skipped`` counter defaulting to 0.
  4. The dose-event identifier is still a valid raw registry entry, so
     ``enforce_strategy`` returns 200-cleanly (the guard, not a reject,
     is what stops persistence).
"""

from __future__ import annotations

import inspect

import pytest

from ownchart.ingest.healthkit import (
    MEDICATION_DOSE_EVENT_IDENTIFIER,
    enforce_strategy,
    is_medication_dose_event_identifier,
)


# ---------------------------------------------------------------------------
# 1. is_medication_dose_event_identifier — robustness
# ---------------------------------------------------------------------------


def test_canonical_dose_event_identifier_is_flagged():
    assert is_medication_dose_event_identifier(
        MEDICATION_DOSE_EVENT_IDENTIFIER
    )
    # Explicit string, in case the constant ever drifts.
    assert is_medication_dose_event_identifier(
        "HKMedicationDoseEventTypeIdentifierMedicationDoseEvent"
    )


def test_legacy_dose_event_spellings_are_flagged_via_normalization():
    """Old payloads still carrying a legacy alias must be caught even
    though the alias itself doesn't share the canonical prefix — the
    helper normalizes legacy → canonical before the prefix match."""
    assert is_medication_dose_event_identifier(
        "HKCategoryTypeIdentifierMedicationDoseEvent"
    )
    assert is_medication_dose_event_identifier(
        "HKUserAnnotatedMedicationDose"
    )


def test_future_dose_event_variant_is_flagged_by_prefix():
    """A hypothetical future Apple dose-event type under the same
    hierarchy must be covered by the prefix match without a code
    change here."""
    assert is_medication_dose_event_identifier(
        "HKMedicationDoseEventTypeIdentifierScheduledDose"
    )


def test_ordinary_quantity_identifiers_are_not_flagged():
    """A normal raw quantity sample (BodyMass) and an aggregate
    identifier (StepCount) must be untouched by the guard."""
    assert not is_medication_dose_event_identifier(
        "HKQuantityTypeIdentifierBodyMass"
    )
    assert not is_medication_dose_event_identifier(
        "HKQuantityTypeIdentifierStepCount"
    )


def test_workout_and_unknown_identifiers_are_not_flagged():
    assert not is_medication_dose_event_identifier("HKWorkoutType")
    assert not is_medication_dose_event_identifier("not-an-hk-id")
    assert not is_medication_dose_event_identifier("")
    # A clinical medication *record* (Health Records) is a different
    # type entirely and must stay on its clinical lane, not be dropped.
    assert not is_medication_dose_event_identifier(
        "HKClinicalTypeIdentifierMedicationRecord"
    )


# ---------------------------------------------------------------------------
# 2. Route wiring — guard short-circuits before the fact insert
# ---------------------------------------------------------------------------


def test_sync_inner_guards_raw_dose_events_before_persisting():
    """`_sync_healthkit_inner` must consult
    `is_medication_dose_event_identifier` and skip (continue) the
    batch, counting it under `skipped`, rather than reaching the
    `ExtractedFact` insert. Source-level assertion so a revert that
    silently drops the guard is caught (the generic raw fallback would
    otherwise happily persist a per-dose fact)."""
    from ownchart.routes.healthkit_sync import _sync_healthkit_inner

    src = inspect.getsource(_sync_healthkit_inner)
    assert "is_medication_dose_event_identifier(body.identifier)" in src
    assert "skip_dose_event_raw" in src
    # The guard must be gated on the raw strategy specifically.
    assert 'body.strategy == "raw"' in src
    # It must skip via a counted continue, not raise (no retry loop).
    assert "skipped += len(day_samples)" in src


def test_sync_inner_does_not_reject_dose_events_with_http_error():
    """The dose-event guard must acknowledge (skip), never raise. If a
    4xx/5xx path were introduced for dose events the old client would
    retry-loop. Assert no HTTPException is raised inside the guard
    block by checking the guard skips via `continue`."""
    from ownchart.routes.healthkit_sync import _sync_healthkit_inner

    src = inspect.getsource(_sync_healthkit_inner)
    # Isolate the guard block: from `skip_dose_event_raw` to the first
    # `first = day_samples[0]` (start of the normal persistence path).
    start = src.index("skip_dose_event_raw")
    end = src.index("first = day_samples[0]")
    guard_block = src[start:end]
    assert "raise HTTPException" not in guard_block, (
        "dose-event guard must not raise — a 4xx makes old clients "
        "retry-loop; it must skip + count instead"
    )
    assert "continue" in guard_block


# ---------------------------------------------------------------------------
# 3. SyncResponse carries a skipped counter
# ---------------------------------------------------------------------------


def test_sync_response_has_skipped_field_defaulting_to_zero():
    from ownchart.routes.healthkit_sync import SyncResponse

    resp = SyncResponse(
        accepted=3,
        deduplicated=1,
        cursor_id="abc",
        anchor_blob=None,
        mode="full",
    )
    # Default keeps every non-guard response identical in intent.
    assert resp.skipped == 0
    dumped = resp.model_dump()
    assert dumped["skipped"] == 0

    # And it round-trips when set explicitly.
    resp2 = SyncResponse(
        accepted=0,
        deduplicated=0,
        skipped=5,
        cursor_id="abc",
        anchor_blob=None,
        mode="full",
    )
    assert resp2.skipped == 5


# ---------------------------------------------------------------------------
# 4. enforce_strategy still accepts the identifier (guard, not reject)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["demo", "full"])
def test_dose_event_identifier_still_passes_strategy_enforcement(mode):
    """The stop-persisting behavior is the route guard, NOT a strategy
    rejection. `enforce_strategy` must still return the spec cleanly so
    the route reaches a 200 (skip) path rather than a 422 that would
    retry-loop the old client."""
    spec = enforce_strategy(MEDICATION_DOSE_EVENT_IDENTIFIER, "raw", mode)
    assert spec.identifier == MEDICATION_DOSE_EVENT_IDENTIFIER
    assert spec.scope == "medications"
