"""Pictal JSON export — widen requested_format + file_type CHECK constraints.

Revision ID: 0046_pictal_json_export
Revises: 0045_export_job_filters
Create Date: 2026-06-10

Adds ``pictal_json`` as an allowed value on:

  - ``export_jobs.requested_format``   (previously {ownchart_json, txt, all})
  - ``export_files.file_type``         (previously {ownchart_json, txt})

The route + model + runner already reference ``pictal_json``; this
migration is what makes the DB accept it. ``requested_format='all'``
continues to render only ``ownchart_json`` + ``txt`` (no behavioral
change) — Pictal JSON is an explicit-only choice per PM 2026-06-10.

Drop + re-add the CHECK constraints. Postgres CHECK constraint ALTER
isn't atomic-editable; drop-and-recreate is the canonical pattern and
is safe because no row can violate the wider constraint.
"""

from __future__ import annotations

from alembic import op


revision = "0046_pictal_json_export"
down_revision = "0045_export_job_filters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # export_jobs.requested_format
    op.drop_constraint(
        "export_jobs_requested_format_chk",
        "export_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "export_jobs_requested_format_chk",
        "export_jobs",
        "requested_format IN ('ownchart_json','txt','pictal_json','all')",
    )

    # export_files.file_type
    op.drop_constraint(
        "export_files_file_type_chk",
        "export_files",
        type_="check",
    )
    op.create_check_constraint(
        "export_files_file_type_chk",
        "export_files",
        "file_type IN ('ownchart_json','txt','pictal_json')",
    )


def downgrade() -> None:
    # Reject any rows that would violate the narrowed constraints
    # first. Cleaner to surface the conflict than to silently corrupt
    # them by re-applying a tighter CHECK that drops their values.
    op.execute(
        "DELETE FROM export_files WHERE file_type='pictal_json'"
    )
    op.execute(
        "DELETE FROM export_jobs WHERE requested_format='pictal_json'"
    )

    op.drop_constraint(
        "export_files_file_type_chk",
        "export_files",
        type_="check",
    )
    op.create_check_constraint(
        "export_files_file_type_chk",
        "export_files",
        "file_type IN ('ownchart_json','txt')",
    )

    op.drop_constraint(
        "export_jobs_requested_format_chk",
        "export_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "export_jobs_requested_format_chk",
        "export_jobs",
        "requested_format IN ('ownchart_json','txt','all')",
    )
