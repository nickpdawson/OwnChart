"""Review reasons + task type + source-only flag (docs/07 Priority 1).

Revision ID: 0018_review_reasons
Revises: 0017_equivalence_key
Create Date: 2026-05-10

Per docs/07 §634-649: every review item needs machine-readable +
human-readable reason text, a priority, a task type, and a flag for
items that are eligible to be marked "source-only context" (kept
searchable from the source page but not promoted into the life
graph).

`source_only` is a new review_state value distinct from `rejected`
(wrong / not useful) and `deferred` (low value but maybe later) —
it means "preserved as source context, intentionally not surfaced
on timelines/dossiers/answers." The retrieval layer hides it from
default dossier views via the existing hidden-states tuple.

Existing rows stay NULL on the new columns. Ingest paths that
classify confidently-low-value content (provider contacts from
fax cover sheets, Auto Export Skipped medications) will populate
reason text at insert time going forward.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_review_reasons"
down_revision: Union[str, None] = "0017_equivalence_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extracted_facts",
        sa.Column("why_needs_review_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "extracted_facts",
        sa.Column("why_needs_review_text", sa.String(512), nullable=True),
    )
    op.add_column(
        "extracted_facts",
        # 1 = highest (conflicts), 7 = lowest (metadata cleanup) per
        # docs/07 §442-450. NULL = uncomputed.
        sa.Column("review_priority", sa.SmallInteger, nullable=True),
    )
    op.add_column(
        "extracted_facts",
        sa.Column("review_task_type", sa.String(48), nullable=True),
    )
    op.add_column(
        "extracted_facts",
        # When true the user sees a "Mark source-only" quick action
        # alongside Confirm/Defer/Reject in the Review Inbox. Set
        # automatically for confidently-low-value extractions like
        # vision-extracted contact/provider names.
        sa.Column(
            "source_context_only_eligible",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("extracted_facts", "source_context_only_eligible")
    op.drop_column("extracted_facts", "review_task_type")
    op.drop_column("extracted_facts", "review_priority")
    op.drop_column("extracted_facts", "why_needs_review_text")
    op.drop_column("extracted_facts", "why_needs_review_code")
