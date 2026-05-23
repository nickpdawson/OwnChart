"""Invitations table — FU-MULTITENANT-ONBOARDING.

Revision ID: 0043_invitations
Revises: 0042_calendar_google_and_history_window
Create Date: 2026-05-22

Beta 1 follow-up to Section B (record switcher). Section B let
existing memberships be navigated; this migration lays the schema
for creating new memberships safely via owner-issued invitations.

Two invite shapes share one table, gated by a XOR check:

  - `target_person_record_id` set, `create_new_record = false`:
    invitee joins an EXISTING record as the specified role.

  - `target_person_record_id` null, `create_new_record = true`:
    on accept, registration materializes a NEW person_record and
    a fresh owner membership for the invitee.

Tokens are hashed at rest (`token_hash`, argon2id via
`core.security.hash_invite_token`). A non-secret 8-char
`token_lookup_prefix` exists only to make `WHERE prefix = ?`
lookups indexed — verification still runs argon2 against the
hash. Storing the prefix is safe (it's 32 bits of entropy and the
hash protects the remaining 224 bits).

Single-use enforced at acceptance time by a `SELECT FOR UPDATE`
inside the route transaction, plus the partial index below which
makes "active invite for this email" lookups fast without scanning
revoked / accepted rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043_invitations"
down_revision = "0042_calendar_google_and_history_window"
branch_labels = None
depends_on = None


_ROLES = ("viewer", "caregiver", "owner")


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # CITEXT is provided by an extension; keep the column as TEXT
        # and enforce case-insensitive comparison in the application
        # layer (matches how `users.email` is handled — see
        # `auth.py` login lookup).
        sa.Column("invited_email", sa.String(320), nullable=False),
        sa.Column(
            "target_person_record_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "create_new_record", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
        sa.Column(
            "proposed_record_name", sa.String(255),
            nullable=True,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        # argon2id hash of the raw token. Length is ~96 chars.
        sa.Column("token_hash", sa.String(256), nullable=False, unique=True),
        # First 8 chars of the raw token. Lets us index lookups
        # without storing the raw secret. 8 chars of url-safe base64
        # = 48 bits = enough collision-resistance for index speed.
        sa.Column("token_lookup_prefix", sa.String(8), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "accepted_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_at", sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "role IN ('viewer','caregiver','owner')",
            name="invitations_role_chk",
        ),
        # Exactly one of the two target shapes must apply.
        sa.CheckConstraint(
            "(target_person_record_id IS NOT NULL AND create_new_record = false) OR "
            "(target_person_record_id IS NULL     AND create_new_record = true)",
            name="invitations_target_xor_chk",
        ),
        # When creating a new record, role can only be 'owner'.
        # The invitee gets their own record and is its owner; granting
        # a non-owner role on a record that's about to be created
        # would leave the record permanently un-owned. The route layer
        # validates this too; the constraint is belt-and-suspenders.
        sa.CheckConstraint(
            "create_new_record = false OR role = 'owner'",
            name="invitations_new_record_owner_chk",
        ),
    )
    # Index by lookup prefix for fast token-resolution.
    op.create_index(
        "ix_invitations_lookup_prefix",
        "invitations",
        ["token_lookup_prefix"],
    )
    # Partial index for "active invites by email" — used by the
    # admin UI (Outstanding invites listing) and to detect duplicate
    # active invites to the same address.
    op.create_index(
        "ix_invitations_email_active",
        "invitations",
        ["invited_email"],
        postgresql_where=sa.text(
            "accepted_at IS NULL AND revoked_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_invitations_email_active", table_name="invitations")
    op.drop_index("ix_invitations_lookup_prefix", table_name="invitations")
    op.drop_table("invitations")
