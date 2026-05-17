"""Auto Export bearer-token auth (PM A-2 option C, Beta 1 M02 Slice 1).

Two-path bearer auth for `POST /api/auto-export/push`:

  1. **Per-(user, person_record) token** stored in `auto_export_tokens`.
     The user creates one via Settings → Auto Export; the raw token
     is shown once. Server stores only `sha256(token)`. On push,
     we lookup by hash, verify not revoked, and bind the upload to
     the token's `(user_id, person_record_id)` tuple.

  2. **Legacy env token** `OWNCHART_AUTO_EXPORT_TOKEN` continues to
     work IFF the instance has exactly one person_record. Resolves
     to the first-and-only record. This keeps single-record self-
     hosters working without a token-migration step.

If neither matches, 401. If neither is configured AT ALL (no
per-record tokens and no env var), 503 — push is closed until the
operator provisions one path.

Pure-function helpers exposed for test:
  - `hash_token(raw: str) -> str` — sha256 hex digest.
  - `verify_token_hash(presented: str, stored: str) -> bool` —
    constant-time compare of `hash_token(presented)` against
    `stored`.

The DB-touching `authenticate_auto_export_push` lives here too;
it's a single async function the route calls.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.auto_export_token import AutoExportToken
from ..models.person_record import PersonRecord
from ..models.user import User
from .config import get_settings


# ---------------------------------------------------------------------------
# Pure-function helpers


def hash_token(raw: str) -> str:
    """Stable hex sha256. Same input → same output across processes."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_token_hash(presented_raw: str, stored_hash: str) -> bool:
    """Constant-time compare. Hashes the presented raw token and
    compares against the stored hash via hmac.compare_digest to
    avoid timing leaks."""
    if not presented_raw or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(presented_raw), stored_hash)


def parse_bearer_header(header_value: str | None) -> str | None:
    """Extract the raw token from `Authorization: Bearer <token>`.

    Returns None for missing / malformed. Constant-time across
    'no header' vs 'wrong scheme' to avoid telling probes which
    case they hit.
    """
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def generate_token() -> str:
    """Issue a fresh URL-safe token. 48 bytes → 64 char base64url.

    Tokens are opaque to clients; collision space is effectively
    infinite at this length.
    """
    return secrets.token_urlsafe(48)


# ---------------------------------------------------------------------------
# Auth resolution result


@dataclass(frozen=True)
class AutoExportAuthResult:
    """Outcome of an Auto Export auth attempt."""

    user: User
    person_record_id: uuid.UUID
    # 'token' = per-record AutoExportToken; 'legacy_env' = env-var
    # single-record fallback. For audit/logging.
    auth_method: str
    # Token id if auth_method='token'; None otherwise.
    token_id: uuid.UUID | None


# ---------------------------------------------------------------------------
# DB-backed resolver


async def authenticate_auto_export_push(
    db: AsyncSession,
    *,
    authorization_header: str | None,
) -> AutoExportAuthResult:
    """Authenticate a `POST /api/auto-export/push` request.

    Resolution order:
      1. Try the per-record token table (any non-revoked row).
      2. Fall back to the legacy env token IFF exactly one
         non-disconnected person_record exists.
      3. Otherwise raise 401 (or 503 if neither path is configured).
    """
    raw_token = parse_bearer_header(authorization_header)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. "
                   "Use `Bearer <token>`.",
            headers={"WWW-Authenticate": 'Bearer realm="auto-export"'},
        )

    # --- Path 1: per-(user, record) token ----------------------------------
    presented_hash = hash_token(raw_token)
    token_row = (await db.execute(
        select(AutoExportToken)
        .where(AutoExportToken.token_hash == presented_hash)
        .where(AutoExportToken.revoked_at.is_(None))
        .limit(1)
    )).scalar_one_or_none()

    if token_row is not None:
        user = await db.get(User, token_row.user_id)
        if user is None:
            # Orphan token (user gone, FK cascade failed somehow).
            # Treat as unauthenticated rather than leak existence.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Authorization token.",
                headers={"WWW-Authenticate":
                         'Bearer realm="auto-export"'},
            )
        # Stamp last_used_at. Best-effort — don't fail the auth if
        # this UPDATE collides with a concurrent push.
        await db.execute(
            update(AutoExportToken)
            .where(AutoExportToken.id == token_row.id)
            .values(last_used_at=func.now())
        )
        return AutoExportAuthResult(
            user=user,
            person_record_id=token_row.person_record_id,
            auth_method="token",
            token_id=token_row.id,
        )

    # --- Path 2: legacy env token (single-record instances only) -----------
    settings = get_settings()
    env_token = (
        settings.auto_export_token.get_secret_value()
        if settings.auto_export_token else None
    )
    if not env_token:
        # No env token configured. Per-record token didn't match.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization token.",
            headers={"WWW-Authenticate": 'Bearer realm="auto-export"'},
        )

    # Count active person_records to enforce the single-record
    # legacy boundary.
    record_count = (await db.execute(
        select(func.count(PersonRecord.id))
        .where(PersonRecord.disconnected_at.is_(None))
    )).scalar_one() or 0

    if record_count > 1:
        # PM A-2: legacy env token is intentionally closed when the
        # instance has multiple records. Tell the operator explicitly.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "OWNCHART_AUTO_EXPORT_TOKEN env var is not usable on "
                "instances with more than one person_record. Issue a "
                "per-record token via Settings → Auto Export."
            ),
        )

    # Exactly one record (or zero — handled below). Constant-time
    # compare of the env token.
    if not hmac.compare_digest(raw_token, env_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization token.",
            headers={"WWW-Authenticate": 'Bearer realm="auto-export"'},
        )

    # Find the single active record + its owner.
    only_record = (await db.execute(
        select(PersonRecord)
        .where(PersonRecord.disconnected_at.is_(None))
        .limit(1)
    )).scalar_one_or_none()
    if only_record is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No active person_record exists on this instance; "
                   "create one before pushing.",
        )
    only_user = await db.get(User, only_record.created_by_user_id)
    if only_user is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The active person_record has no creating user.",
        )

    return AutoExportAuthResult(
        user=only_user,
        person_record_id=only_record.id,
        auth_method="legacy_env",
        token_id=None,
    )
