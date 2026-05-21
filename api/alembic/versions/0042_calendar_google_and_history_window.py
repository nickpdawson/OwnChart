"""Calendar Google adapter + ICS + history-window — Beta 1 calendar gate stack.

Revision ID: 0042_calendar_google_and_history_window
Revises: 0041_calendar_source_sync_status
Create Date: 2026-05-21

Lands three pieces of the Beta 1 calendar gate stack at the schema layer:

  1. **Adapter allowlist extension.** Drops the Slice 3 CHECK that
     pinned ``calendar_sources.adapter_type = 'ios_eventkit'`` and
     replaces it with ``IN ('ios_eventkit', 'google_calendar', 'ics')``.
     The route + worker layers wire each adapter; the schema just
     reserves the namespace so a future read-only ICS PR can land
     without another migration.

  2. **History-window backend contract** (FU-CAL-HISTORY-WINDOW).
     ``calendar_sources.history_window_back`` is the per-source pick
     from ``{'90d','1y','3y','5y','all'}``. Default ``'90d'`` matches
     today's behavior. Widening triggers client backfill (handled in
     the iOS / Google sync workers); narrowing hides events from
     projections without hard delete (the projector filters by the
     window at read time — no destructive UPDATE on narrow).

  3. **Google OAuth credentials.** New ``calendar_oauth_credentials``
     table. One row per (user, person_record, provider='google',
     google_account_email). Holds the encrypted refresh token (DEK
     pattern from ``core.crypto``), access token expiry, granted
     scope list, and a status state machine matching
     ``provider_connections`` (connected | expired | revoked | error).
     A CalendarSource row points back via ``oauth_credential_id`` so
     one Google account can bind multiple Google calendars without
     re-OAuthing per calendar.

NOT NULL / record-scoping invariants (Slice 1 perimeter):
  - ``calendar_oauth_credentials.person_record_id`` NOT NULL from
    creation (no 0029-style add-then-backfill chain — the table
    is born record-scoped).
  - ``calendar_oauth_credentials.user_id`` NOT NULL so revocation
    can fan out from a user-delete.
  - Refresh token column ``refresh_token_enc`` is ``bytea`` matching
    ``provider_connections``; access token + expiry NULLABLE because
    we may store credentials before the first sync triggers a token
    refresh.

Down-migration drops the new table and reverts the adapter_type
CHECK to ios_eventkit-only. The history_window_back column is
dropped (default-90d so no behavioral change on revert).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042_calendar_google_and_history_window"
down_revision = "0041_calendar_source_sync_status"
branch_labels = None
depends_on = None


_HISTORY_WINDOWS = ("90d", "1y", "3y", "5y", "all")
_OAUTH_STATUSES = ("connected", "expired", "revoked", "error")


def upgrade() -> None:
    # 1. Adapter allowlist — drop the ios_eventkit-only CHECK, install
    #    the wider one. Postgres has no "ALTER CHECK"; drop + add.
    op.drop_constraint(
        "calendar_sources_adapter_type_chk", "calendar_sources",
        type_="check",
    )
    op.create_check_constraint(
        "calendar_sources_adapter_type_chk",
        "calendar_sources",
        "adapter_type IN ('ios_eventkit', 'google_calendar', 'ics')",
    )

    # 2. History-window column. Default '90d' = today's iOS behavior;
    #    no backfill needed for existing rows.
    op.add_column(
        "calendar_sources",
        sa.Column(
            "history_window_back",
            sa.String(8),
            nullable=False,
            server_default="90d",
        ),
    )
    op.create_check_constraint(
        "calendar_sources_history_window_back_chk",
        "calendar_sources",
        "history_window_back IN ('90d','1y','3y','5y','all')",
    )

    # 3. Google OAuth credentials table.
    op.create_table(
        "calendar_oauth_credentials",
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
        sa.Column(
            "person_record_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Reserve the column for future adapters (Outlook OAuth, etc.);
        # today only 'google' is wired. CHECK keeps the namespace tight.
        sa.Column(
            "provider", sa.String(16),
            nullable=False, server_default="google",
        ),
        sa.Column("google_account_email", sa.String(320), nullable=False),
        # AES-256-GCM ciphertext = nonce(12) || ct+tag, per core.crypto.
        sa.Column("refresh_token_enc", sa.LargeBinary, nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary, nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True)),
        # Scopes granted by the user at consent time. Stored as a
        # space-separated string per OAuth 2.0 convention. The route
        # layer verifies this is a subset of READ_ONLY_SCOPES before
        # writing the row.
        sa.Column("scope_granted", sa.String(1024), nullable=False),
        sa.Column(
            "status", sa.String(16),
            nullable=False, server_default="connected",
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(2048)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "provider IN ('google')",
            name="calendar_oauth_credentials_provider_chk",
        ),
        sa.CheckConstraint(
            "status IN ('connected','expired','revoked','error')",
            name="calendar_oauth_credentials_status_chk",
        ),
        # One credential per (user, record, provider, account email).
        # Re-consenting under the same Google account is a no-op
        # upsert that refreshes refresh_token_enc and resets status.
        sa.UniqueConstraint(
            "user_id", "person_record_id", "provider", "google_account_email",
            name="calendar_oauth_credentials_uq",
        ),
    )
    op.create_index(
        "calendar_oauth_credentials_record_idx",
        "calendar_oauth_credentials",
        ["person_record_id"],
        postgresql_where=sa.text("status = 'connected'"),
    )

    # CalendarSource → OAuth credential link. NULLABLE because
    # ios_eventkit sources don't have a credential row; only the
    # google_calendar adapter populates this.
    op.add_column(
        "calendar_sources",
        sa.Column(
            "oauth_credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "calendar_oauth_credentials.id", ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("calendar_sources", "oauth_credential_id")
    op.drop_index(
        "calendar_oauth_credentials_record_idx",
        table_name="calendar_oauth_credentials",
    )
    op.drop_table("calendar_oauth_credentials")

    op.drop_constraint(
        "calendar_sources_history_window_back_chk",
        "calendar_sources",
        type_="check",
    )
    op.drop_column("calendar_sources", "history_window_back")

    op.drop_constraint(
        "calendar_sources_adapter_type_chk",
        "calendar_sources",
        type_="check",
    )
    op.create_check_constraint(
        "calendar_sources_adapter_type_chk",
        "calendar_sources",
        "adapter_type IN ('ios_eventkit')",
    )
