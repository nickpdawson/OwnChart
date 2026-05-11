"""Per-user, per-device bearer tokens for the native iOS app (PR1).

Cookie sessions (`ownchart_session`, 14-day TTL) are wrong for a native
app that runs in the background. The iOS app does a one-time email +
password + device-name `/pair` exchange, receives a bearer token, and
stores it in Keychain. The plaintext token is shown exactly once on
the pair response — only the sha256 of the token lives in this table.

Tokens are revocable from a future Devices web page (deferred for
alpha); revocation flips `revoked_at` and the auth dependency drops
the token on the next request.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class DeviceToken(Base, TimestampMixin):
    __tablename__ = "device_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # sha256 of the plaintext token, hex-encoded (64 chars). The 256-bit
    # secret-class token doesn't need Argon2 — no per-token brute-force
    # surface, and we'd pay Argon2's latency on every authenticated
    # request.
    hashed_token: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
