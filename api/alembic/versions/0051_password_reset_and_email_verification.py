"""Password-reset tokens + email-verification columns.

Revision ID: 0051_password_reset_and_email_verification
Revises: 0050_topic_demo_session_scope
Create Date: 2026-07-12

Email/SMTP workstream — the schema half of the hostable invite-only
multi-tenant story:

  1. ``password_reset_tokens`` — single-use, short-lived tokens for the
     unauthenticated forgotten-password flow. Same at-rest shape as
     ``invitations``: argon2id ``token_hash`` (unique) plus an indexed
     8-char ``token_lookup_prefix``. ``consumed_at`` enforces single-use;
     ``expires_at`` bounds the leak window.

  2. ``users.email_verified`` / ``users.email_verified_at`` — the state
     behind the previously-dead ``auth.require_email_verification`` flag.
     New rows default to ``false`` (registration stamps them true on the
     no-verification path). Existing rows are backfilled to ``true`` so
     turning the flag on later never locks out an established account.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_password_reset_and_email_verification"
down_revision = "0050_topic_demo_session_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(256), nullable=False, unique=True),
        sa.Column("token_lookup_prefix", sa.String(8), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id",
        "password_reset_tokens",
        ["user_id"],
    )
    op.create_index(
        "ix_password_reset_tokens_lookup_prefix",
        "password_reset_tokens",
        ["token_lookup_prefix"],
    )

    op.add_column(
        "users",
        sa.Column(
            "email_verified", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: everyone who already has an account predates verification —
    # don't strand them behind the flag if an operator turns it on.
    op.execute("UPDATE users SET email_verified = true")


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email_verified")
    op.drop_index(
        "ix_password_reset_tokens_lookup_prefix",
        table_name="password_reset_tokens",
    )
    op.drop_index(
        "ix_password_reset_tokens_user_id",
        table_name="password_reset_tokens",
    )
    op.drop_table("password_reset_tokens")
