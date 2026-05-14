"""Episode display_title + aliases for Named Events.

Revision ID: 0025_episode_aliases
Revises: 0024_native_hk_src_dedupe
Create Date: 2026-05-13

The Named Events directive: a user should be able to rename
"STRABISMUS SCARRING EO MUSC/RSTCV MYOPATHY" to "2026 left eye
surgery" and refer to that Event by name (or any alias) in chat.

Schema:
  - episodes.display_title TEXT (nullable; UI falls back to title)
  - episodes.aliases TEXT[] DEFAULT '{}'
  - GIN index on aliases for fast membership lookup during chat
    resolution.

Backfill: leave display_title NULL and aliases empty on existing
rows. The chat resolver treats both as "no override set."
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_episode_aliases"
down_revision: Union[str, None] = "0024_native_hk_src_dedupe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "episodes",
        sa.Column("display_title", sa.Text(), nullable=True),
    )
    op.add_column(
        "episodes",
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    # GIN index for fast membership lookups during chat resolution.
    # Case-folding happens at query time (we lower() both the user's
    # phrase and the candidate alias before comparing). Postgres has
    # no built-in case-insensitive ARRAY operator, so this index
    # accelerates the exact-match leg; the case-fold leg falls back
    # to a sequential scan over the alias rows that match by GIN.
    op.execute(
        "CREATE INDEX ix_episodes_aliases_gin ON episodes USING gin (aliases)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_episodes_aliases_gin")
    op.drop_column("episodes", "aliases")
    op.drop_column("episodes", "display_title")
