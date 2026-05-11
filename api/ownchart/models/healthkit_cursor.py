"""Per (user, device_token, HK-identifier) sync cursor for native iOS sync.

The iOS app uses `HKAnchoredObjectQuery` (raw) or
`HKStatisticsCollectionQuery` (daily aggregates) and gets back an
opaque `HKQueryAnchor` it must persist to resume after the app dies
or is reinstalled. Server stores the archived-data bytes verbatim
plus the latest sample's end-time for human-readable progress
reporting.

Scoped per `device_token_id`, not just per user: a re-paired phone or
a second device starts its own anchor for the same identifier so it
doesn't try to backfill from another device's progress.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class HealthKitCursor(Base, TimestampMixin):
    __tablename__ = "healthkit_sync_cursors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    device_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_tokens.id", ondelete="SET NULL"),
    )
    identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    anchor_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    last_sample_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_strategy: Mapped[str | None] = mapped_column(String(32))
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "device_token_id", "identifier",
            name="uq_hkcursor_user_dev_id",
        ),
    )
