"""Cross-source equivalence_key on extracted_facts (Priority 5).

Revision ID: 0017_equivalence_key
Revises: 0016_healthkit_sync
Create Date: 2026-05-10

Per docs/07 §487-545 (Duplication Doctrine) + §651-664 (Equivalence
Layer): different sources may describe the same real-world thing.
Auto Export and native HealthKit both report daily steps for the
same date; two EHRs report the same flu shot; a CCDA Procedure and
a scanned operative note describe the same surgery.

`equivalence_key` is the source-neutral identity of a fact's
underlying real-world event. Same key across multiple rows means
"these are the same thing seen by different sources" — preserve
all rows (provenance never deleted) but collapse them in
presentation.

V1 populates the key only for **daily-aggregate metrics** (the
highest-overlap case between Auto Export and native HK). Workouts /
sleep / body metrics get NULL for now; those need fuzzier matching
rules (overlap windows, value tolerances). Existing rows stay NULL
— no backfill in this migration.

Index is non-unique by design — duplicates ARE the whole point.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_equivalence_key"
down_revision: Union[str, None] = "0016_healthkit_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extracted_facts",
        sa.Column("equivalence_key", sa.String(256), nullable=True),
    )
    # Partial index — only over rows that have a key (NULL is "no
    # canonical event known"; those rows render individually). Keeps
    # the index small while making GROUP BY equivalence_key fast.
    op.create_index(
        "ix_extracted_facts_equivalence_key",
        "extracted_facts",
        ["equivalence_key"],
        postgresql_where=sa.text("equivalence_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extracted_facts_equivalence_key", table_name="extracted_facts"
    )
    op.drop_column("extracted_facts", "equivalence_key")
