"""Calendar source sync status — FU-CAL-SOURCE-STATUS.

Revision ID: 0041_calendar_source_sync_status
Revises: 0040_export_jobs_and_files
Create Date: 2026-05-20

Adds two columns to ``calendar_sources`` so the web settings UI can
render real "last sync" health without each client computing it
client-side from a sample of events:

  - ``last_sync_at``      — timestamp of the most recent successful
    POST /api/calendar/ingest for this source. NULL until the first
    ingest. Cleared on disconnect is intentionally NOT done; the
    field reflects the last time iOS posted, even if the user later
    disconnected.
  - ``last_sync_status``  — coarse outcome of the most recent ingest:
    ``"ok"``     — at least one accepted or tombstoned event.
    ``"empty"``  — zero accepted, zero tombstoned (iOS scanned the
                   window and the calendar was empty).
    NULL         — never synced.

Visible/stored event counts are NOT promoted columns; they're
computed at read time from ``calendar_events`` so they can't drift.

Down-migration drops both columns and the CHECK constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_calendar_source_sync_status"
down_revision = "0040_export_jobs_and_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_sources",
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "calendar_sources",
        sa.Column("last_sync_status", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "calendar_sources_last_sync_status_chk",
        "calendar_sources",
        "last_sync_status IS NULL OR last_sync_status IN ('ok','empty')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "calendar_sources_last_sync_status_chk", "calendar_sources",
    )
    op.drop_column("calendar_sources", "last_sync_status")
    op.drop_column("calendar_sources", "last_sync_at")
