"""ModMed SMART scope contract — PM correction 2026-05-23.

The scope string is built from the actual ModMed FHIR vendor
dashboard for Nick's registered app: 3 standard scopes + 23
explicit `patient/<Resource>.rs` resource scopes. ModMed's
registration does NOT include the wildcard `patient/*.rs`, does
NOT include `offline_access`, and does NOT include `fhirUser` on
this app's selected-scopes list.

Pinned invariants:
  - online_access is the refresh-token-bearing scope ModMed grants
    on Standalone Patient (offline_access requires separate
    approval Nick's app doesn't have).
  - SMART v2 `.rs` permission suffix, never v1 `.read`.
  - Explicit resource list — wildcard rejected by ModMed even when
    individual resources are selected.
  - fhirUser absent — not selected on the registration, requesting
    it pre-approval would fail invalid_scope.
"""

from __future__ import annotations

from ownchart.routes.connectors import (
    _MODMED_DEFAULT_SCOPES,
    _MODMED_PATIENT_RESOURCES,
    _default_scopes_for,
)


def test_modmed_default_scopes_includes_online_access():
    assert "online_access" in _default_scopes_for("modmed")


def test_modmed_default_scopes_includes_explicit_patient_resources():
    """Every resource on Nick's ModMed registration must appear as
    `patient/<R>.rs` in the scope string. Pinning the full list so
    a future tidy-up that drops one silently fails CI."""
    scopes = _default_scopes_for("modmed")
    for r in _MODMED_PATIENT_RESOURCES:
        assert f"patient/{r}.rs" in scopes, f"missing patient/{r}.rs"


def test_modmed_default_scopes_includes_core_uscdi_resources():
    """Direct read of the most-likely-needed clinical resources, so a
    future refactor that reorders the tuple still passes through the
    USCDI must-haves."""
    scopes = _default_scopes_for("modmed")
    for r in (
        "Patient",
        "Condition",
        "AllergyIntolerance",
        "MedicationRequest",
        "Observation",
        "Procedure",
        "Immunization",
        "DiagnosticReport",
        "DocumentReference",
        "Encounter",
    ):
        assert f"patient/{r}.rs" in scopes


def test_modmed_default_scopes_does_not_include_wildcard():
    """ModMed's vendor dashboard for Nick's app shows 23 explicit
    resource scopes selected, not the wildcard. Most EHRs that
    approve explicit resources reject the wildcard form."""
    assert "patient/*.rs" not in _default_scopes_for("modmed")
    assert "patient/*.read" not in _default_scopes_for("modmed")


def test_modmed_default_scopes_does_not_include_offline_access():
    """offline_access requires separate ModMed app approval Nick's
    app doesn't have today — would fail access_denied. online_access
    is the documented Standalone Patient default."""
    assert "offline_access" not in _default_scopes_for("modmed")


def test_modmed_default_scopes_does_not_include_fhirUser():
    """fhirUser is available on the ModMed app catalog but NOT
    selected on Nick's registration; requesting it pre-approval
    would fail invalid_scope at the authorize endpoint. Standalone
    Patient launch returns the patient context regardless."""
    assert "fhirUser" not in _default_scopes_for("modmed")


def test_modmed_scope_constant_matches_resolved_string():
    """Sanity: the `_default_scopes_for` resolver returns the
    constant we pinned at the module level — no inadvertent
    rewriting in the helper."""
    assert _default_scopes_for("modmed") == _MODMED_DEFAULT_SCOPES


def test_modmed_scope_string_carries_openid_and_launch_patient():
    """The SMART preamble must still be present so the authorize
    request is a well-formed SMART-on-FHIR launch — even after we
    dropped fhirUser, the launch context scope is required."""
    scopes = _default_scopes_for("modmed")
    assert "openid" in scopes
    assert "launch/patient" in scopes


def test_modmed_resource_list_has_23_entries():
    """Lock the count so a future PR can't silently add a non-
    approved resource (which would fail the OAuth request)."""
    assert len(_MODMED_PATIENT_RESOURCES) == 23
    # And no duplicates.
    assert len(set(_MODMED_PATIENT_RESOURCES)) == 23


def test_other_vendors_unchanged_by_modmed_correction():
    """Pin that fixing ModMed didn't smear v2 syntax onto Epic's or
    the default vendor scope strings."""
    epic = _default_scopes_for("epic")
    assert "patient/*.read" in epic
    assert "patient/*.rs" not in epic


def test_unknown_vendor_falls_back_to_default():
    out = _default_scopes_for("brand-new-ehr")
    assert "patient/*.read" in out
    assert "patient/*.rs" not in out
    assert "offline_access" not in out
