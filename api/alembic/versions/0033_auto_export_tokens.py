"""Auto Export per-(user, person_record) tokens (PM A-2 option C).

Revision ID: 0033_auto_export_tokens
Revises: 0032_topics_per_record_uniqueness
Create Date: 2026-05-17

Beta 1 Milestone 02, Slice 1 batch.

Per PM A-2 (2026-05-17): Auto Export bearer tokens become
per-(user, person_record). Each token is provisioned via a future
Settings → Auto Export UI; the legacy `OWNCHART_AUTO_EXPORT_TOKEN`
env var continues to work IFF the instance has exactly one
person_record (a clear compat boundary so single-record self-hosters
don't have to migrate).

Schema mirrors the MCP-token pattern from BE-6 §"Storage":
  - `token_hash` stores sha256 of the raw token; the raw token is
    surfaced to the user once at creation time and never again.
  - `scopes TEXT[]` is forward-looking — Beta 1 is just `['push']`.
  - Soft delete via `revoked_at`.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0033_auto_export_tokens"
down_revision: Union[str, None] = "0032_topics_per_record_uniqueness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auto_export_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("person_record_id", UUID(as_uuid=True),
                  sa.ForeignKey("person_records.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False,
                  unique=True),
        sa.Column("scopes", sa.ARRAY(sa.String(length=32)),
                  nullable=False,
                  server_default=sa.text("ARRAY['push']::text[]")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "CREATE INDEX auto_export_tokens_user_active_idx "
        "ON auto_export_tokens(user_id) WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX auto_export_tokens_record_active_idx "
        "ON auto_export_tokens(person_record_id) WHERE revoked_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auto_export_tokens_record_active_idx")
    op.execute("DROP INDEX IF EXISTS auto_export_tokens_user_active_idx")
    op.drop_table("auto_export_tokens")
