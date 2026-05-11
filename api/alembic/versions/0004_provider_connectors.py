"""SMART on FHIR provider connectors + connections + oauth sessions

Revision ID: 0004_provider_connectors
Revises: 0003_topic_slug_and_indexes
Create Date: 2026-05-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_provider_connectors"
down_revision: Union[str, None] = "0003_topic_slug_and_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_connectors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("ehr_vendor", sa.String(32)),
        sa.Column("fhir_base", sa.String(1024), nullable=False),
        sa.Column("smart_config_url", sa.String(1024)),
        sa.Column("authorize_endpoint", sa.String(1024)),
        sa.Column("token_endpoint", sa.String(1024)),
        sa.Column("client_id", sa.String(255)),
        sa.Column(
            "scopes",
            sa.String(1024),
            nullable=False,
            server_default="openid fhirUser launch/patient patient/*.read",
        ),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("raw_config", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "provider_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_connectors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("patient_fhir_id", sa.String(255)),
        sa.Column("patient_display_name", sa.String(512)),
        sa.Column("access_token_enc", sa.LargeBinary),
        sa.Column("refresh_token_enc", sa.LargeBinary),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("scope_granted", sa.String(1024)),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'connected'")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String),
        sa.Column("cached_resource_counts", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_provider_connections_user_id", "provider_connections", ["user_id"])
    op.create_index("ix_provider_connections_connector_id", "provider_connections", ["connector_id"])

    op.create_table(
        "oauth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pkce_verifier", sa.String(255), nullable=False),
        sa.Column("redirect_back_to", sa.String(512)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_oauth_sessions_user_id", "oauth_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_oauth_sessions_user_id", table_name="oauth_sessions")
    op.drop_table("oauth_sessions")
    op.drop_index("ix_provider_connections_connector_id", table_name="provider_connections")
    op.drop_index("ix_provider_connections_user_id", table_name="provider_connections")
    op.drop_table("provider_connections")
    op.drop_table("provider_connectors")
