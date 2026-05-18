"""Topics per-record uniqueness (PM A-1).

Revision ID: 0032_topics_per_record_uniqueness
Revises: 0031_person_record_id_not_null
Create Date: 2026-05-17

Beta 1 Milestone 02, Slice 1, separate batch.

Drop the global `UNIQUE(slug)` and `UNIQUE(name)` constraints on
`topics`, add per-record uniqueness instead. Same for
`topic_briefs`: backfill `person_record_id` via the parent topic.

Why a separate migration from 0029–0031:
  - Topics today are globally seeded vocabulary (Activity, Heart,
    Body, Sleep, etc.). Adding `person_record_id` requires a
    *clone-per-record* approach in the general case, not a flat
    backfill. Migration 0029 deliberately skipped them.
  - At the moment this migration runs, every existing instance has
    exactly one person_record (just seeded by 0028), so the
    clone-per-record reduces to "set person_record_id = the one
    self-record for every topic." Easy.
  - The general "clone topics when a new record is created" logic
    lives in the application code path (POST /api/person-records
    handler in Slice 1) — that's not a migration concern.

After this:
  - `topics` carries `person_record_id NOT NULL`.
  - `UNIQUE (person_record_id, slug)` and `UNIQUE (person_record_id, name)`
    replace the global uniques.
  - `topic_briefs.person_record_id` denormalized from topics.

PM resolution A-1 (2026-05-17): approved as foundational for
multi-record. Same dossier name ("Knee") can now exist on two
different person_records without collision.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0032_topics_per_record_uniqueness"
down_revision: Union[str, None] = "0031_person_record_id_not_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- topics ----------------------------------------------------------
    # 1. Add nullable person_record_id.
    op.add_column(
        "topics",
        sa.Column(
            "person_record_id", UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # 2. Backfill: every existing topic → its creator's self-record.
    #    If created_by is NULL (seeded vocabulary rows), fall back to
    #    the first instance-admin's self-record.
    op.execute(
        """
        UPDATE topics t
        SET person_record_id = pr.id
        FROM person_records pr
        WHERE pr.created_by_user_id = t.created_by
          AND pr.is_self = true
          AND t.person_record_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE topics t
        SET person_record_id = (
            SELECT pr.id FROM person_records pr
            JOIN users u ON u.id = pr.created_by_user_id
            WHERE pr.is_self = true AND u.is_instance_admin = true
            ORDER BY u.created_at ASC
            LIMIT 1
        )
        WHERE t.person_record_id IS NULL
        """
    )

    # 3. Fresh-install handling: if there's no instance admin yet
    # (brand-new DB, never had a user signup), the seed topics from
    # migrations 0003 + 0012 are orphans — no record to bind them to.
    # Drop them rather than fail; the first user signup re-seeds
    # per-record topic shells via the route's create-user hook. This
    # path is taken on `alembic upgrade head` against an empty DB
    # (the dry-run case) and on first-deploy operator runs.
    #
    # Online-only — `--sql` render mode has no live connection.
    from alembic import context as _ctx
    if not _ctx.is_offline_mode():
        admin_record_count = op.get_bind().execute(
            sa.text(
                "SELECT COUNT(*) FROM person_records pr "
                "JOIN users u ON u.id = pr.created_by_user_id "
                "WHERE pr.is_self = true AND u.is_instance_admin = true"
            )
        ).scalar() or 0
        if admin_record_count == 0:
            op.execute(
                "DELETE FROM topic_briefs WHERE topic_id IN "
                "(SELECT id FROM topics WHERE person_record_id IS NULL)"
            )
            op.execute("DELETE FROM topics WHERE person_record_id IS NULL")
        # After potential cleanup, every remaining row must have a
        # record binding — otherwise the NOT NULL flip below blows up.
        result = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM topics WHERE person_record_id IS NULL")
        ).scalar()
        if result and result > 0:
            raise RuntimeError(
                f"0032 topics backfill: {result} rows still NULL. "
                "An instance-admin user exists but their self-record "
                "is missing or `topics.created_by` references a user "
                "without a self-record. Investigate before re-running."
            )

    # 4. Drop global UNIQUE constraints on name + slug.
    #    Postgres auto-named them at table-create time. Names follow
    #    `<table>_<col>_key` convention (per 0001_core_tables).
    op.execute("ALTER TABLE topics DROP CONSTRAINT IF EXISTS topics_name_key")
    op.execute("ALTER TABLE topics DROP CONSTRAINT IF EXISTS topics_slug_key")
    # 0003_topic_slug_and_indexes also created a btree index on slug;
    # drop and re-add as composite.
    op.execute("DROP INDEX IF EXISTS ix_topics_slug")

    # 5. Add per-record uniques.
    op.create_unique_constraint(
        "topics_record_slug_uq", "topics",
        ["person_record_id", "slug"],
    )
    op.create_unique_constraint(
        "topics_record_name_uq", "topics",
        ["person_record_id", "name"],
    )
    op.execute(
        "CREATE INDEX ix_topics_record_slug "
        "ON topics(person_record_id, slug)"
    )

    # 6. Flip NOT NULL.
    op.alter_column("topics", "person_record_id", nullable=False)

    # --- topic_briefs ----------------------------------------------------
    # Same shape: add column, backfill via parent topic, flip NOT NULL.
    op.add_column(
        "topic_briefs",
        sa.Column(
            "person_record_id", UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE topic_briefs tb
        SET person_record_id = t.person_record_id
        FROM topics t
        WHERE t.id = tb.topic_id
          AND tb.person_record_id IS NULL
        """
    )
    if not _ctx.is_offline_mode():
        result = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM topic_briefs WHERE person_record_id IS NULL")
        ).scalar()
        if result and result > 0:
            raise RuntimeError(
                f"0032 topic_briefs backfill: {result} rows still NULL. "
                "Likely an orphan brief whose parent topic was lost."
            )
    op.alter_column("topic_briefs", "person_record_id", nullable=False)
    op.execute(
        "CREATE INDEX ix_topic_briefs_record "
        "ON topic_briefs(person_record_id, generated_at DESC)"
    )


def downgrade() -> None:
    # --- topic_briefs ----------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_topic_briefs_record")
    op.drop_column("topic_briefs", "person_record_id")
    # --- topics ----------------------------------------------------------
    op.alter_column("topics", "person_record_id", nullable=True)
    op.execute("DROP INDEX IF EXISTS ix_topics_record_slug")
    op.drop_constraint("topics_record_name_uq", "topics", type_="unique")
    op.drop_constraint("topics_record_slug_uq", "topics", type_="unique")
    # Restore the global uniques. If two records happen to have the
    # same slug after downgrade, this will fail — manual cleanup needed.
    op.create_unique_constraint("topics_slug_key", "topics", ["slug"])
    op.create_unique_constraint("topics_name_key", "topics", ["name"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_topics_slug ON topics(slug)"
    )
    op.drop_column("topics", "person_record_id")
