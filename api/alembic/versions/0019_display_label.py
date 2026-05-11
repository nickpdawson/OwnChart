"""LLM-assisted candidate display labels on extracted_facts (R5).

Revision ID: 0019_display_label
Revises: 0018_review_reasons
Create Date: 2026-05-10

Per the product/design team direction (2026-05-10) + Nick's R5
constraints: SNOMED-style clinical labels ("PLMT ADJUSTABLE SUTR
STRABISMUS") are technically correct but jargon. Translate to
patient English ("Adjustable suture an eye surgery
(placement)") via LLM — but **never overwrite the original
label**. Add a candidate `display_label` that the UI prefers when
present, with `display_label_method` tracking how it was produced
('llm_v1' for now; future heuristic / human-edited paths get their
own values).

Original `label` remains immutable: source-of-truth for retrieval,
audit, and any future relabel revision. `display_label` is the
patient-facing rendering and can be regenerated/improved without
touching ingest.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_display_label"
down_revision: Union[str, None] = "0018_review_reasons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extracted_facts",
        sa.Column("display_label", sa.String(512), nullable=True),
    )
    op.add_column(
        "extracted_facts",
        sa.Column("display_label_method", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extracted_facts", "display_label_method")
    op.drop_column("extracted_facts", "display_label")
