"""HealthKit sync cursors table (PR2).

Revision ID: 0016_healthkit_sync
Revises: 0015_device_tokens
Create Date: 2026-05-10

Per (user, device_token, HK-identifier) anchor for resumable native
iOS HealthKit sync. Anchor blob is opaque bytes (Apple's
HKQueryAnchor.archivedData). One row per device per identifier; a
re-paired phone gets its own anchor for the same identifier so it
doesn't share backfill state with the prior device.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_healthkit_sync"
down_revision: Union[str, None] = "0015_device_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "healthkit_sync_cursors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "device_token_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("device_tokens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("identifier", sa.String(128), nullable=False),
        sa.Column("anchor_blob", sa.LargeBinary, nullable=True),
        sa.Column("last_sample_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_strategy", sa.String(32), nullable=True),
        sa.Column("sample_count", sa.Integer, nullable=False, server_default="0"),
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
        sa.UniqueConstraint(
            "user_id", "device_token_id", "identifier",
            name="uq_hkcursor_user_dev_id",
        ),
    )
    op.create_index(
        "ix_healthkit_sync_cursors_user_id",
        "healthkit_sync_cursors",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_healthkit_sync_cursors_user_id",
        table_name="healthkit_sync_cursors",
    )
    op.drop_table("healthkit_sync_cursors")
