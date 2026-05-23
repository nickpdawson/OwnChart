"""FHIR date resolver — Section C Phase 1.

Pure-function tests for `_date_for()` and `_classify_condition_lifecycle()`
in `routes/connectors.py`. These are the load-bearing helpers behind the
UVA-bug fix: stop treating documentation timestamps as event dates.

What this pins:
  - Explicit occurrence fields (onsetDateTime, performedDateTime,
    effectiveDateTime, occurrenceDateTime, authoredOn) return
    provenance='explicit'.
  - Conditions with only `recordedDate` or `assertedDate` return
    (None, None, None). These were event dates in pre-Phase-1 code
    and are the headline UVA bug. **Regression test pinned.**
  - Observations/DiagnosticReports falling back to `issued` return
    provenance='issued_approximate'.
  - Period-shaped explicit fields work.
  - Encounter fallback path returns provenance='encounter_proximate'.
  - Condition `clinicalStatus` and `verificationStatus` classifier:
      resolved/inactive/remission → historical
      refuted/entered-in-error → skip
"""

from __future__ import annotations

from datetime import datetime, timezone

from ownchart.routes.connectors import (
    _classify_condition_lifecycle,
    _condition_status_codes,
    _date_for,
    _date_for_with_fallback,
)


# ---------------------------------------------------------------------------
# Explicit single-field paths


def test_condition_with_onset_dateTime_is_explicit():
    res = {
        "resourceType": "Condition",
        "onsetDateTime": "2014-03-15T00:00:00Z",
    }
    d, p, prov = _date_for(res)
    assert d == datetime(2014, 3, 15, tzinfo=timezone.utc)
    assert p == "day"
    assert prov == "explicit"


def test_procedure_with_performedDateTime_is_explicit():
    res = {
        "resourceType": "Procedure",
        "performedDateTime": "2014-04-01T08:30:00Z",
    }
    d, p, prov = _date_for(res)
    assert d == datetime(2014, 4, 1, 8, 30, tzinfo=timezone.utc)
    assert prov == "explicit"


def test_observation_effective_beats_issued():
    """The UVA-adjacent invariant the PM called out: Observation's
    effectiveDateTime is the clinical event time; issued is the report
    timestamp. Effective must win."""
    res = {
        "resourceType": "Observation",
        "effectiveDateTime": "2014-05-01T00:00:00Z",
        "issued":            "2026-05-09T00:00:00Z",  # decade-late re-issue
    }
    d, _, prov = _date_for(res)
    assert d == datetime(2014, 5, 1, tzinfo=timezone.utc)
    assert prov == "explicit"


def test_observation_with_only_issued_is_approximate():
    res = {
        "resourceType": "Observation",
        "issued": "2026-05-09T00:00:00Z",
    }
    d, _, prov = _date_for(res)
    assert d == datetime(2026, 5, 9, tzinfo=timezone.utc)
    assert prov == "issued_approximate"


def test_procedure_with_period_is_explicit():
    res = {
        "resourceType": "Procedure",
        "performedPeriod": {"start": "2014-04-01T08:00:00Z", "end": "2014-04-01T10:00:00Z"},
    }
    d, _, prov = _date_for(res)
    assert d == datetime(2014, 4, 1, 8, tzinfo=timezone.utc)
    assert prov == "explicit"


def test_period_falls_back_to_end_when_no_start():
    res = {
        "resourceType": "Procedure",
        "performedPeriod": {"end": "2014-04-01T10:00:00Z"},
    }
    d, _, prov = _date_for(res)
    assert d == datetime(2014, 4, 1, 10, tzinfo=timezone.utc)
    assert prov == "explicit"


# ---------------------------------------------------------------------------
# THE UVA BUG REGRESSION TESTS


def test_condition_with_only_recordedDate_returns_NULL():
    """The headline UVA bug. A Condition with no onset but with a
    `recordedDate` matching the FHIR sync day used to be promoted to
    an event with date_start=2026-05-09. Phase 1 drops recordedDate
    from the event-date priority list entirely."""
    res = {
        "resourceType": "Condition",
        "recordedDate": "2026-05-09T00:00:00Z",
        # No onsetDateTime, no onsetPeriod, no encounter.
    }
    d, p, prov = _date_for(res)
    assert d is None
    assert p is None
    assert prov is None


def test_condition_with_only_assertedDate_returns_NULL():
    """Legacy `assertedDate` (R3 → R4) same posture as recordedDate."""
    res = {
        "resourceType": "Condition",
        "assertedDate": "2026-05-09T00:00:00Z",
    }
    d, _, prov = _date_for(res)
    assert d is None
    assert prov is None


def test_condition_recordedDate_does_not_override_real_onset():
    """When both are present (the happy case for newer Conditions),
    onsetDateTime is honored. recordedDate doesn't even register."""
    res = {
        "resourceType": "Condition",
        "onsetDateTime": "2014-03-15T00:00:00Z",
        "recordedDate":  "2026-05-09T00:00:00Z",
    }
    d, _, prov = _date_for(res)
    assert d == datetime(2014, 3, 15, tzinfo=timezone.utc)
    assert prov == "explicit"


