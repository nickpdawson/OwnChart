"""Add ``invitations.pending_accept_user_id`` — mailbox-ownership gate.

Revision ID: 0055_invitation_pending_accept
Revises: 0054_user_disabled
Create Date: 2026-07-20

P0-4 (2026-07-20 release review) — invitation possession is too close to
account ownership. When email verification is required (flag ON + SMTP
configured), registering against an invite must NOT hand the invitee a
live membership before they prove they own the invited mailbox.

This column records the unverified user who consumed an invite during
registration. The membership is deliberately NOT created at that point;
it is materialized only when that user verifies their email
(``POST /api/auth/verify-email``). Because no membership row exists in the
interim, none of the ~10 active-membership queries can mistakenly treat a
pending invitee as active — the "not active until verified" invariant is
enforced by absence, not by a status flag threaded through every query.

  - ``pending_accept_user_id`` UUID NULL FK users(id) ON DELETE SET NULL

Cleared back to NULL once the membership is materialized, so a repeat
verify is idempotent. Nullable + defaults NULL, so existing invites and
the SMTP-off self-host path are entirely unaffected.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0055_invitation_pending_accept"
down_revision = "0054_user_disabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invitations",
        sa.Column(
            "pending_accept_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_invitations_pending_accept_user_id",
        "invitations",
        ["pending_accept_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_invitations_pending_accept_user_id", table_name="invitations")
    op.drop_column("invitations", "pending_accept_user_id")
