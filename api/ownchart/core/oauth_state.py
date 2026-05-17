"""OAuth state-param signing helper (PM A-3, Beta 1 M02 Slice 1).

Every OAuth start endpoint (connectors today, calendar Google OAuth
when BE-4 lands) signs a state value that carries:

  - `user_id`: who initiated the flow.
  - `person_record_id`: which record the tokens will bind to.
  - `csrf_nonce`: random per-flow; prevents replay.
  - `started_at`: timestamp for an absolute expiry window
    (10 minutes; matches today's OAuthSession.expires_at).

On callback, the server decodes the state, verifies the user
matches the authenticated caller, confirms the user still has a
non-revoked `caregiver+` membership on `person_record_id`, and
binds the new tokens to that record. **Never** infers the record
from the active record at callback time — the user may have
switched tabs between connect-start and callback.

Signature uses `SESSION_SECRET` + `URLSafeTimedSerializer` so
state is opaque, tamper-evident, and self-expiring. Independent
of the `OAuthSession` DB row (which still gets created for PKCE
verifier storage and audit), so the state param is verifiable
without a DB hit.

Pure-function helpers:
  - `sign_oauth_state(payload)` → URL-safe string.
  - `decode_oauth_state(token, max_age_seconds=600)` →
    payload dict OR raises `OAuthStateError`.

Round-trip is deterministic enough to test without mocking time
(uses the configured SESSION_SECRET).
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeTimedSerializer,
)

from .config import get_settings


# Stable salt to keep oauth-state signatures distinct from
# session-cookie signatures (which share the same SESSION_SECRET).
# Changing this value invalidates every in-flight OAuth handshake.
_OAUTH_STATE_SALT = "ownchart-oauth-state"

# 10-minute default expiry. Matches today's OAuthSession.expires_at
# and most EHR OAuth flows' acceptable round-trip window.
DEFAULT_OAUTH_STATE_TTL_SECONDS = 600


class OAuthStateError(Exception):
    """Raised when a state param fails signature, expiry, or shape
    verification. Route layer catches and emits 400 Bad State."""


@dataclass(frozen=True)
class OAuthStatePayload:
    """The decoded shape after `decode_oauth_state` succeeds."""

    user_id: uuid.UUID
    person_record_id: uuid.UUID
    csrf_nonce: str
    started_at: datetime
    # Optional opaque pointer back to the DB-side OAuthSession row.
    # When set, the callback can also verify the row exists + matches.
    oauth_session_id: uuid.UUID | None = None


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().session_secret.get_secret_value()
    return URLSafeTimedSerializer(secret, salt=_OAUTH_STATE_SALT)


def generate_csrf_nonce() -> str:
    """Per-flow random. 22 chars of base64url entropy is plenty
    (~132 bits)."""
    return secrets.token_urlsafe(16)


def sign_oauth_state(
    *,
    user_id: uuid.UUID,
    person_record_id: uuid.UUID,
    csrf_nonce: str | None = None,
    oauth_session_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
) -> str:
    """Build + sign an OAuth state param.

    `csrf_nonce` defaults to a fresh random value. Callers MAY pass
    their own (e.g. to log it for replay-attack tracing) but the
    nonce must be present in the decoded payload by the time the
    callback runs.

    `started_at` defaults to now-UTC. The serializer also embeds
    its own timestamp for `max_age` verification at decode time,
    so the `started_at` field is informational; the actual expiry
    is enforced by `URLSafeTimedSerializer.loads(max_age=...)`.
    """
    payload: dict[str, Any] = {
        "user_id": str(user_id),
        "person_record_id": str(person_record_id),
        "csrf_nonce": csrf_nonce or generate_csrf_nonce(),
        "started_at": (started_at or datetime.now(timezone.utc)).isoformat(),
    }
    if oauth_session_id is not None:
        payload["oauth_session_id"] = str(oauth_session_id)
    return _serializer().dumps(payload)


def decode_oauth_state(
    token: str,
    *,
    max_age_seconds: int = DEFAULT_OAUTH_STATE_TTL_SECONDS,
) -> OAuthStatePayload:
    """Decode + verify a state token.

    Raises `OAuthStateError` on any failure (bad signature, expired,
    malformed shape). The caller maps to HTTP 400.
    """
    if not token:
        raise OAuthStateError("Missing state")
    try:
        raw = _serializer().loads(token, max_age=max_age_seconds)
    except SignatureExpired:
        raise OAuthStateError("State expired") from None
    except BadSignature:
        raise OAuthStateError("Bad state signature") from None
    if not isinstance(raw, dict):
        raise OAuthStateError("State payload is not a dict")

    try:
        user_id = uuid.UUID(str(raw["user_id"]))
        person_record_id = uuid.UUID(str(raw["person_record_id"]))
        csrf_nonce = str(raw["csrf_nonce"])
        started_at_iso = str(raw["started_at"])
        started_at = datetime.fromisoformat(started_at_iso)
        oauth_session_id = (
            uuid.UUID(str(raw["oauth_session_id"]))
            if raw.get("oauth_session_id") else None
        )
    except (KeyError, ValueError, TypeError) as e:
        raise OAuthStateError(f"State payload malformed: {e}") from None

    if not csrf_nonce:
        raise OAuthStateError("State missing csrf_nonce")

    return OAuthStatePayload(
        user_id=user_id,
        person_record_id=person_record_id,
        csrf_nonce=csrf_nonce,
        started_at=started_at,
        oauth_session_id=oauth_session_id,
    )