# ---------------------------------------------------------------------------
# Encounter fallback path


def test_encounter_fallback_marks_provenance_proximate():
    """Procedure with no own date but a linked Encounter inherits the
    Encounter's date with `provenance='encounter_proximate'` so the
    UI renders 'from this visit' rather than treating it as canonical."""
    encounter_dates = {
        "enc-123": (datetime(2014, 4, 1, tzinfo=timezone.utc), "day"),
    }
    res = {
        "resourceType": "Procedure",
        "encounter": {"reference": "Encounter/enc-123"},
        # no performedDateTime, no performedPeriod
    }
    d, p, prov = _date_for_with_fallback(res, encounter_dates)
    assert d == datetime(2014, 4, 1, tzinfo=timezone.utc)
    assert p == "day"
    assert prov == "encounter_proximate"


def test_resource_own_date_beats_encounter():
    """If the resource has its own date AND a linked Encounter, the
    resource wins and stays 'explicit' — Encounter is only the
    fallback for missing-date resources."""
    encounter_dates = {
        "enc-123": (datetime(2026, 5, 9, tzinfo=timezone.utc), "day"),
    }
    res = {
        "resourceType": "Procedure",
        "performedDateTime": "2014-04-01T00:00:00Z",
        "encounter": {"reference": "Encounter/enc-123"},
    }
    d, _, prov = _date_for_with_fallback(res, encounter_dates)
    assert d == datetime(2014, 4, 1, tzinfo=timezone.utc)
    assert prov == "explicit"


def test_no_date_anywhere_returns_NULL():
    res = {"resourceType": "Condition"}  # nothing at all
    d, p, prov = _date_for_with_fallback(res, {})
    assert d is None
    assert p is None
    assert prov is None


# ---------------------------------------------------------------------------
# Condition lifecycle classifier


def test_condition_resolved_marks_historical():
    res = {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"system": "...", "code": "resolved"}]},
    }
    status, skip = _classify_condition_lifecycle(res)
    assert status == "resolved"
    assert skip is False


def test_condition_inactive_marks_historical():
    res = {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"code": "inactive"}]},
    }
    status, _ = _classify_condition_lifecycle(res)
    assert status == "inactive"


def test_condition_remission_marks_historical():
    res = {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"code": "remission"}]},
    }
    status, _ = _classify_condition_lifecycle(res)
    assert status == "remission"


def test_condition_active_clinical_status_is_not_historical():
    res = {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"code": "active"}]},
    }
    status, skip = _classify_condition_lifecycle(res)
    assert status is None
    assert skip is False


def test_condition_refuted_signals_skip():
    """Refuted Conditions are the EHR's self-corrections; honor them
    silently. Caller drops the fact entirely; emits a count-only
    audit event."""
    res = {
        "resourceType": "Condition",
        "verificationStatus": {"coding": [{"code": "refuted"}]},
    }
    status, skip = _classify_condition_lifecycle(res)
    assert skip is True
    assert status is None  # don't carry a historical label for skipped rows


def test_condition_entered_in_error_signals_skip():
    res = {
        "resourceType": "Condition",
        "verificationStatus": {"coding": [{"code": "entered-in-error"}]},
    }
    _, skip = _classify_condition_lifecycle(res)
    assert skip is True


def test_condition_status_code_extractor_handles_uppercase():
    """FHIR systems vary on case. Codes should compare lowercased."""
    res = {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"code": "RESOLVED"}]},
    }
    clin, _ = _condition_status_codes(res)
    assert clin == "resolved"


def test_condition_status_extractor_returns_none_when_absent():
    res = {"resourceType": "Condition"}
    clin, verif = _condition_status_codes(res)
    assert clin is None
    assert verif is None


def test_condition_status_extractor_handles_missing_coding_array():
    res = {
        "resourceType": "Condition",
        "clinicalStatus": {"text": "active"},  # text without coding
    }
    clin, _ = _condition_status_codes(res)
    assert clin is None  # we only consume coding[].code; text-only is a no-op


# ---------------------------------------------------------------------------
# Combined: UVA strabismus regression


def test_uva_strabismus_shape_returns_null_date_and_no_skip():
    """Recreates the structural shape of UVA's strabismus Condition
    that triggered the bug: no onset, recordedDate = sync day, active
    clinical status, confirmed verification.

    After Phase 1:
      - date_start should be NULL
      - historical_status should be None (it's "active" per UVA)
      - should_skip should be False (we don't drop active conditions)
    """
    res = {
        "resourceType": "Condition",
        "recordedDate": "2026-05-09T00:00:00Z",
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}],
        },
        "verificationStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}],
        },
        "code": {"text": "Strabismus, left eye"},
    }
    d, p, prov = _date_for(res)
    assert d is None, "Phase 1: recordedDate must not become event date"
    assert prov is None
    historical, skip = _classify_condition_lifecycle(res)
    assert historical is None
    assert skip is False
