"""Membership role helper tests (Beta 1 M02 Slice 1).

Pure-function tests for the `role_rank()` helper that powers
`require_role()`. No DB; the SQL CHECK constraint is exercised by
the alembic-migration integration tests, separately.

PM resolution A-4 (2026-05-17): `viewer` is included in the role
enum even though no viewer UI ships in M02. These tests pin the
role ordinal contract so future viewer-aware routes (and the
require_role helper landing in this slice) read against a stable
rank set.
"""

from __future__ import annotations

from ownchart.models.membership import (
    MEMBERSHIP_ROLES,
    role_rank,
)


def test_role_set_is_canonical_three():
    """Three roles in Beta 1: viewer, caregiver, owner. instance_admin
    is on `User`, NOT a membership role."""
    assert set(MEMBERSHIP_ROLES) == {"viewer", "caregiver", "owner"}
    assert len(MEMBERSHIP_ROLES) == 3


def test_role_rank_ordering():
    """The privilege ordering: owner > caregiver > viewer. Any helper
    that does `caller_rank >= required_rank` must respect this."""
    assert role_rank("owner") > role_rank("caregiver")
    assert role_rank("caregiver") > role_rank("viewer")
    assert role_rank("viewer") > 0


def test_unknown_role_ranks_zero():
    """An unknown role string is treated as no privilege. Defensive
    so a bad DB row doesn't accidentally pass a role check."""
    assert role_rank("admin") == 0
    assert role_rank("") == 0
    assert role_rank("OWNER") == 0  # case-sensitive — schema is lower-case


def test_require_role_caregiver_accepts_owner_and_caregiver():
    """The require_role check is `caller_rank >= required_rank`.
    Caregiver-required endpoints accept both caregiver and owner;
    reject viewer + unknown."""
    required = role_rank("caregiver")
    assert role_rank("owner") >= required
    assert role_rank("caregiver") >= required
    assert role_rank("viewer") < required
    assert role_rank("nonsense") < required


def test_require_role_owner_only_accepts_owner():
    """Owner-only endpoints (membership management, record delete)
    must reject caregiver."""
    required = role_rank("owner")
    assert role_rank("owner") >= required
    assert role_rank("caregiver") < required
    assert role_rank("viewer") < required


def test_require_role_viewer_accepts_all_three():
    """A viewer-min endpoint (every record-scoped GET) accepts all
    three roles. This is the broad permissive read tier."""
    required = role_rank("viewer")
    assert role_rank("owner") >= required
    assert role_rank("caregiver") >= required
    assert role_rank("viewer") >= required
