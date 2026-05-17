"""AutoExportToken — per-(user, person_record) bearer for Auto Export REST push.

Beta 1 Milestone 02, Slice 1 (PM A-2 option C). Replaces the
single instance-wide `OWNCHART_AUTO_EXPORT_TOKEN` env var for any
instance that has more than one person_record. Single-record
self-hosters can keep using the env var; the route falls back to
it cleanly.

Token raw value is shown to the user ONCE at creation; only the
sha256 hash is stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class AutoExportToken(Base):
    __tablename__ = "auto_export_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    # User-supplied display label. "iPhone — Avery", "Watch backup", etc.
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    # sha256 of the raw token. Constant-time compare at auth time.
    # Raw token is shown to the user exactly once at creation.
    token_hash: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True,
    )
    # Forward-looking. Beta 1 ships ['push']; future could add
    # ['push', 'delete_own_pushes'] or similar.
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, default=lambda: ["push"],
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    # Soft delete. Auth check filters `revoked_at IS NULL`.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
