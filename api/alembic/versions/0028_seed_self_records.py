"""Multi-person foundations — seed self-record + owner membership per existing user.

Revision ID: 0028_seed_self_records
Revises: 0027_person_records_and_memberships
Create Date: 2026-05-17

Beta 1 Milestone 02, Slice 1, Phase A step 2.

For each existing `users` row, create exactly one `person_records`
row with `is_self=TRUE` and `display_name='Me'`, plus a
`memberships` row binding the user as `owner` of that record. Set
`users.default_person_record_id` to the new record so AuthContext
resolves without explicit selection.

Additionally: flag the FIRST user (by `created_at ASC`) with
`is_instance_admin=TRUE`. This matches today's "first signup is
owner" behavior — that user becomes the instance admin going
forward.

Idempotent: skips users that already have a self-record (e.g. if
the migration is re-run after a partial failure or in a fresh
environment that already provisioned via the new flow).

Reversible: downgrade drops the seeded rows. Safe to run on demo
and ownchart.dzsec.net because today there's exactly one user per
instance.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_seed_self_records"
down_revision: Union[str, None] = "0027_person_records_and_memberships"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Seed self-records for any user that doesn't already have one.
    # `INSERT ... SELECT ... WHERE NOT EXISTS` keeps the migration
    # idempotent — re-running it doesn't duplicate.
    op.execute(
        """
        INSERT INTO person_records (
            id, display_name, is_self,
            created_by_user_id, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), 'Me', true,
            u.id, now(), now()
        FROM users u
        WHERE NOT EXISTS (
            SELECT 1
            FROM person_records pr
            WHERE pr.created_by_user_id = u.id AND pr.is_self = true
        )
        """
    )

    # Seed owner memberships from the seeded self-records.
    op.execute(
        """
        INSERT INTO memberships (
            id, user_id, person_record_id, role, accepted_at, created_at
        )
        SELECT
            gen_random_uuid(), pr.created_by_user_id, pr.id, 'owner',
            now(), now()
        FROM person_records pr
        WHERE pr.is_self = true
          AND NOT EXISTS (
              SELECT 1 FROM memberships m
              WHERE m.user_id = pr.created_by_user_id
                AND m.person_record_id = pr.id
          )
        """
    )

    # Set default_person_record_id to the self-record for each user
    # who doesn't already have a default set.
    op.execute(
        """
        UPDATE users u
        SET default_person_record_id = (
            SELECT pr.id
            FROM person_records pr
            WHERE pr.created_by_user_id = u.id AND pr.is_self = true
            LIMIT 1
        )
        WHERE u.default_person_record_id IS NULL
        """
    )

    # First user (oldest created_at) gets is_instance_admin=true.
    # Matches today's "first signup is owner" behavior.
    op.execute(
        """
        UPDATE users
        SET is_instance_admin = true
        WHERE id = (
            SELECT id FROM users ORDER BY created_at ASC, id ASC LIMIT 1
        )
        """
    )


def downgrade() -> None:
    # Wind back the is_instance_admin flag.
    op.execute("UPDATE users SET is_instance_admin = false")
    # Drop seeded default_person_record_id pointers.
    op.execute("UPDATE users SET default_person_record_id = NULL")
    # Drop seeded memberships.
    op.execute(
        "DELETE FROM memberships m USING person_records pr "
        "WHERE m.person_record_id = pr.id "
        "  AND pr.is_self = true "
        "  AND pr.created_by_user_id = m.user_id"
    )
    # Drop seeded self-records.
    op.execute("DELETE FROM person_records WHERE is_self = true")
