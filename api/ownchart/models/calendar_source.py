"""CalendarSource — one bound calendar (iOS EventKit picker selection).

Created by migration 0036 (Slice 3). One row per (user, person_record,
adapter, external calendar id). Carries the user's privacy posture
for that calendar:

  - ``privacy_mode`` controls what fields land on every event from
    this source (server-enforced defense-in-depth — iOS is expected
    to apply it client-side too).
  - ``llm_full_details_consent`` is the second elevation (PM B-4):
    even when fields are stored, the Ask retrieval projector hides
    them unless this flag is true.

Disconnect is soft (``disconnected_at`` set); the route layer
cascade-tombstones events on the source. ON DELETE CASCADE on the
FK fires only when the source row is hard-deleted, which is not the
normal disconnect path.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


PRIVACY_MODES: tuple[str, ...] = (
    "full_details",
    "title_and_time",
    "busy_only",
)
ADAPTER_TYPES: tuple[str, ...] = ("ios_eventkit",)


class CalendarSource(Base, TimestampMixin):
    __tablename__ = "calendar_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    person_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    adapter_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    privacy_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="title_and_time",
    )
    llm_full_details_consent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
