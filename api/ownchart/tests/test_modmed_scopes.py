"""ModMed SMART scope contract — PM correction 2026-05-23.

Pre-first-connect: ModMed's portal docs specify SMART v2-shape
scopes for the Standalone Patient launch flow, not the v1 shape we
had been defaulting to. This file pins the corrected scope string
so a future refactor that "tidies" the scope list back to v1 fails
CI before reaching ModMed's authorize endpoint and getting silently
rejected.

Pinned invariants (per PM directive):
  - online_access is the refresh-token scope ModMed actually grants
    on Standalone Patient. offline_access is gated separately and
    only valid when explicitly approved on the app registration.
  - patient/*.rs is the v2 wildcard. patient/*.read is the v1 form
    which ModMed rejects.
"""

from __future__ import annotations

from ownchart.routes.connectors import (
    _MODMED_DEFAULT_SCOPES,
    _default_scopes_for,
)


def test_modmed_default_scopes_includes_online_access():
    """ModMed grants refresh tokens via `online_access` on Standalone
    Patient. `offline_access` is only valid when separately approved."""
    assert "online_access" in _default_scopes_for("modmed")


def test_modmed_default_scopes_includes_v2_wildcard_rs():
    """ModMed's portal docs publish `patient/*.rs` (v2 read+search),
    not `patient/*.read` (v1). Pinning so we don't silently regress."""
    assert "patient/*.rs" in _default_scopes_for("modmed")


def test_modmed_default_scopes_does_not_include_offline_access():
    """Counter-pin: `offline_access` would fail the authorize-endpoint
    check unless the ModMed app is explicitly approved for refresh
    tokens. Nick's app is not, so omitting it is correct as default."""
    assert "offline_access" not in _default_scopes_for("modmed")


def test_modmed_default_scopes_does_not_include_v1_wildcard_read():
    """Counter-pin: `patient/*.read` is the v1 form ModMed rejects.
    A revert to that string would silently break first-connect."""
    assert "patient/*.read" not in _default_scopes_for("modmed")


def test_modmed_scope_constant_matches_resolved_string():
    """Sanity: the `_default_scopes_for` resolver returns the
    constant we pinned at the module level — no inadvertent
    rewriting in the helper."""
    assert _default_scopes_for("modmed") == _MODMED_DEFAULT_SCOPES


def test_modmed_scope_string_carries_openid_and_fhirUser_and_launch():
    """The non-ModMed-specific SMART preamble must still be present
    so the authorize request is a well-formed SMART-on-FHIR launch."""
    scopes = _default_scopes_for("modmed")
    assert "openid" in scopes
    assert "fhirUser" in scopes
    assert "launch/patient" in scopes


def test_other_vendors_unchanged_by_modmed_correction():
    """Pin that fixing ModMed didn't smear v2 syntax onto Epic's or
    Athena's default scope strings — those have their own correct
    shapes and must remain untouched."""
    epic = _default_scopes_for("epic")
    # Epic uses v1 patient/*.read; .rs would be wrong for Epic.
    assert "patient/*.read" in epic
    assert "patient/*.rs" not in epic


def test_unknown_vendor_falls_back_to_default():
    """Belt: a brand-new vendor string flows through the default
    (v1 wildcard read) branch, not the ModMed branch. Pinning so a
    future case-label tweak doesn't accidentally make ModMed the
    default."""
    out = _default_scopes_for("brand-new-ehr")
    assert "patient/*.read" in out
    assert "patient/*.rs" not in out
    assert "offline_access" not in out  # default has no offline_access
