"""OAuth flow record binding — provider_connections + oauth_sessions get person_record_id.

Revision ID: 0034_oauth_flow_record_binding
Revises: 0033_auto_export_tokens
Create Date: 2026-05-18

Beta 1 Milestone 02, Slice 1 — closeout migration.

Migrations 0029 (column add) and 0031 (NOT NULL flip) intentionally
SKIPPED these two tables per the BE-1 design comment: "device_tokens,
oauth_sessions, provider_connections, llm_provider_credentials,
user_settings → stay user-scoped per BE-1; user identity not record
identity."

Slice 1 Batch 8 (PM A-3 directive) overrode that for the OAuth flow
specifically: the `start_connect` route now signs the active record
into the OAuth state param, and `oauth_callback` binds the resulting
ProviderConnection to that signed value (NOT to whatever active
record the user has switched to mid-flow). For that binding to be
durable across sync calls, the long-lived ProviderConnection needs
a `person_record_id` column too — without it, the route layer's
binding has nowhere to land.

This migration is the schema half of that route change. It is
NULLABLE (not NOT NULL) on both tables for the same reason the
0029→0031 chain is two-step elsewhere: pre-migration rows from
Phase A early adopters exist with no record binding. Those rows
continue to work via the pre-migration-fallback branch in
`sync_connection` (legacy NULL bindings fall back to the active
record); new rows always carry the signed binding.

A future migration (Slice 2+) can backfill these NULL columns
from the existing user-id chain (provider_connections.user_id +
the user's self-record from 0028), then flip NOT NULL on
provider_connections. OAuthSession rows are short-lived
(~10 minute TTL) and don't need a backfill — they age out.

Why a separate migration (not folded into 0029):
  - Keeps 0029's BE-1 doctrine commentary intact and accurate.
  - Makes the PM A-3 override traceable in git history.
  - Lets future operators apply Slice 1 cleanly without
    needing to know the BE-1 design decision was revised.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0034_oauth_flow_record_binding"
down_revision: Union[str, None] = "0033_auto_export_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables in scope for this closeout migration.
_TABLES_TO_BIND: tuple[str, ...] = (
    "oauth_sessions",
    "provider_connections",
)


def upgrade() -> None:
    for table in _TABLES_TO_BIND:
        op.add_column(
            table,
            sa.Column(
                "person_record_id",
                UUID(as_uuid=True),
                sa.ForeignKey("person_records.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
    # Composite index on provider_connections so the list-by-record
    # path in `routes/connectors.list_connectors` doesn't seq-scan
    # for a caregiver who has connections under many records.
    op.execute(
        "CREATE INDEX provider_connections_user_record_idx "
        "ON provider_connections(user_id, person_record_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS provider_connections_user_record_idx")
    for table in reversed(_TABLES_TO_BIND):
        op.drop_column(table, "person_record_id")
