"""Dual-mode auth dependency: device bearer token OR session cookie.

The native iOS app authenticates every request with
`Authorization: Bearer <token>` (token issued by /api/auth/device/pair).
The existing web UI keeps using its `ownchart_session` cookie. Both
hit the same route handlers via this dependency.

Precedence: header first (so a stale webview cookie doesn't trump an
explicit bearer). `last_used_at` is updated at most once per 5 minutes
per token to avoid a write per request.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.db import get_session
from ..core.security import unsign_session
from ..models.device_token import DeviceToken
from ..models.user import User

_settings = get_settings()

# Throttle window for the last_used_at write so we don't take a write
# lock on every authenticated request.
_LAST_USED_WRITE_INTERVAL_SECONDS = 300


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def get_user_from_device_token_or_session(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> User:
    """Authenticate via Authorization: Bearer <token>, else fall back
    to the session cookie path.

    Raises 401 on missing/invalid/revoked token (when the header path
    is taken) or on missing/invalid cookie (when no Authorization
    header is present).
    """
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Empty bearer token",
            )
        row = (await db.execute(
            select(DeviceToken).where(
                DeviceToken.hashed_token == _hash_token(token),
                DeviceToken.revoked_at.is_(None),
            )
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked device token",
            )
        now = datetime.now(timezone.utc)
        # Throttled write — avoid a DB hit on every authenticated
        # request. last_used_at is purely informational (Devices page).
        if (
            row.last_used_at is None
            or (now - row.last_used_at).total_seconds() > _LAST_USED_WRITE_INTERVAL_SECONDS
        ):
            row.last_used_at = now
            await db.commit()
        user = await db.get(User, row.user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token references a missing user",
            )
        # Account lockout (Batch C). A disabled account's device bearer
        # tokens stop working immediately — the token row is left intact
        # (re-enabling the account restores it) but auth is refused here.
        # This is the device-token half of the belt-and-suspenders freeze;
        # the cookie half rides the session_token_version bump below.
        _reject_if_disabled(user)
        # Tuck the token id on the request state so downstream routes
        # (e.g. /healthkit/sync) can scope cursors per device.
        request.state.device_token_id = row.id
        return user

    # Cookie path — replicate the existing /api/auth/me verification
    # inline so we don't fight FastAPI's Cookie(...) parameter binding
    # (which lives on the original dependency's signature).
    request.state.device_token_id = None
    session_token = request.cookies.get(_settings.session_cookie_name)
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    # unsign_session enforces the signing timestamp against
    # SESSION_MAX_AGE_SECONDS (HIGH-5) — an expired cookie decodes to
    # None here, exactly like a tampered one.
    payload = unsign_session(session_token)
    if not payload or "uid" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = await db.get(User, payload["uid"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    # Session revocation lever (HIGH-5): the cookie embeds the
    # session_token_version it was signed at. A bump (e.g. password
    # change) leaves every live cookie carrying a stale `stv`, so they
    # all 401 and route to re-login. Pre-migration cookies with no
    # `stv` are treated as version 0 (matches the column default), so
    # existing sessions keep working until they expire or are bumped.
    if payload.get("stv", 0) != user.session_token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    # Account lockout (Batch C) — belt-and-suspenders. Disabling a user
    # bumps their session_token_version, so any live cookie already 401s
    # at the stv check above. This explicit check covers the edge where a
    # freshly-signed cookie could exist and makes the freeze
    # self-documenting: a disabled account never authenticates, period.
    _reject_if_disabled(user)
    return user


def _reject_if_disabled(user: User) -> None:
    """401 a disabled account. The single choke point both auth paths
    call so a frozen tester's cookie AND bearer token stop working."""
    if getattr(user, "disabled", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account disabled",
        )
