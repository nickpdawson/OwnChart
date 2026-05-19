"""Export skeleton — export_jobs + export_files tables (M02 Slice 4).

Revision ID: 0040_export_jobs_and_files
Revises: 0036_calendar_tables
Create Date: 2026-05-19

Beta 1 Milestone 02, Slice 4 — export skeleton (BE-9).

The migration gap 0037-0039 is intentional — reserved during Slice 1
closeout for any Slice 2 / Slice 3 follow-up. Slice 4 keeps 0040 as
its base number per the original plan.

Two tables, both born multi-person-aware (NOT NULL person_record_id
from the start — same pattern as Slice 3's calendar_*).

export_jobs:
  - One row per user-initiated export request.
  - person_record_id stamps which record the export covers — a
    caregiver who switches active records and requests an export
    gets a NEW job under the new record, not a merged export.
  - status ∈ {pending, running, completed, failed} — pending at
    create, running once the worker picks it up, completed or
    failed at finish. (M03 may add `cancelled`; out of scope for
    Slice 4.)
  - requested_format ∈ {ownchart_json, txt, all} — "all" produces
    both files under one job, per PM revised Group-C ("TXT is in
    Beta 1 user-visible minimum, not deferred").
  - expires_at is the hard 72-hour TTL (PM C-6). Set at completion
    time. A separate purge worker hard-deletes the on-disk file
    and the row after expiry — same soft-then-hard pattern as
    Slice 3's calendar tombstones.

export_files:
  - One row per file produced under an export_job (so a job with
    requested_format='all' has two rows; 'ownchart_json' or 'txt'
    alone has one).
  - file_type ∈ {ownchart_json, txt} for Slice 4. Pictal JSON and
    CCDA land in M03+ (per PM revised Group-C).
  - storage_uri is file:// scheme today; could become s3://, etc.
    in a later slice. byte_size + sha256 captured at write time so
    the download path can set Content-Length + Etag without
    re-reading the file.
  - person_record_id is denormalized from the parent job for
    record-scoped SELECTs that don't want to JOIN. Same pattern as
    evidence_anchors → source_documents in the alpha schema.

Indexes (one per real query path):
  - export_jobs: (person_record_id, requested_at DESC) WHERE
    deleted_at IS NULL for the "list my exports" UI.
  - export_jobs: (expires_at) WHERE status='completed' AND
    deleted_at IS NULL for the 72h purge worker scan.
  - export_files: (export_job_id) for the per-job download lookup;
    (person_record_id) for the record-scoped sweep.

NOT NULL on: person_record_id (both), user_id, requested_at,
requested_format, status (jobs); export_job_id, person_record_id,
file_type, storage_uri (files).

Status semantics on the status column are pinned by CHECK constraint
so an iOS bug can't write a junk value that would break the worker's
dispatch logic.

Forward-only migration. Reversible via drop_table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic revision identifiers.
revision = "0040_export_jobs_and_files"
down_revision = "0036_calendar_tables"
branch_labels = None
depends_on = None


_JOB_STATUSES = ("pending", "running", "completed", "failed")
_REQUESTED_FORMATS = ("ownchart_json", "txt", "all")
_FILE_TYPES = ("ownchart_json", "txt")


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "person_record_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("requested_format", sa.String(32), nullable=False),
        sa.Column(
            "status", sa.String(16),
            nullable=False, server_default="pending",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="export_jobs_status_chk",
        ),
        sa.CheckConstraint(
            "requested_format IN ('ownchart_json','txt','all')",
            name="export_jobs_requested_format_chk",
        ),
    )
    op.create_index(
        "export_jobs_record_recent_idx",
        "export_jobs",
        ["person_record_id", "requested_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
        postgresql_ops={"requested_at": "DESC"},
    )
    op.create_index(
        "export_jobs_expiry_sweep_idx",
        "export_jobs",
        ["expires_at"],
        postgresql_where=sa.text(
            "status = 'completed' AND deleted_at IS NULL"
        ),
    )

    op.create_table(
        "export_files",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "export_job_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("export_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "person_record_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_type", sa.String(32), nullable=False),
        sa.Column("storage_uri", sa.String(1024), nullable=False),
        sa.Column("byte_size", sa.BigInteger),
        sa.Column("sha256", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "file_type IN ('ownchart_json','txt')",
            name="export_files_file_type_chk",
        ),
        sa.UniqueConstraint(
            "export_job_id", "file_type",
            name="export_files_job_type_uq",
        ),
    )
    op.create_index(
        "export_files_record_idx",
        "export_files",
        ["person_record_id"],
    )


def downgrade() -> None:
    op.drop_index("export_files_record_idx", table_name="export_files")
    op.drop_table("export_files")
    op.drop_index("export_jobs_expiry_sweep_idx", table_name="export_jobs")
    op.drop_index("export_jobs_record_recent_idx", table_name="export_jobs")
    op.drop_table("export_jobs")
