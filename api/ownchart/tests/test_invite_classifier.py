"""Invitation state classifier — FU-MULTITENANT-ONBOARDING.

Pure-function tests for `_classify_invite_state`, the helper that
collapses (`accepted_at`, `revoked_at`, `expires_at`) into one of
four terminal states the route + UI care about.

What this pins:
  - Active when none of (accepted, revoked, expired) hold.
  - Accepted takes priority over expired (a used invite stays
    "accepted" even after its original window passes).
  - Revoked takes priority over expired.
  - Expired is computed against now, not stored as a column.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from ownchart.models.invitation import Invitation
from ownchart.routes.invitations import _classify_invite_state


def _u() -> uuid.UUID:
    return uuid.uuid4()


def _invite(
    *,
    expires_at: datetime,
    accepted_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> Invitation:
    now = datetime.now(timezone.utc)
    return Invitation(
        id=_u(),
        invited_email="b@example.com",
        target_person_record_id=_u(),
        create_new_record=False,
        proposed_record_name=None,
        role="caregiver",
        token_hash="x",
        token_lookup_prefix="abcdef12",
        expires_at=expires_at,
        created_by_user_id=_u(),
        created_at=now,
        accepted_at=accepted_at,
        revoked_at=revoked_at,
    )


def test_active_when_future_expiry_and_unused():
    now = datetime.now(timezone.utc)
    inv = _invite(expires_at=now + timedelta(days=7))
    assert _classify_invite_state(inv, now=now) == "active"


def test_expired_when_past_expiry():
    now = datetime.now(timezone.utc)
    inv = _invite(expires_at=now - timedelta(seconds=1))
    assert _classify_invite_state(inv, now=now) == "expired"


def test_accepted_priority_over_expired():
    """Once accepted, the invite is terminal. Expiry no longer
    matters — we don't 'un-accept' an old invite."""
    now = datetime.now(timezone.utc)
    inv = _invite(
        expires_at=now - timedelta(days=1),
        accepted_at=now - timedelta(days=2),
    )
    assert _classify_invite_state(inv, now=now) == "accepted"


def test_revoked_priority_over_expired():
    now = datetime.now(timezone.utc)
    inv = _invite(
        expires_at=now - timedelta(days=1),
        revoked_at=now - timedelta(days=2),
    )
    assert _classify_invite_state(inv, now=now) == "revoked"


def test_accepted_priority_over_revoked():
    """If a row somehow has both (shouldn't happen in practice;
    the schema doesn't prevent it but the route does), accepted
    wins. Pinning the order so we don't flip-flop on it."""
    now = datetime.now(timezone.utc)
    inv = _invite(
        expires_at=now + timedelta(days=7),
        accepted_at=now - timedelta(hours=1),
        revoked_at=now - timedelta(minutes=30),
    )
    assert _classify_invite_state(inv, now=now) == "accepted"


def test_at_exact_expiry_is_expired():
    """A boundary check: `expires_at <= now` is the inclusive cut."""
    now = datetime.now(timezone.utc)
    inv = _invite(expires_at=now)
    assert _classify_invite_state(inv, now=now) == "expired"
