"""Unauthenticated account-recovery endpoints (Email/SMTP workstream).

Three endpoints, all mounted under ``/api/auth``:

  POST /api/auth/password/reset-request   {email}
      ALWAYS 200 (anti-enumeration). When the email maps to a real user AND
      SMTP is configured, mints a single-use reset token and emails the
      link. Rate-limited per IP + per email.

  POST /api/auth/password/reset           {token, new_password}
      Verifies the token (unexpired, unconsumed), sets a new argon2 hash,
      bumps ``session_token_version`` (kills the user's other web sessions),
      and consumes the token. Rate-limited per IP (the token is the secret,
      so it must NOT seed a per-identifier counter).

  POST /api/auth/verify-email             {token}
      Flips ``users.email_verified`` when a signed verification token
      validates for the account. Idempotent. Rate-limited per IP.

The pure decision logic (``classify_reset_token``) is separated from the DB
so it can be pinned without a Postgres fixture, matching the codebase's
existing classifier-test pattern (see ``_classify_invite_for_accept``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.db import get_session
from ..core.email import (
    is_smtp_configured,
    public_url,
    send_password_reset_email,
)
from ..core.logger import get_logger
from ..core.rate_limit import enforce_rate_limit
from ..core.security import (
    generate_reset_token,
    hash_password,
    hash_reset_token,
    read_email_verification_token,
    reset_lookup_prefix,
    verify_password,
    verify_reset_token,
)
from ..models.audit_event import AuditEvent
from ..models.invitation import Invitation
from ..models.password_reset_token import PasswordResetToken
from ..models.person_record import PersonRecord
from ..models.user import User
from .auth import (
    _materialize_invite_membership,
    notify_inviter_of_acceptance,
)

router = APIRouter()
log = get_logger("ownchart.routes.auth_recovery")


# ---------------------------------------------------------------------------
# Request shapes


class ResetRequestBody(BaseModel):
    email: EmailStr


class ResetPasswordBody(BaseModel):
    token: str = Field(min_length=1)
    new_password: str


class VerifyEmailBody(BaseModel):
    token: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Pure helpers


def _humanize_ttl(seconds: int) -> str:
    """Short human phrase for an expiry note. No PHI, no user input."""
    hours = seconds / 3600
    if hours >= 24 and (seconds % 86400 == 0):
        days = seconds // 86400
        unit = "day" if days == 1 else "days"
        return f"This link expires in {days} {unit}."
    if hours >= 1:
        n = int(round(hours))
        unit = "hour" if n == 1 else "hours"
        return f"This link expires in about {n} {unit}."
    minutes = max(1, int(round(seconds / 60)))
    unit = "minute" if minutes == 1 else "minutes"
    return f"This link expires in about {minutes} {unit}."


def classify_reset_token(
    token_row: PasswordResetToken | None, *, now: datetime | None = None,
) -> Literal["ok", "not_found", "consumed", "expired"]:
    """Pure classifier for an attempted reset. Separated from the DB lookup
    so the decision tree can be unit-tested without Postgres."""
    n = now or datetime.now(timezone.utc)
    if token_row is None:
        return "not_found"
    if token_row.consumed_at is not None:
        return "consumed"
    if token_row.expires_at <= n:
        return "expired"
    return "ok"


# ---------------------------------------------------------------------------
# DB helpers (thin, patchable — kept small so route branching is testable
# with these mocked, mirroring the account-deletion route tests).


async def _find_user_by_email(db: AsyncSession, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()


async def _issue_and_send_reset(
    db: AsyncSession, user: User,
) -> None:
    """Mint a single-use reset token for ``user`` and email the link.

    Only called for a real user with SMTP configured. Send failures are
    swallowed — the token still exists, and we never want a flaky MTA to
    turn an always-200 endpoint into an enumeration oracle via timing/500s.
    """
    now = datetime.now(timezone.utc)
    ttl = get_settings().password_reset_token_ttl_seconds
    raw = generate_reset_token()
    row = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_reset_token(raw),
        token_lookup_prefix=reset_lookup_prefix(raw),
        expires_at=now + timedelta(seconds=ttl),
        created_at=now,
    )
    db.add(row)
    await db.commit()

    url = public_url(f"/reset-password/{raw}")
    try:
        await send_password_reset_email(
            to=user.email, reset_url=url, expiry_note=_humanize_ttl(ttl),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "password_reset_email_failed",
            error=f"{type(exc).__name__}: {exc}",
        )


async def _lookup_reset_token(
    db: AsyncSession, token: str,
) -> PasswordResetToken | None:
    """Resolve a raw reset token to its row under a row-level lock.

    Prefix-indexed lookup, then constant-time argon2 verify. FOR UPDATE so a
    concurrent double-consume blocks until the first transaction commits.
    """
    if not token or len(token) < 12:
        return None
    prefix = reset_lookup_prefix(token)
    rows = (await db.execute(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_lookup_prefix == prefix)
        .with_for_update()
    )).scalars().all()
    for cand in rows:
        if verify_reset_token(token, cand.token_hash):
            return cand
    return None


# ---------------------------------------------------------------------------
# POST /api/auth/password/reset-request


@router.post("/password/reset-request")
async def request_password_reset(
    body: ResetRequestBody,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Begin a password reset. ALWAYS returns 200 so an attacker can't use
    the response to learn whether an email has an account. A token is minted
    (and a link emailed) only when the email maps to a real user and SMTP is
    configured."""
    s = get_settings()
    await enforce_rate_limit(
        request,
        bucket="password_reset_request",
        max_attempts=s.rate_limit_auth_max_attempts,
        window_seconds=s.rate_limit_auth_window_seconds,
        identifier=body.email,
    )
    email = body.email.lower().strip()
    if is_smtp_configured():
        user = await _find_user_by_email(db, email)
        if user is not None:
            await _issue_and_send_reset(db, user)
    # Uniform response regardless of existence / SMTP state.
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/auth/password/reset


