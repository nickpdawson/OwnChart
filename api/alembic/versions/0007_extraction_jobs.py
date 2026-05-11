"""Extraction jobs — background-job pattern for Claude vision.

Revision ID: 0007_extraction_jobs
Revises: 0006_classify_template_noise
Create Date: 2026-05-09

Vision extraction over an 11-page PDF takes ~6 minutes serially. Doing
that as a foreground HTTP request hits the Next.js proxy timeout, leaves
the user staring at a spinner-of-mystery, and tempts double-clicks that
end up writing duplicate facts.

Background jobs solve all three: POST returns a job_id immediately, an
Arq worker processes pages and commits per-page, the UI polls for
real progress, and a partial unique index on (source_document_id,
status='running') prevents concurrent runs against the same source.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_extraction_jobs"
down_revision: Union[str, None] = "0006_classify_template_noise"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "extraction_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # pending | running | completed | failed | cancelled
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("total_pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completed_pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("facts_added", sa.Integer, nullable=False, server_default="0"),
        # Per-page errors: [{"page": 4, "error": "..."}]
        sa.Column(
            "page_errors",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Optional subset of pages to extract; null means all.
        sa.Column("only_pages", postgresql.JSONB, nullable=True),
        sa.Column("patient_context", sa.String, nullable=True),
        sa.Column("arq_job_id", sa.String(64), nullable=True),
        # Overall failure (worker crashed, consent revoked mid-run, etc.).
        sa.Column("error", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Enforce: one in-flight job per source. Lets the POST handler return 409
    # cleanly on a re-click without racing.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_extraction_jobs_one_in_flight
            ON extraction_jobs (source_document_id)
            WHERE status IN ('pending', 'running')
        """
    )

    op.create_index(
        "ix_extraction_jobs_user_status",
        "extraction_jobs",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_jobs_user_status", table_name="extraction_jobs")
    op.drop_index("ix_extraction_jobs_one_in_flight", table_name="extraction_jobs")
    op.drop_table("extraction_jobs")
