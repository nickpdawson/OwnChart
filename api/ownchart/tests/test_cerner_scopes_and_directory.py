"""Cerner / Oracle Health Millennium scope + directory contract.

Beta 1 (PM directive 2026-05-24). Pins:

  - _CERNER_DEFAULT_SCOPES contains no wildcard.
  - _CERNER_DEFAULT_SCOPES uses SMART v2 `.rs` shape per resource.
  - _CERNER_DEFAULT_SCOPES includes openid + fhirUser + launch/patient
    + online_access.
  - _CERNER_DEFAULT_SCOPES does NOT include offline_access (Beta 1
    posture — only when Nick confirms refresh-token approval on
    the Cerner app registration).
  - Manual Cerner connector creation stamps the OWNCHART_CERNER_CLIENT_ID
    env var (verified via the existing _client_id_env_for helper).
  - parse_cerner_bundle resolves a synthetic Bundle modeled on
    Oracle's published shape to (name, fhir_base) pairs.
  - Specifically: a Centra Health-shaped Organization resolves to
    https://fhir-myrecord.cerner.com/r4/<tenant>/  (matches PM's
    smoke target).
"""

from __future__ import annotations

import json

from ownchart.ingest.provider_directory import (
    DirectoryEntry,
    parse_cerner_bundle,
    search,
)
from ownchart.routes.connectors import (
    _CERNER_DEFAULT_SCOPES,
    _CERNER_PATIENT_RESOURCES,
    _client_id_env_for,
    _default_scopes_for,
)


# ---------------------------------------------------------------------------
# Scope contract


def test_cerner_default_scopes_has_no_wildcard():
    assert "patient/*.read" not in _default_scopes_for("cerner")
    assert "patient/*.rs" not in _default_scopes_for("cerner")


def test_cerner_default_scopes_uses_rs_explicit_per_resource():
    scopes = _default_scopes_for("cerner")
    for r in _CERNER_PATIENT_RESOURCES:
        assert f"patient/{r}.rs" in scopes, f"missing patient/{r}.rs"


def test_cerner_default_scopes_includes_uscdi_core_resources():
    """Direct USCDI-aligned read so a future reorder of
    _CERNER_PATIENT_RESOURCES still passes the must-have set."""
    scopes = _default_scopes_for("cerner")
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
        "CarePlan",
        "CareTeam",
        "Goal",
        "Provenance",
    ):
        assert f"patient/{r}.rs" in scopes


def test_cerner_default_scopes_includes_smart_preamble():
    scopes = _default_scopes_for("cerner")
    assert "openid" in scopes
    assert "fhirUser" in scopes
    assert "launch/patient" in scopes
    assert "online_access" in scopes


def test_cerner_default_scopes_omits_offline_access():
    """Beta 1 posture per PM — only request offline_access if
    Nick confirms the registration includes refresh-token approval."""
    assert "offline_access" not in _default_scopes_for("cerner")


def test_cerner_scope_constant_matches_resolver():
    assert _default_scopes_for("cerner") == _CERNER_DEFAULT_SCOPES


def test_cerner_resource_list_has_25_entries_no_duplicates():
    assert len(_CERNER_PATIENT_RESOURCES) == 25
    assert len(set(_CERNER_PATIENT_RESOURCES)) == 25


def test_cerner_client_id_env_var_name():
    """OWNCHART_CERNER_CLIENT_ID is the env var the route reads at
    create-connector time to stamp the new ProviderConnector row's
    client_id. Pinning the exact env var name pre-empts a future
    rename that would silently break Connect."""
    assert _client_id_env_for("cerner") == "OWNCHART_CERNER_CLIENT_ID"


def test_other_vendors_unchanged_by_cerner_addition():
    """Pin that adding Cerner didn't smear v2 syntax onto Epic or
    the default."""
    epic = _default_scopes_for("epic")
    assert "patient/*.read" in epic
    assert "patient/*.rs" not in epic


# ---------------------------------------------------------------------------
# Directory parser — synthetic Bundle modeled on Oracle's shape


_CENTRA_FHIR_BASE = (
    "https://fhir-myrecord.cerner.com/r4/"
    "ab208292-75a1-4788-9fc7-1e9a40a7eee3/"
)

