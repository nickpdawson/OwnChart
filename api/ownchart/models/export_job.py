"""ExportJob — one user-initiated record export request.

Created by migration 0040 (Slice 4 export skeleton). Owned by a
(user, person_record) pair — a caregiver who switches active
records and requests an export gets a NEW job under the new record,
not a merged export.

Lifecycle:
  pending  → enqueued, waiting for the worker
  running  → worker picked it up, snapshot + mapping in progress
  completed → both mappers finished, export_files rows written
  failed   → error_message + failed_at set; user can retry

``expires_at`` is the 72-hour TTL (PM C-6). Set at completion time.
The expiry purge worker hard-deletes the on-disk file AND the row
(cascade-removes export_files via FK ON DELETE CASCADE) once
``now() > expires_at``.

``deleted_at`` is the user-facing soft-delete (DELETE /api/exports/{id})
— UI hides the export, retention timer drives hard delete after
the same TTL.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


JOB_STATUSES: tuple[str, ...] = ("pending", "running", "completed", "failed")
REQUESTED_FORMATS: tuple[str, ...] = ("ownchart_json", "txt", "all")

# Section D — domain filter values. "calendar" includes both
# calendar_sources and (non-tombstoned) calendar_events. AI summaries
# / Conversations are NOT yet wired into the snapshot — the UI shows
# the option as "coming soon" until the matching backend ships.
EXPORT_DOMAINS: tuple[str, ...] = ("clinical", "body_signals", "calendar")
EXPORT_DATE_RANGES: tuple[str, ...] = ("all", "last_90d", "last_1y", "custom")


class ExportJob(Base, TimestampMixin):
    __tablename__ = "export_jobs"

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
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    requested_format: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending",
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Section D — request-time filter envelope. Pre-Section-D jobs have
    # filters=NULL, treated as "no filters, full record." Shape:
    #   {
    #     "date_range_kind": "all" | "last_90d" | "last_1y" | "custom",
    #     "date_range_start": iso8601 | null,
    #     "date_range_end":   iso8601 | null,
    #     "domains": ["clinical", "body_signals", "calendar"],
    #   }
    # See exports/snapshot.py for how the runner consumes it.
    filters: Mapped[dict | None] = mapped_column(JSONB)
