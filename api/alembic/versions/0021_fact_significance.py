"""User-confirmable fact significance.

Revision ID: 0021_fact_significance
Revises: 0020_sensemaking_and_settings
Create Date: 2026-05-11

Per Nick (2026-05-11 morning): the system is still promoting machine-
visible structure rather than human significance. Fact count, recency,
and same-day-same-source clustering all let a cold-sore refill outrank
a an eye surgery. Add a real significance axis that every patient-
facing surface ranks against.

Taxonomy (lower index = louder in the UI):

  1. major_event
  2. major_diagnosis
  3. major_procedure
  4. major_medication
  5. major_activity_lifestyle
  6. background
  7. source_only

`significance_source ∈ {user, llm, heuristic, default}` tracks who set
the value. User overrides always win. Heuristic is the initial pass at
ingest; LLM is sensemaking-assigned; default is "not yet classified."

NOT a constraint check at the DB layer — we keep the column TEXT so a
future taxonomy addition doesn't require a column migration.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_fact_significance"
down_revision: Union[str, None] = "0020_sensemaking_and_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extracted_facts",
        sa.Column("significance", sa.String(48), nullable=True),
    )
    op.add_column(
        "extracted_facts",
        sa.Column("significance_source", sa.String(16), nullable=True),
    )
    op.add_column(
        "extracted_facts",
        sa.Column("significance_set_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Composite-ish index for "give me all major_* facts, recent first"
    # — the ranking pattern used by Home / Timeline / Discover. Plain
    # btree on significance is good enough; date is separately indexed.
    op.create_index(
        "ix_extracted_facts_significance",
        "extracted_facts",
        ["significance"],
    )


def downgrade() -> None:
    op.drop_index("ix_extracted_facts_significance", table_name="extracted_facts")
    op.drop_column("extracted_facts", "significance_set_at")
    op.drop_column("extracted_facts", "significance_source")
    op.drop_column("extracted_facts", "significance")