_SYNTHETIC_BUNDLE = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [
        {
            "resource": {
                "resourceType": "Organization",
                "id": "Oab208292-75a1-4788-9fc7-1e9a40a7eee3",
                "name": "Centra Health, Inc.",
                "alias": ["Centra"],
                "endpoint": [
                    {"reference": "Endpoint/ep-centra"},
                ],
            },
        },
        {
            "resource": {
                "resourceType": "Endpoint",
                "id": "ep-centra",
                "status": "active",
                "address": _CENTRA_FHIR_BASE,
            },
        },
        {
            "resource": {
                "resourceType": "Organization",
                "id": "Oxyz",
                "name": "Other Health System",
                "endpoint": [
                    {"reference": "Endpoint/ep-other"},
                ],
            },
        },
        {
            "resource": {
                "resourceType": "Endpoint",
                "id": "ep-other",
                "address": "https://fhir-myrecord.cerner.com/r4/0000-other/",
            },
        },
        # An Organization with no endpoints — skipped.
        {
            "resource": {
                "resourceType": "Organization",
                "id": "Oempty",
                "name": "Endpoint-less Hospital",
            },
        },
        # An Organization whose Endpoint reference doesn't resolve.
        {
            "resource": {
                "resourceType": "Organization",
                "id": "Odangling",
                "name": "Dangling Reference Health",
                "endpoint": [{"reference": "Endpoint/ep-missing"}],
            },
        },
    ],
}


def test_centra_health_resolves_to_exact_fhir_base():
    """The PM-specified acceptance: Centra Health → the exact
    fhir-myrecord URL Oracle publishes for that tenant."""
    entries = parse_cerner_bundle(json.dumps(_SYNTHETIC_BUNDLE))
    centra = [e for e in entries if e.name == "Centra Health, Inc."]
    assert len(centra) == 1
    assert centra[0].fhir_base == _CENTRA_FHIR_BASE
    assert centra[0].ehr_vendor == "cerner"


def test_parser_drops_organizations_without_resolvable_endpoint():
    """Endpoint-less orgs and dangling references both skipped."""
    entries = parse_cerner_bundle(json.dumps(_SYNTHETIC_BUNDLE))
    names = {e.name for e in entries}
    assert "Endpoint-less Hospital" not in names
    assert "Dangling Reference Health" not in names


def test_parser_emits_two_valid_organizations():
    entries = parse_cerner_bundle(json.dumps(_SYNTHETIC_BUNDLE))
    assert len(entries) == 2
    names = sorted(e.name for e in entries)
    assert names == ["Centra Health, Inc.", "Other Health System"]


def test_parser_handles_full_url_endpoint_reference():
    """Some Cerner bundles reference endpoints by full URL, not
    'Endpoint/X'. The parser must handle both."""
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "Organization",
                    "id": "Oabs",
                    "name": "Absolute URL Health",
                    "endpoint": [
                        {"reference": "https://example.com/r4/Endpoint/ep-abs"},
                    ],
                },
            },
            {
                "resource": {
                    "resourceType": "Endpoint",
                    "id": "ep-abs",
                    "address": "https://fhir.example.com/r4/tenant-abs/",
                },
            },
        ],
    }
    entries = parse_cerner_bundle(json.dumps(bundle))
    assert len(entries) == 1
    assert entries[0].name == "Absolute URL Health"
    assert entries[0].fhir_base == "https://fhir.example.com/r4/tenant-abs/"


def test_parser_returns_empty_on_malformed_json():
    """A non-JSON / non-Bundle response shouldn't raise — the route
    that calls this catches the empty list and surfaces a clean
    'no results' state to the user."""
    assert parse_cerner_bundle("not-json") == []
    assert parse_cerner_bundle("") == []
    assert parse_cerner_bundle("<html></html>") == []


def test_parser_returns_empty_on_non_bundle_shape():
    """A JSON document that isn't a Bundle (e.g. an Organization
    resource served at the wrong URL) yields no entries."""
    weird = {
        "resourceType": "Patient",
        "id": "X",
        "name": [{"family": "Doe"}],
    }
    assert parse_cerner_bundle(json.dumps(weird)) == []


def test_centra_resolvable_via_alias_search():
    """The PM directive says 'Search Organizations by name/alias/...'.
    Centra Health Inc.'s alias 'Centra' should match a 'centra' query
    even though the canonical name is the full corporate form."""
    entries = parse_cerner_bundle(json.dumps(_SYNTHETIC_BUNDLE))
    hits = search(entries, "centra")
    assert len(hits) >= 1
    assert hits[0].name == "Centra Health, Inc."


def test_alias_search_isnt_too_greedy():
    """A query that doesn't match any token should still return
    nothing — the alias text doesn't artificially expand the hit
    set for unrelated queries."""
    entries = parse_cerner_bundle(json.dumps(_SYNTHETIC_BUNDLE))
    hits = search(entries, "zzzzzz")
    assert hits == []


# ---------------------------------------------------------------------------
# DirectoryEntry default for non-cerner vendors


def test_directory_entry_aliases_optional_and_defaults_empty():
    """Existing Epic entries don't carry aliases; the dataclass
    must default to an empty tuple so old serialized cache files
    still deserialize without raising."""
    e = DirectoryEntry(name="X", fhir_base="https://x/", ehr_vendor="epic")
    assert e.aliases == ()
