"""`_classify_invite_for_accept` — FU-MULTITENANT-ONBOARDING.

The register-route accept classifier. Maps an Invitation row +
the registering email to one of six terminal verdicts:

  ok            — proceed with accept
  not_found     — no matching token (caller wraps in 410)
  expired       — past expires_at (410)
  accepted      — already used (410)
  revoked       — explicitly revoked (410)
  email_mismatch — invite email != registration email (403)

Pinned here so the FU's test matrix items (3 valid invite,
4 expired, 5 used, 6 revoked, 7 email mismatch) have a
DB-free unit-level check. The route layer's 410/403 mapping is
also covered by the route-level integration tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from ownchart.models.invitation import Invitation
from ownchart.routes.auth import _classify_invite_for_accept


def _u() -> uuid.UUID:
    return uuid.uuid4()


def _invite(
    *,
    invited_email: str = "b@example.com",
    expires_at: datetime,
    accepted_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> Invitation:
    now = datetime.now(timezone.utc)
    return Invitation(
        id=_u(),
        invited_email=invited_email,
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


def test_ok_when_active_and_email_matches():
    """FU test §E.3: valid invite allows registration."""
    now = datetime.now(timezone.utc)
    inv = _invite(
        invited_email="b@example.com",
        expires_at=now + timedelta(days=1),
    )
    assert _classify_invite_for_accept(
        inv, "b@example.com", now=now,
    ) == "ok"


def test_not_found_when_invite_is_none():
    """The token-lookup query returned no row that verified —
    caller passes None."""
    now = datetime.now(timezone.utc)
    assert _classify_invite_for_accept(
        None, "b@example.com", now=now,
    ) == "not_found"


def test_expired_when_past_expires_at():
    """FU test §E.4: expired invite fails."""
    now = datetime.now(timezone.utc)
    inv = _invite(expires_at=now - timedelta(seconds=1))
    assert _classify_invite_for_accept(
        inv, "b@example.com", now=now,
    ) == "expired"


def test_accepted_when_already_used():
    """FU test §E.5: used invite fails."""
    now = datetime.now(timezone.utc)
    inv = _invite(
        expires_at=now + timedelta(days=1),
        accepted_at=now - timedelta(minutes=1),
    )
    assert _classify_invite_for_accept(
        inv, "b@example.com", now=now,
    ) == "accepted"


def test_revoked_when_owner_revoked():
    """FU test §E.6: revoked invite fails."""
    now = datetime.now(timezone.utc)
    inv = _invite(
        expires_at=now + timedelta(days=1),
        revoked_at=now - timedelta(minutes=1),
    )
    assert _classify_invite_for_accept(
        inv, "b@example.com", now=now,
    ) == "revoked"


def test_email_mismatch_case_sensitive_input_normalized():
    """FU test §E.7: email mismatch is detected case-insensitively
    so 'Bob@Example.com' vs 'bob@example.com' is treated as same,
    but a genuinely different address fails."""
    now = datetime.now(timezone.utc)
    inv = _invite(
        invited_email="b@example.com",
        expires_at=now + timedelta(days=1),
    )
    # Different mailbox → mismatch.
    assert _classify_invite_for_accept(
        inv, "c@example.com", now=now,
    ) == "email_mismatch"
    # Different case → ok.
    assert _classify_invite_for_accept(
        inv, "B@Example.com", now=now,
    ) == "ok"
    # Surrounding whitespace tolerated.
    assert _classify_invite_for_accept(
        inv, "  b@example.com  ", now=now,
    ) == "ok"


def test_accepted_state_takes_priority_over_email_mismatch():
    """An already-accepted invite is reported as 'accepted' even if
    a stranger comes along with the wrong email and the token by
    chance. This stops a leak about whether the email matches
    once the invite is terminal."""
    now = datetime.now(timezone.utc)
    inv = _invite(
        invited_email="b@example.com",
        expires_at=now + timedelta(days=1),
        accepted_at=now - timedelta(minutes=1),
    )
    assert _classify_invite_for_accept(
        inv, "stranger@example.com", now=now,
    ) == "accepted"


def test_revoked_state_takes_priority_over_email_mismatch():
    now = datetime.now(timezone.utc)
    inv = _invite(
        invited_email="b@example.com",
        expires_at=now + timedelta(days=1),
        revoked_at=now - timedelta(minutes=1),
    )
    assert _classify_invite_for_accept(
        inv, "stranger@example.com", now=now,
    ) == "revoked"
