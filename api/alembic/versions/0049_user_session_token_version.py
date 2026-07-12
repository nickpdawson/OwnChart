"""Add users.session_token_version — web session revocation lever.

Revision ID: 0049_user_session_token_version
Revises: 0048_client_sample_key_per_record
Create Date: 2026-07-11

HIGH-5 (credential lifecycle). The web session cookie is now signed
with ``URLSafeTimedSerializer`` (time-based expiry) AND carries a
``stv`` field re-checked on every cookie-authenticated request. This
column is the server-side counterpart: bumping
``users.session_token_version`` invalidates every outstanding web
session for that user (all live cookies still carry the old ``stv``),
which is exactly what a password change does. Device bearer tokens are
unaffected — they are separately revocable via ``device_tokens``.

Backfill: every existing user starts at 0, matching the default that
freshly-issued cookies embed. NOT NULL with a server default so no
application code has to special-case a NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_user_session_token_version"
down_revision = "0048_client_sample_key_per_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "session_token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "session_token_version")
