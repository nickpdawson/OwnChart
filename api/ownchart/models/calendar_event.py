"""CalendarEvent — one event ingested from a CalendarSource.

Created by migration 0036 (Slice 3). One row per (calendar_source_id,
external_id). Upsert key is ``(calendar_source_id, external_id)``;
the EventKit external_id is stable across re-syncs.

Privacy posture is recorded at ingest time on ``privacy_mode_applied``.
Lower-mode storage drops fields to NULL — the row's existence is the
"busy" signal. A subsequent privacy_mode tightening on the source
triggers a redaction sweep in the route layer (NOT the DB).

``tombstoned_at`` is the soft-delete marker. Retrieval filters it
out; a 30-day periodic worker hard-deletes (PM B-3). Source
disconnect cascade-tombstones in the route layer, not the DB.

LLM exposure is a separate decision (PM B-4): the projector
``project_event_for_llm()`` in ``ingest/calendar_eventkit.py`` hides
stored fields from Ask unless the owning source has
``llm_full_details_consent=true``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class CalendarEvent(Base, TimestampMixin):
    __tablename__ = "calendar_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    person_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    calendar_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calendar_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    external_modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    all_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )

    # Stored only when privacy_mode_applied allows; NULL otherwise.
    # Server enforces redaction at ingest as defense in depth.
    title: Mapped[str | None] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(512))
    notes: Mapped[str | None] = mapped_column(String)
    attendees_count: Mapped[int | None] = mapped_column(SmallInteger)

    privacy_mode_applied: Mapped[str] = mapped_column(
        String(16), nullable=False,
    )
    tombstoned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)
