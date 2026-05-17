"""MeResponse shape tests (Beta 1 M02 Slice 1, Batch 1).

Pure-function tests for `_compose_me_response` — the helper that
builds the new `/api/auth/me` response shape per PM Decision Note §1.
No DB, no HTTP. The DB-loading half (`_load_active_memberships`)
is a one-liner SQL query whose user-id filter is visible by code
review; the load-bearing complexity is in the response composer.

What this pins:
  - The MembershipOut shape (person_record_id, role, display_name,
    is_self) for every active membership.
  - The ActiveRecordOut shape when active_record_id matches a
    membership.
  - active_record=None when (a) user has zero memberships, OR (b)
    the resolved active_record_id doesn't appear in memberships.
  - is_instance_admin + default_person_record_id passthrough.
  - Cross-record leak prevention: the response NEVER includes
    rows that weren't in the `memberships` argument. (Verifies
    the composer does not query/extrapolate.)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ownchart.models.membership import Membership
from ownchart.models.person_record import PersonRecord
from ownchart.models.user import User
from ownchart.routes.auth import (
    ActiveRecordOut,
    MembershipOut,
    _compose_me_response,
)


def _u() -> uuid.UUID:
    return uuid.uuid4()


def _user(
    *,
    is_instance_admin: bool = False,
    default_record: uuid.UUID | None = None,
) -> User:
    return User(
        id=_u(),
        # Pydantic EmailStr rejects reserved TLDs (.test, .local).
        # Use example.com per RFC 2606.
        email="alice@example.com",
        password_hash="x",
        phi_consent_granted=True,
        is_instance_admin=is_instance_admin,
        default_person_record_id=default_record,
    )


def _record(*, record_id: uuid.UUID, name: str, is_self: bool = False) -> PersonRecord:
    return PersonRecord(
        id=record_id,
        display_name=name,
        is_self=is_self,
        created_by_user_id=_u(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _membership(
    *, user_id: uuid.UUID, record_id: uuid.UUID, role: str,
) -> Membership:
    return Membership(
        id=_u(),
        user_id=user_id,
        person_record_id=record_id,
        role=role,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Shape: zero memberships


def test_zero_memberships_returns_empty_list_and_null_active():
    """A user who has no memberships still gets a valid 200 response
    so the client can show 'no records — contact admin' UI. PM A-5
    semantics: session valid, just nothing to scope to."""
    user = _user()
    out = _compose_me_response(
        user=user, memberships=[], active_record_id=None,
    )
    assert out.memberships == []
    assert out.active_record is None
    assert out.id == str(user.id)
    assert out.email == user.email
    assert out.is_instance_admin is False


# ---------------------------------------------------------------------------
# Shape: single membership (owner of own record)


def test_one_membership_owner_self_record():
    """The canonical post-migration-0028 state: user is owner of
    one is_self=True record."""
    user = _user()
    rid = _u()
    rec = _record(record_id=rid, name="Me", is_self=True)
    mem = _membership(user_id=user.id, record_id=rid, role="owner")
    out = _compose_me_response(
        user=user, memberships=[(mem, rec)], active_record_id=rid,
    )
    assert len(out.memberships) == 1
    m0 = out.memberships[0]
    assert m0.person_record_id == str(rid)
    assert m0.role == "owner"
    assert m0.display_name == "Me"
    assert m0.is_self is True
    assert out.active_record is not None
    assert out.active_record.id == str(rid)
    assert out.active_record.role == "owner"


# ---------------------------------------------------------------------------
# Shape: caregiver on someone else's record


def test_caregiver_membership_carries_role():
    """A user added as caregiver to a parent's record. The role
    surfaces in BOTH the memberships list AND the active_record
    block — the client uses both for UI."""
    user = _user()
    rid = _u()
    rec = _record(record_id=rid, name="Mom", is_self=False)
    mem = _membership(user_id=user.id, record_id=rid, role="caregiver")
    out = _compose_me_response(
        user=user, memberships=[(mem, rec)], active_record_id=rid,
    )
    assert out.memberships[0].role == "caregiver"
    assert out.memberships[0].is_self is False
    assert out.active_record.role == "caregiver"


# ---------------------------------------------------------------------------
# Shape: multiple memberships


def test_multiple_memberships_preserves_order_and_distinct_roles():
    """Nick caring for Mom + Dad while also owning his own record.
    Active record is "Me"; the response carries all three records
    with their respective roles."""
    user = _user()
    own_id, mom_id, dad_id = _u(), _u(), _u()
    own_rec = _record(record_id=own_id, name="Me", is_self=True)
    mom_rec = _record(record_id=mom_id, name="Mom", is_self=False)
    dad_rec = _record(record_id=dad_id, name="Dad", is_self=False)
    memberships = [
        (_membership(user_id=user.id, record_id=own_id, role="owner"), own_rec),
        (_membership(user_id=user.id, record_id=mom_id, role="caregiver"), mom_rec),
        (_membership(user_id=user.id, record_id=dad_id, role="caregiver"), dad_rec),
    ]
    out = _compose_me_response(
        user=user, memberships=memberships, active_record_id=own_id,
    )
    assert len(out.memberships) == 3
    # Order preserved.
    assert [m.display_name for m in out.memberships] == ["Me", "Mom", "Dad"]
    assert [m.role for m in out.memberships] == ["owner", "caregiver", "caregiver"]
    # Active record is the one resolved.
    assert out.active_record.id == str(own_id)
    assert out.active_record.role == "owner"


# ---------------------------------------------------------------------------
# Active-record resolution: explicit ask hits the right record


def test_active_record_can_be_a_caregiver_record():
    """User switches to "Mom" as active record. The active block
    reports caregiver role; the memberships list still shows all
    three."""
    user = _user()
    own_id, mom_id = _u(), _u()
    own_rec = _record(record_id=own_id, name="Me", is_self=True)
    mom_rec = _record(record_id=mom_id, name="Mom", is_self=False)
    memberships = [
        (_membership(user_id=user.id, record_id=own_id, role="owner"), own_rec),
        (_membership(user_id=user.id, record_id=mom_id, role="caregiver"), mom_rec),
    ]
    out = _compose_me_response(
        user=user, memberships=memberships, active_record_id=mom_id,
    )
    assert out.active_record.id == str(mom_id)
    assert out.active_record.role == "caregiver"


# ---------------------------------------------------------------------------
# Active-record resolution: unresolved


def test_active_record_id_not_in_memberships_yields_null_active():
    """If the resolver returned a record id the user isn't a member
    of, the composer returns active_record=None rather than fabricate
    a row. Defensive — `_compose_me_response` doesn't trust its
    input blindly. The /me route uses this branch when the requested
    header points at a record the user can't access; client sees
    null and refreshes."""
    user = _user()
    rid = _u()
    rec = _record(record_id=rid, name="Me", is_self=True)
    mem = _membership(user_id=user.id, record_id=rid, role="owner")
    out = _compose_me_response(
        user=user,
        memberships=[(mem, rec)],
        active_record_id=_u(),  # totally different record id
    )
    assert out.active_record is None
    # Memberships list is still correct.
    assert len(out.memberships) == 1
    assert out.memberships[0].person_record_id == str(rid)


# ---------------------------------------------------------------------------
# User-level fields passthrough


def test_is_instance_admin_passes_through():
    user = _user(is_instance_admin=True)
    out = _compose_me_response(
        user=user, memberships=[], active_record_id=None,
    )
    assert out.is_instance_admin is True


def test_default_person_record_id_passes_through():
    default_id = _u()
    user = _user(default_record=default_id)
    out = _compose_me_response(
        user=user, memberships=[], active_record_id=None,
    )
    assert out.default_person_record_id == str(default_id)


def test_default_person_record_id_is_null_when_unset():
    user = _user(default_record=None)
    out = _compose_me_response(
        user=user, memberships=[], active_record_id=None,
    )
    assert out.default_person_record_id is None


# ---------------------------------------------------------------------------
# Cross-record leak guard


def test_composer_returns_only_what_was_passed_in():
    """The cross-record leak property: the composer is a pure
    function of its inputs. It does NOT extrapolate, query, or
    decorate with anything outside the `memberships` argument.

    This is the structural guarantee that /me cannot leak another
    user's memberships — the route layer passes only the calling
    user's rows; the composer faithfully relays them.
    """
    user = _user()
    rid = _u()
    rec = _record(record_id=rid, name="Me", is_self=True)
    mem = _membership(user_id=user.id, record_id=rid, role="owner")
    out = _compose_me_response(
        user=user, memberships=[(mem, rec)], active_record_id=rid,
    )
    # Exactly one membership in, exactly one out.
    assert len(out.memberships) == 1
    # The display_name is the one from the record we passed in;
    # no fabrication.
    assert out.memberships[0].display_name == "Me"
    # Adding nothing magically.
    extra_attrs = set(out.model_dump().keys()) - {
        "id", "email", "phi_consent_granted",
        "is_instance_admin", "default_person_record_id",
        "memberships", "active_record",
    }
    assert not extra_attrs, f"composer leaked attrs: {extra_attrs}"


# ---------------------------------------------------------------------------
# Backward compatibility — pre-M02 iOS reads


def test_pre_m02_fields_still_present():
    """Pre-M02 iOS builds read `id`, `email`, `phi_consent_granted`.
    The new shape MUST keep those — additive only."""
    user = _user()
    out = _compose_me_response(
        user=user, memberships=[], active_record_id=None,
    )
    d = out.model_dump()
    assert "id" in d
    assert "email" in d
    assert "phi_consent_granted" in d
