"""Add ``users.disabled`` — instance-admin account lockout (Batch C).

Revision ID: 0054_user_disabled
Revises: 0053_model_run_user_cost
Create Date: 2026-07-12

A hosted operator (the Lapland tenant) needs to be able to lock a
tester's account without deleting it — freezing their login AND every
live credential they hold. This is that flag.

  - ``disabled`` BOOLEAN NOT NULL DEFAULT false

Semantics (enforced in ``core/device_auth.py`` + the login path):
a disabled user's cookie session AND device bearer tokens both stop
authenticating; the login endpoint refuses them. Re-enabling clears the
flag; the disable action also bumps ``session_token_version`` so live
cookies die immediately (device tokens die via the ``disabled`` check).

Backfills existing rows to ``false`` (server_default) so enabling this
never bricks an established account.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_user_disabled"
down_revision = "0053_model_run_user_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "disabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "disabled")
