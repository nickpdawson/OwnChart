"""Device-pairing endpoints for the native iOS app (PR1).

`/pair` is the one-shot exchange: email + password + device name →
plaintext bearer token (shown exactly once in the response body).
The iOS app stores it in Keychain and uses it as
`Authorization: Bearer <token>` for every subsequent request.

`/tokens` lists the current user's active devices. `/tokens/{id}`
revokes one. Auth on the list/revoke endpoints accepts either the
device bearer token OR the session cookie — a paired phone can
manage its peers, and the web UI can revoke a phone.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.device_auth import get_user_from_device_token_or_session
from ..core.security import verify_password
from ..models.device_token import DeviceToken
from ..models.user import User

router = APIRouter()


class PairRequest(BaseModel):
    email: EmailStr
    password: str
    device_name: str


class ServerCapabilities(BaseModel):
    """Bundled with the pair response so the iOS app can feature-flag.

    Mirrors what would otherwise need a separate /api/server-info
    call. Capabilities list grows as endpoints land.
    """

    server_version: str
    healthkit_sync_endpoint: bool
    ask_endpoint: bool
    discover_endpoint: bool


class UserSummary(BaseModel):
    id: str
    email: EmailStr
    phi_consent_granted: bool


class PairResponse(BaseModel):
    token: str  # plaintext, only returned at pairing time
    token_id: str
    user: UserSummary
    server_capabilities: ServerCapabilities


class DeviceTokenReadout(BaseModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None
    is_current: bool


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _capabilities() -> ServerCapabilities:
    # Static for V1; expand as features land. The iOS app keys off
    # individual booleans, not the version string.
    return ServerCapabilities(
        server_version="0.1.0",
        healthkit_sync_endpoint=True,
        ask_endpoint=True,
        discover_endpoint=True,
    )


@router.post("/pair", status_code=status.HTTP_201_CREATED)
async def pair_device(
    body: PairRequest,
    db: AsyncSession = Depends(get_session),
) -> PairResponse:
    """Issue a bearer token for a named device after email/password
    verification. Plaintext token is in the response exactly once.
    """
    if not body.device_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="device_name is required",
        )

    user = (await db.execute(
        select(User).where(User.email == body.email)
    )).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        # Constant-time-ish: invalid email and invalid password return
        # the same error; bcrypt verify_password handles the timing.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # 32 bytes → 43-char URL-safe string, 256 bits of entropy. No need
    # for Argon2 — sha256 is sufficient for a high-entropy bearer.
    plaintext = secrets.token_urlsafe(32)
    row = DeviceToken(
        id=uuid.uuid4(),
        user_id=user.id,
        name=body.device_name.strip()[:255],
        hashed_token=_hash_token(plaintext),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    return PairResponse(
        token=plaintext,
        token_id=str(row.id),
        user=UserSummary(
            id=str(user.id),
            email=user.email,
            phi_consent_granted=user.phi_consent_granted,
        ),
        server_capabilities=_capabilities(),
    )


@router.get("/tokens")
async def list_device_tokens(
    request: Request,
    user: User = Depends(get_user_from_device_token_or_session),
    db: AsyncSession = Depends(get_session),
) -> list[DeviceTokenReadout]:
    """List active (non-revoked) device tokens for the current user."""
    rows = (await db.execute(
        select(DeviceToken)
        .where(
            DeviceToken.user_id == user.id,
            DeviceToken.revoked_at.is_(None),
        )
        .order_by(DeviceToken.created_at.desc())
    )).scalars().all()
    current_id = getattr(request.state, "device_token_id", None)
    return [
        DeviceTokenReadout(
            id=str(r.id),
            name=r.name,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            is_current=(current_id is not None and r.id == current_id),
        )
        for r in rows
    ]


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device_token(
    token_id: uuid.UUID,
    user: User = Depends(get_user_from_device_token_or_session),
    db: AsyncSession = Depends(get_session),
) -> None:
    row = (await db.execute(
        select(DeviceToken).where(
            DeviceToken.id == token_id,
            DeviceToken.user_id == user.id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    return None
