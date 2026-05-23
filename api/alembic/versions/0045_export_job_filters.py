"""ExportJob.filters — Beta 1 Section D Phase 1.

Revision ID: 0045_export_job_filters
Revises: 0044_extracted_fact_date_provenance
Create Date: 2026-05-23

Stores the request-time filter envelope on the ExportJob row so:

  - The runner can read filters when picking up the job (snapshot
    builder needs them too).
  - The user-facing list endpoint can echo what was exported, so
    "Export from May 2026 (last 90 days, clinical + body signals,
    JSON)" reads honestly back to the user.
  - The audit detail can include the filter envelope for the
    EXPORT_REQUESTED event.

Shape:

    {
      "date_range_kind": "all" | "last_90d" | "last_1y" | "custom",
      "date_range_start": "<iso8601>" | null,
      "date_range_end":   "<iso8601>" | null,
      "domains": ["clinical", "body_signals", "calendar"]
    }

Nullable column — pre-Phase-1 jobs (the ones already in the DB at
deploy time) have filters=NULL, treated as "no filters, full
record." The runner + snapshot builder default to the full-record
shape when filters is absent, matching pre-Phase-1 behavior.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045_export_job_filters"
down_revision = "0044_extracted_fact_date_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "export_jobs",
        sa.Column(
            "filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("export_jobs", "filters")
