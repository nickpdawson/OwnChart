"""AuthContext — resolves the active person record for a request.

Beta 1 Milestone 02, Slice 1, batch 1 (BE-1 §3.3 + PM A-5).

Two concerns:
  1. **Active-record resolution.** Pick which `person_record_id`
     the request is operating on, given the user's memberships
     and what they sent on the wire.
  2. **Membership re-check on every request.** Per PM A-5 the
     server does NOT trust a session's claim to a record — it
     verifies the user still has a non-revoked membership on the
     resolved record.

Resolution order (first match wins):
  a. `X-OwnChart-Person-Record` header (iOS sends this).
  b. Signed-session `active_record_id` field (web switcher pins this).
  c. `users.default_person_record_id` (set by migration 0028 to
     the user's self-record; the user can update later).
  d. First active membership ordered by
     `(memberships.created_at ASC, id ASC)`. Deterministic — same
     user gets the same fallback record on every cold request.

Error semantics (PM A-5 revised):

  - User has **zero** non-revoked memberships:
      → `403 {"code": "no_memberships", "message": ...}`.
      Session stays valid. iOS/web client routes to recovery /
      "no records available" UI; does NOT clear the session.

  - User has memberships, but the EXPLICITLY REQUESTED record
    (header or session pin) is one they have NO non-revoked
    membership on:
      → `403 {"code": "record_access_revoked", "message": ...}`.
      iOS/web refreshes memberships from `/api/auth/me`, picks
      another available record, retries. Session stays valid.
      **Never** silently falls back to a different record — the
      user explicitly asked for THIS record.

  - User authenticated but resolved record can't be selected
    even by the fallback chain (rare; implies DB corruption):
      → same `no_memberships` shape.

  - Request unauthenticated → 401 (existing contract, handled
    upstream by `get_user_from_device_token_or_session`).

Role gate:

  `require_role(min_role: MembershipRole)` returns a FastAPI
  dependency that depends on `get_auth_context` and raises
  `403 {"code": "insufficient_role", "required": min_role}` when
  the caller's role rank is below the required rank.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Literal

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.device_auth import get_user_from_device_token_or_session
from ..core.security import unsign_session
from ..models.membership import (
    MEMBERSHIP_ROLES,
    Membership,
    MembershipRole,
    role_rank,
)
from ..models.person_record import PersonRecord
from ..models.user import User
from .config import get_settings


HEADER_PERSON_RECORD = "x-ownchart-person-record"
SESSION_KEY_ACTIVE_RECORD = "active_record_id"


@dataclass(frozen=True)
class AuthContext:
    """The resolved auth + active-record state for a request.

    Lives for the request lifetime. Carries the user + the chosen
    person_record + the user's role on it. Built by
    `get_auth_context`; consumed by every record-scoped route.
    """

    user: User
    active_person_record: PersonRecord
    active_role: MembershipRole

    @property
    def active_record_id(self) -> uuid.UUID:
        return self.active_person_record.id


# ---------------------------------------------------------------------------
# Pure-function helpers (testable without DB)


def _parse_record_id_from_header(value: str | None) -> uuid.UUID | None:
    """Header is opaque to the client; server validates as UUID and
    silently treats malformed input as 'no header.' That matches the
    Demo session pattern and avoids leaking server validation rules."""
    if not value:
        return None
    try:
        return uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_session_active_record(session_payload: dict | None) -> uuid.UUID | None:
    if not session_payload:
        return None
    raw = session_payload.get(SESSION_KEY_ACTIVE_RECORD)
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def resolve_active_record_id(
    *,
    header_record_id: uuid.UUID | None,
    session_pin: uuid.UUID | None,
    default_record_id: uuid.UUID | None,
    active_membership_record_ids: list[uuid.UUID],
    user_explicitly_requested_record: bool,
) -> tuple[uuid.UUID | None, str]:
    """Apply the four-step resolution.

    Returns `(record_id_or_None, reason)` where `reason` is a tag
    for telemetry / debugging: 'header' | 'session' | 'default'
    | 'first_membership' | 'none'.

    Critically, when the user explicitly asked for a record via
    the header (or, on web, a session pin) but they have no
    membership on it, this function returns `(None, 'denied')`
    so the caller can emit `record_access_revoked`. It does
    NOT silently fall through to the next step.
    """
    active_set = set(active_membership_record_ids)

    if header_record_id is not None:
        if header_record_id in active_set:
            return header_record_id, "header"
        return None, "denied"

    if session_pin is not None:
        if session_pin in active_set:
            return session_pin, "session"
        # Web session pin to a record the user lost access to is
        # treated the same as a header miss — explicit ask, denial.
        return None, "denied"

    if default_record_id is not None and default_record_id in active_set:
        return default_record_id, "default"

    if active_membership_record_ids:
        # Deterministic fallback — caller passes the list sorted by
        # (created_at ASC, id ASC).
        return active_membership_record_ids[0], "first_membership"

    return None, "none"


# ---------------------------------------------------------------------------
# DB-backed dependency


async def _load_user_memberships(
    db: AsyncSession, user_id: uuid.UUID,
) -> list[Membership]:
    """Load all non-revoked memberships for the user, ordered
    deterministically. The order is the fallback ordering for the
    'first membership' resolution step."""
    rows = await db.execute(
        select(Membership)
        .where(Membership.user_id == user_id)
        .where(Membership.revoked_at.is_(None))
        .order_by(Membership.created_at.asc(), Membership.id.asc())
    )
    return list(rows.scalars().all())


def _read_session_payload(request: Request) -> dict | None:
    """Read + verify the session cookie. Returns the unsigned dict
    or None if no/invalid cookie. The session cookie's name is
    settings.session_cookie_name; the value is signed by
    SESSION_SECRET via itsdangerous."""
    s = get_settings()
    raw = request.cookies.get(s.session_cookie_name)
    if not raw:
        return None
    return unsign_session(raw)


async def get_auth_context(
    request: Request,
    user: User = Depends(get_user_from_device_token_or_session),
    db: AsyncSession = Depends(get_session),
) -> AuthContext:
    """The dependency every record-scoped route uses.

    Resolves the active person_record per the four-step rule above
    and emits the PM-specified 403 codes when the resolution fails.
    """
    memberships = await _load_user_memberships(db, user.id)

    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "no_memberships",
                "message": (
                    "Your account has no record memberships. Contact "
                    "your instance admin to be added to a record."
                ),
            },
        )

    header_record = _parse_record_id_from_header(
        request.headers.get(HEADER_PERSON_RECORD),
    )
    session_pin = _parse_session_active_record(_read_session_payload(request))

    active_ids = [m.person_record_id for m in memberships]
    resolved, reason = resolve_active_record_id(
        header_record_id=header_record,
        session_pin=session_pin,
        default_record_id=user.default_person_record_id,
        active_membership_record_ids=active_ids,
        user_explicitly_requested_record=(
            header_record is not None or session_pin is not None
        ),
    )

    if reason == "denied":
        # User asked for a specific record they no longer have access to.
        # Don't fall back — the caller wanted THIS record.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "record_access_revoked",
                "message": (
                    "Your access to this record has been revoked or "
                    "the record no longer exists. Refresh your "
                    "memberships and select another record."
                ),
            },
        )

    if resolved is None:
        # Should be unreachable given the no-memberships guard above,
        # but defensive: emit the no_memberships code.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "no_memberships",
                "message": "No accessible record could be resolved.",
            },
        )

    record = await db.get(PersonRecord, resolved)
    if record is None or record.disconnected_at is not None:
        # Membership lookup said the record id is active, but the
        # record itself is disconnected. Treat as revoked.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "record_access_revoked",
                "message": "This record has been disconnected.",
            },
        )

    role = next(
        (m.role for m in memberships if m.person_record_id == resolved),
        None,
    )
    if role not in MEMBERSHIP_ROLES:
        # Membership row carries an unknown role — fail closed.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "record_access_revoked",
                "message": "Membership role is invalid; contact admin.",
            },
        )

    return AuthContext(
        user=user,
        active_person_record=record,
        active_role=role,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Role gate factory


def require_role(min_role: MembershipRole) -> Callable[..., AuthContext]:
    """Return a FastAPI dependency that enforces a minimum membership role.

    Usage:
        @router.post("/api/sources/photo")
        async def upload_photo(
            ...,
            ctx: AuthContext = Depends(require_role("caregiver")),
        ):
            ...

    Raises `403 {"code": "insufficient_role", "required": <role>}`
    when the caller's role rank is below the required rank. The
    user is authenticated and has a valid active record; they just
    don't have the privilege for THIS operation.
    """
    if min_role not in MEMBERSHIP_ROLES:
        raise ValueError(
            f"require_role: {min_role!r} is not a valid role; "
            f"must be one of {MEMBERSHIP_ROLES}"
        )
    required_rank = role_rank(min_role)

    async def _dep(
        ctx: AuthContext = Depends(get_auth_context),
    ) -> AuthContext:
        if role_rank(ctx.active_role) < required_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "insufficient_role",
                    "required": min_role,
                    "actual": ctx.active_role,
                    "message": (
                        f"This action requires '{min_role}' role on "
                        "the active record."
                    ),
                },
            )
        return ctx

    return _dep