@router.post("/password/reset")
async def reset_password(
    body: ResetPasswordBody,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Consume a reset token and set a new password.

    Invalidates the user's other web sessions (``session_token_version``
    bump) so a reset also boots an attacker who was riding a live cookie.
    """
    s = get_settings()
    # Per-IP only — the token IS the secret; using it as a rate-limit
    # identifier would leak nothing but also buys nothing, and a per-email
    # dimension isn't available here (no email in the body).
    await enforce_rate_limit(
        request,
        bucket="password_reset",
        max_attempts=s.rate_limit_auth_max_attempts,
        window_seconds=s.rate_limit_auth_window_seconds,
    )

    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "weak_password",
                "message": "New password must be at least 8 characters.",
            },
        )

    token_row = await _lookup_reset_token(db, body.token)
    verdict = classify_reset_token(token_row)
    if verdict != "ok":
        # Single shape for every terminal state — don't distinguish
        # not_found / expired / consumed.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_or_expired_token",
                "message": "This password reset link is invalid or has expired.",
            },
        )
    assert token_row is not None

    user = await db.get(User, token_row.user_id)
    if user is None:
        # The FK cascades tokens on user delete, so this is unreachable in
        # practice — treat defensively as an invalid token.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_or_expired_token",
                "message": "This password reset link is invalid or has expired.",
            },
        )

    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "password_unchanged",
                "message": "New password must differ from your current one.",
            },
        )

    now = datetime.now(timezone.utc)
    user.password_hash = hash_password(body.new_password)
    user.session_token_version = (user.session_token_version or 0) + 1
    token_row.consumed_at = now
    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=user.default_person_record_id,
        event_type="password_reset",
        subject_type="user",
        subject_id=str(user.id),
        detail={"session_token_version": user.session_token_version},
    ))
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/auth/verify-email


@router.post("/verify-email")
async def verify_email(
    body: VerifyEmailBody,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Verify an email address from a signed token. Idempotent — verifying an
    already-verified account returns 200 without a second write."""
    s = get_settings()
    await enforce_rate_limit(
        request,
        bucket="verify_email",
        max_attempts=s.rate_limit_auth_max_attempts,
        window_seconds=s.rate_limit_auth_window_seconds,
    )

    payload = read_email_verification_token(
        body.token, max_age=s.email_verification_token_ttl_seconds,
    )
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "invalid_or_expired_token",
            "message": "This verification link is invalid or has expired.",
        },
    )
    if payload is None:
        raise invalid
    try:
        uid = uuid.UUID(str(payload["uid"]))
    except (ValueError, KeyError, TypeError):
        raise invalid

    user = await db.get(User, uid)
    if user is None:
        raise invalid
    # Bind to the current email — a subsequent address change silently
    # invalidates an outstanding link.
    if user.email.lower().strip() != str(payload.get("email", "")).lower().strip():
        raise invalid

    now = datetime.now(timezone.utc)
    did_change = False
    if not user.email_verified:
        user.email_verified = True
        user.email_verified_at = now
        db.add(AuditEvent(
            user_id=user.id,
            person_record_id=user.default_person_record_id,
            event_type="email_verified",
            subject_type="user",
            subject_id=str(user.id),
            detail={},
        ))
        did_change = True

    # P0-4 mailbox-ownership gate: materialize any membership that was
    # deferred at registration until this mailbox was proven. The invite was
    # already consumed at register; `pending_accept_user_id` is the debt. We
    # create the membership now and clear the marker so a repeat verify is a
    # no-op.
    materialized_invite = await _materialize_pending_invite(db, user, now)
    if materialized_invite is not None:
        did_change = True

    if did_change:
        await db.commit()

    if materialized_invite is not None:
        await notify_inviter_of_acceptance(db, materialized_invite)
    return {"ok": True, "email_verified": True}


async def _materialize_pending_invite(
    db: AsyncSession, user: User, now: datetime,
) -> Invitation | None:
    """Create the deferred membership for an invite this user consumed at
    registration but couldn't activate until verifying their mailbox.

    Returns the materialized invite (for the inviter receipt), or None when
    there is nothing pending. Idempotent — clears ``pending_accept_user_id``
    once done, so a second verify finds nothing.
    """
    invite = (await db.execute(
        select(Invitation)
        .where(Invitation.pending_accept_user_id == user.id)
    )).scalars().first()
    if invite is None:
        return None

    # For an existing-record invite the target may have been deleted or
    # disconnected between register and verify. Don't FK-fail — just clear the
    # marker; the user lands on the no-records recovery screen and can be
    # re-invited.
    if not invite.create_new_record:
        target_id = invite.target_person_record_id
        record = await db.get(PersonRecord, target_id) if target_id else None
        if record is None or record.disconnected_at is not None:
            invite.pending_accept_user_id = None
            return None

    await _materialize_invite_membership(db, user=user, invite=invite, now=now)
    invite.pending_accept_user_id = None
    return invite
