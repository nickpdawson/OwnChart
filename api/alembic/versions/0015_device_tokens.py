"""Device tokens (PR1) + client_sample_key on extracted_facts (PR2 prep).

Revision ID: 0015_device_tokens
Revises: 0014_source_parent_id
Create Date: 2026-05-10

PR1 needs `device_tokens` for native iOS pairing. PR2 needs a partial
unique index on `(owner — via source — user)` + `client_sample_key`
for HealthKit idempotency. Bundling both into one migration so the
iOS-app blocker lands as a single Alembic step.

`client_sample_key` is nullable; existing facts (CCDA, photos, Auto
Export) keep it NULL and the unique constraint doesn't apply to them
(partial index with `WHERE client_sample_key IS NOT NULL`).

Per-user scoping in the unique index goes through the first-anchor's
source_document for now — we don't add `owner_user_id` to
`extracted_facts` in this round (deferred until V2 multi-tenant). The
HealthKit sync route enforces user scoping at the application layer
when it inserts; the partial unique constraint is on
`client_sample_key` alone for V1 single-tenant.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_device_tokens"
down_revision: Union[str, None] = "0014_source_parent_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hashed_token", sa.String(128), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_device_tokens_user_id", "device_tokens", ["user_id"]
    )
    op.create_index(
        "ix_device_tokens_hashed_token",
        "device_tokens",
        ["hashed_token"],
        unique=True,
    )
    op.create_index(
        "ix_device_tokens_revoked_at", "device_tokens", ["revoked_at"]
    )

    # PR2 prep — partial unique index on client_sample_key for
    # HealthKit idempotency. Existing facts (CCDA, photos, Auto Export)
    # have client_sample_key=NULL and the constraint doesn't bind.
    op.add_column(
        "extracted_facts",
        sa.Column("client_sample_key", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_extracted_facts_client_sample_key",
        "extracted_facts",
        ["client_sample_key"],
        unique=True,
        postgresql_where=sa.text("client_sample_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_extracted_facts_client_sample_key", table_name="extracted_facts")
    op.drop_column("extracted_facts", "client_sample_key")
    op.drop_index("ix_device_tokens_revoked_at", table_name="device_tokens")
    op.drop_index("ix_device_tokens_hashed_token", table_name="device_tokens")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_table("device_tokens")
