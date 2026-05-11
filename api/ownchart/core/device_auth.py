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
    payload = unsign_session(session_token)
    if not payload or "uid" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = await db.get(User, payload["uid"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user
