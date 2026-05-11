"""Conversation full-text search.

Revision ID: 0023_conversation_fts
Revises: 0022_conv_ep_providers
Create Date: 2026-05-11 (late afternoon)

Per Nick's Q-B1 (2026-05-11 PM): `GET /api/conversations?q=` needs to
search message bodies, not just titles. Adds a generated tsvector
column on `conversation_messages.content` with a GIN index. Postgres
keeps it current automatically — no application code needs to
maintain it.

Title-search on `conversations.title` stays as an ILIKE — small
table, no point in another index.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_conversation_fts"
down_revision: Union[str, None] = "0022_conv_ep_providers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Generated column so we don't need a trigger and don't have to
    # update from the application. `english` is fine for V1; can swap
    # to a per-user config later via a `regconfig` lookup.
    op.execute(
        "ALTER TABLE conversation_messages "
        "ADD COLUMN search_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED"
    )
    op.execute(
        "CREATE INDEX ix_conversation_messages_search_tsv "
        "ON conversation_messages USING GIN (search_tsv)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_conversation_messages_search_tsv")
    op.execute("ALTER TABLE conversation_messages DROP COLUMN IF EXISTS search_tsv")
