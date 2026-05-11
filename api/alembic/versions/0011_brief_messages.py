"""brief_messages — threaded follow-up on a dossier brief.

Revision ID: 0011_brief_messages
Revises: 0010_topic_briefs
Create Date: 2026-05-09

Per docs/01 + the locked-in product decision (docs/05), the brief is
not a one-shot summary — it's the start of a research-partner
conversation about a dossier. Users read the brief, then ask
follow-ups ("what's the deal with the 1999 planned surgery?",
"did the 2004 surgery achieve what they expected?") and the system
replies with citations from the dossier's facts.

The thread is anchored to the topic, not to a single brief
generation: when the user regenerates the brief later, the
conversation continues. Each message records which brief was
current when it was sent so we can reconstruct what the model saw
at that moment for audit purposes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_brief_messages"
down_revision: Union[str, None] = "0010_topic_briefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "brief_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "topic_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("topics.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Which brief was current when this message was authored.
        # SET NULL because old briefs may be cleaned up but we want to
        # keep the conversation history.
        sa.Column(
            "topic_brief_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("topic_briefs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # 'user' | 'assistant'
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.String, nullable=False),
        # On assistant messages: list of {fact_id, note}.
        sa.Column(
            "citations",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # On assistant messages: how many facts the retriever pulled.
        sa.Column("retrieved_fact_count", sa.Integer, nullable=True),
        sa.Column(
            "model_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("safety_response", sa.String, nullable=True),
        sa.Column("error", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_brief_messages_topic_created",
        "brief_messages",
        ["topic_id", sa.text("created_at ASC")],
    )


def downgrade() -> None:
    op.drop_index("ix_brief_messages_topic_created", table_name="brief_messages")
    op.drop_table("brief_messages")
