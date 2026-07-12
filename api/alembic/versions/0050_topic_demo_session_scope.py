"""Add topics.scope — demo per-visitor isolation marker.

Revision ID: 0050_topic_demo_session_scope
Revises: 0049_user_session_token_version
Create Date: 2026-07-11

MED-7 (demo cross-visitor leak). Topics created by a demo visitor's
``save-as-topic`` were scoped only by ``person_record_id`` — and every
visitor of the shared ``demo@ownchart.me`` account is on the SAME
person record. So visitor A's dossier leaked into visitor B's topics
list / stats dashboard / fact-context / Home dossier targets.

This adds a nullable JSONB ``scope`` column mirroring
``conversations.scope`` / ``episodes.payload``. NULL for every seeded
or non-demo Topic (unchanged, visible to everyone); demo-created
Topics carry ``{"demo_session_id": "<uuid>"}`` and Topic readers gate
on it. Nullable with no default — non-demo behavior is identical to
before this migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0050_topic_demo_session_scope"
down_revision = "0049_user_session_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "topics",
        sa.Column("scope", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("topics", "scope")
