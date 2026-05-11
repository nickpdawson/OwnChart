"""topic_briefs — persist Executive Briefs across reloads.

Revision ID: 0010_topic_briefs
Revises: 0009_topic_label_patterns
Create Date: 2026-05-09

Brief generation hits Anthropic Opus and costs $0.10–0.30 per call,
with ~30–60s latency. Re-running on every dossier render is bad
economics and bad UX. Persist the latest brief; let the user
regenerate on demand.

Per docs/05 we version, not regenerate-on-every-source — keeping
the row chain lets the user diff briefs over time as new evidence
lands.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_topic_briefs"
down_revision: Union[str, None] = "0009_topic_label_patterns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "topic_briefs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "topic_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("topics.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "model_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prompt_version", sa.String(255), nullable=False),
        sa.Column("narrative", sa.String, nullable=True),
        sa.Column("well_supported", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("uncertain", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("suggested_questions", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("citations", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("safety_response", sa.String, nullable=True),
        sa.Column("error", sa.String, nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_topic_briefs_topic_generated",
        "topic_briefs",
        ["topic_id", sa.text("generated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_topic_briefs_topic_generated", table_name="topic_briefs")
    op.drop_table("topic_briefs")
