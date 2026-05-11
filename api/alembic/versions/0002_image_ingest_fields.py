"""image-ingest fields on source_documents and model_runs

Revision ID: 0002_image_ingest_fields
Revises: 0001_core_tables
Create Date: 2026-05-08

Adds the schema delta from docs/03 for life-event photo ingestion:

  source_documents:
    + captured_at              when the depicted moment happened (EXIF DateTimeOriginal)
    + exif_metadata            full EXIF dict (JSONB)
    + user_supplied_event_date user-stated event date
    + user_supplied_caption    user-authored caption

  model_runs:
    + prompt_artifact_path     filesystem pointer to the materialized prompt+inputs
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_image_ingest_fields"
down_revision: Union[str, None] = "0001_core_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source_documents", sa.Column("captured_at", sa.DateTime(timezone=True)))
    op.add_column("source_documents", sa.Column("exif_metadata", postgresql.JSONB))
    op.add_column("source_documents", sa.Column("user_supplied_event_date", sa.DateTime(timezone=True)))
    op.add_column("source_documents", sa.Column("user_supplied_caption", sa.String))

    op.add_column("model_runs", sa.Column("prompt_artifact_path", sa.String(1024)))


def downgrade() -> None:
    op.drop_column("model_runs", "prompt_artifact_path")
    op.drop_column("source_documents", "user_supplied_caption")
    op.drop_column("source_documents", "user_supplied_event_date")
    op.drop_column("source_documents", "exif_metadata")
    op.drop_column("source_documents", "captured_at")
