"""Add parent_source_document_id to source_documents (#53).

Revision ID: 0014_source_parent_id
Revises: 0013_drop_sleep_rest_alias
Create Date: 2026-05-10

Per docs/03 lane 3: "Preserve the parent archive/package unchanged
when present, then create child SourceDocument records for each
parsed XML." Multi-file CCDA upload (#53 V1) doesn't have a parent
archive — every child gets parent_id NULL — but Epic IHE_XDM bundle
ingestion (the natural follow-on; QA 2C.7-2C.9) will. Add the column
now so bundle support doesn't need a coordinated schema change.

Self-referential nullable FK with ON DELETE SET NULL: deleting a
parent archive doesn't cascade-delete its children (we never want
to lose source evidence), it just orphans them.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_source_parent_id"
down_revision: Union[str, None] = "0013_drop_sleep_rest_alias"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_documents",
        sa.Column(
            "parent_source_document_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_source_documents_parent_source_document_id",
        "source_documents",
        ["parent_source_document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_documents_parent_source_document_id")
    op.drop_column("source_documents", "parent_source_document_id")
