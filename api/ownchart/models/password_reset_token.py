"""PasswordResetToken — single-use, short-lived credential for the
unauthenticated forgotten-password flow (Email/SMTP workstream).

Mirrors the invitation token pattern: the raw token is 32 url-safe random
bytes; the DB stores only its argon2id ``token_hash`` plus an indexed 8-char
``token_lookup_prefix`` so ``WHERE prefix = ?`` is cheap while verification
stays constant-time against the hash.

Single-use is enforced at consume time: the route takes a row-level lock,
checks ``consumed_at IS NULL`` and ``expires_at > now()``, applies the new
password, and stamps ``consumed_at``. A short TTL
(``settings.password_reset_token_ttl_seconds``, default 1h) bounds the
window a leaked link is useful.

Anti-enumeration lives in the route, not here: ``reset-request`` ALWAYS
returns 200 and only creates a row when the email maps to a real user.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True,
    )
    token_lookup_prefix: Mapped[str] = mapped_column(
        String(8), nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    @property
    def is_active(self) -> bool:
        if self.consumed_at is not None:
            return False
        return self.expires_at > datetime.now(timezone.utc)
