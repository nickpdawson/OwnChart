"""Episode dedup + staleness — merged_into_id + intelligence_stale_after.

Revision ID: 0026_episode_merge_stale
Revises: 0025_episode_aliases
Create Date: 2026-05-14

Nick's RC review caught duplicate "2026 left eye surgery" Events
on Home: one created before clinical-note extraction landed, with
a stale summary that said "no operative note found" — and one
created after, with the correct content. Plus the duplicate-event
detection is genuinely useful for any future "save-twice" path.

Schema:
  - episodes.merged_into_id UUID (FK episodes.id, nullable) — when
    set, this Event is a soft-deleted duplicate pointing at the
    canonical Event. Members were copied to the canonical row at
    merge time.
  - episodes.intelligence_stale_after TIMESTAMPTZ (nullable) —
    when a new fact lands in the Event's date window AFTER the
    saved intelligence payload was generated, this gets set to
    the new fact's created_at. UI surfaces a "refresh" affordance.
  - Index on merged_into_id (mostly-NULL) for fast "active events"
    filtering.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0026_episode_merge_stale"
down_revision: Union[str, None] = "0025_episode_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "episodes",
        sa.Column(
            "merged_into_id",
            UUID(as_uuid=True),
            sa.ForeignKey("episodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "episodes",
        sa.Column(
            "intelligence_stale_after",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        "CREATE INDEX ix_episodes_active "
        "ON episodes (user_id) "
        "WHERE merged_into_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_episodes_active")
    op.drop_column("episodes", "intelligence_stale_after")
    op.drop_column("episodes", "merged_into_id")
