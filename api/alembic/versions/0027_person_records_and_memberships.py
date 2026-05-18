"""Multi-person foundations — person_records, memberships, user role columns.

Revision ID: 0027_person_records_and_memberships
Revises: 0026_episode_merge_stale
Create Date: 2026-05-17

Beta 1 Milestone 02, Slice 1, Phase A step 1.

Adds the table backbone for household/caregiver multi-person:

  - `person_records` — the body/life/health record being analyzed.
    Separate from `users` (login identity).
  - `memberships` — `(user, person_record, role)` triple. role is
    one of `owner` / `caregiver` / `viewer`. Soft-delete via
    `revoked_at` so we keep audit history.
  - `users.is_instance_admin` — server-management privilege.
    Does NOT confer record access; record access is membership-only.
  - `users.display_name` — UI affordance; nullable.
  - `users.default_person_record_id` — fallback active record when
    the request carries no `X-OwnChart-Person-Record` header and no
    session pin. Resolved in `AuthContext`.

Constraints worth highlighting:
  - At most one OWNER per record (partial unique index).
  - Membership uniqueness on `(user_id, person_record_id)`.
  - Role is enforced via CHECK constraint (vs an Enum so future
    role additions don't require a CREATE TYPE migration).

No data motion here. The empty tables sit until 0028 seeds them.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0027_person_records_and_memberships"
down_revision: Union[str, None] = "0026_episode_merge_stale"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Slice 1 closeout (added after Batch 9): widen
    # alembic_version.version_num because the Slice 1 revision IDs
    # (e.g. "0027_person_records_and_memberships" = 35 chars) exceed
    # Alembic's default VARCHAR(32). Without this the very first
    # post-upgrade write of `version_num` fails with
    # StringDataRightTruncationError and rolls back the migration.
    #
    # Safe to run on a fresh DB (no rows) and on the prod DB where
    # the column holds a single ≤32-char value. Idempotent re-run is
    # a no-op because Postgres ALTER COLUMN TYPE is idempotent when
    # the column is already the requested type.
    op.execute(
        "ALTER TABLE alembic_version "
        "ALTER COLUMN version_num TYPE VARCHAR(255)"
    )

    # --- person_records --------------------------------------------------
    op.create_table(
        "person_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("given_names", sa.String(length=255), nullable=True),
        sa.Column("family_name", sa.String(length=255), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("gender", sa.String(length=64), nullable=True),
        sa.Column("is_self", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("disconnected_at", sa.DateTime(timezone=True),
                  nullable=True),
    )
    op.create_index(
        "ix_person_records_created_by",
        "person_records", ["created_by_user_id"],
    )

    # --- memberships -----------------------------------------------------
    op.create_table(
        "memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("person_record_id", UUID(as_uuid=True),
                  sa.ForeignKey("person_records.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("invited_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "role IN ('owner','caregiver','viewer')",
            name="memberships_role_chk",
        ),
        sa.UniqueConstraint(
            "user_id", "person_record_id",
            name="memberships_user_record_uq",
        ),
    )
    # Partial indexes: only active (non-revoked) memberships are hot.
    op.execute(
        "CREATE INDEX memberships_user_active_idx "
        "ON memberships(user_id) WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX memberships_record_active_idx "
        "ON memberships(person_record_id) WHERE revoked_at IS NULL"
    )
    # At most one OWNER per record (active).
    op.execute(
        "CREATE UNIQUE INDEX memberships_one_owner_per_record_idx "
        "ON memberships(person_record_id) "
        "WHERE role = 'owner' AND revoked_at IS NULL"
    )

    # --- users extensions ------------------------------------------------
    # is_instance_admin: server-management role. NOT record-access.
    op.add_column(
        "users",
        sa.Column("is_instance_admin", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )
    # display_name: UI nicety; user can set "Nick" or similar.
    op.add_column(
        "users",
        sa.Column("display_name", sa.String(length=255), nullable=True),
    )
    # default_person_record_id: fallback when the request has no header
    # and no session pin. Nullable — until the user has at least one
    # membership, there's no default.
    op.add_column(
        "users",
        sa.Column(
            "default_person_record_id", UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "default_person_record_id")
    op.drop_column("users", "display_name")
    op.drop_column("users", "is_instance_admin")
    op.execute("DROP INDEX IF EXISTS memberships_one_owner_per_record_idx")
    op.execute("DROP INDEX IF EXISTS memberships_record_active_idx")
    op.execute("DROP INDEX IF EXISTS memberships_user_active_idx")
    op.drop_table("memberships")
    op.drop_index("ix_person_records_created_by", table_name="person_records")
    op.drop_table("person_records")
