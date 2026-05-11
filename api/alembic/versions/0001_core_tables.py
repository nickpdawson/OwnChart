"""core tables

Revision ID: 0001_core_tables
Revises:
Create Date: 2026-05-08

The seven core tables from docs/03-data-ingestion-and-model.md, plus the
`users` table needed for V1 single-tenant auth.

Enables pgvector and pg_trgm. Vector columns are added in a later migration
once the embedding model + dimensionality is locked.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_core_tables"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("phi_consent_granted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "source_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("original_filename", sa.String(512)),
        sa.Column("storage_uri", sa.String(1024), nullable=False),
        sa.Column("hash", sa.String(128), nullable=False),
        sa.Column("mime_type", sa.String(128)),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_system", sa.String(255)),
        sa.Column("source_label", sa.String(255)),
        sa.Column("raw_metadata", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_source_documents_owner_user_id", "source_documents", ["owner_user_id"])
    op.create_index("ix_source_documents_source_type", "source_documents", ["source_type"])
    op.create_index("ix_source_documents_hash", "source_documents", ["hash"])

    op.create_table(
        "evidence_anchors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("anchor_type", sa.String(64), nullable=False),
        sa.Column("page_number", sa.Integer),
        sa.Column("section_path", sa.String(1024)),
        sa.Column("line_range", sa.String(64)),
        sa.Column("bbox", postgresql.JSONB),
        sa.Column("timestamp_range", sa.String(64)),
        sa.Column("text_excerpt", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_evidence_anchors_source_document_id",
        "evidence_anchors",
        ["source_document_id"],
    )

    op.create_table(
        "model_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column(
            "input_source_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("input_hash", sa.String(128)),
        sa.Column("output_hash", sa.String(128)),
        sa.Column("prompt_version", sa.String(255), nullable=False),
        sa.Column("consent_state", sa.Boolean, nullable=False),
        sa.Column("usage", postgresql.JSONB),
        sa.Column("error", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "extracted_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("claim_type", sa.String(64), nullable=False),
        sa.Column("label", sa.String(512), nullable=False),
        sa.Column("description", sa.String),
        sa.Column("date_start", sa.DateTime(timezone=True)),
        sa.Column("date_end", sa.DateTime(timezone=True)),
        sa.Column("date_precision", sa.String(16)),
        sa.Column("body_site", sa.String(128)),
        sa.Column("laterality", sa.String(16)),
        sa.Column("coded_concepts", postgresql.JSONB),
        sa.Column("confidence", sa.Integer),
        sa.Column("review_state", sa.String(16), nullable=False, server_default=sa.text("'needs_review'")),
        sa.Column(
            "evidence_anchor_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("extraction_method", sa.String(64), nullable=False),
        sa.Column(
            "model_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_extracted_claims_claim_type", "extracted_claims", ["claim_type"])
    op.create_index("ix_extracted_claims_review_state", "extracted_claims", ["review_state"])

    op.create_table(
        "user_assertions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "related_claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("extracted_claims.id", ondelete="SET NULL"),
        ),
        sa.Column("assertion_type", sa.String(16), nullable=False),
        sa.Column("canonical_label", sa.String(512)),
        sa.Column("canonical_description", sa.String),
        sa.Column("canonical_date_start", sa.DateTime(timezone=True)),
        sa.Column("canonical_date_end", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_user_assertions_user_id", "user_assertions", ["user_id"])
    op.create_index("ix_user_assertions_related_claim_id", "user_assertions", ["related_claim_id"])

    op.create_table(
        "topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.String),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("description", sa.String),
        sa.Column(
            "related_concepts",
            postgresql.ARRAY(sa.String),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "health_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "topic_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("display_title", sa.String(512), nullable=False),
        sa.Column("display_summary", sa.String),
        sa.Column("date_start", sa.DateTime(timezone=True)),
        sa.Column("date_end", sa.DateTime(timezone=True)),
        sa.Column("date_precision", sa.String(16)),
        sa.Column(
            "source_claim_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column(
            "canonical_assertion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_assertions.id", ondelete="SET NULL"),
        ),
        sa.Column("confidence", sa.Integer),
        sa.Column("review_state", sa.String(16), nullable=False, server_default=sa.text("'needs_review'")),
        sa.Column("visibility_layer", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_health_events_event_type", "health_events", ["event_type"])
    op.create_index("ix_health_events_date_start", "health_events", ["date_start"])
    op.create_index("ix_health_events_review_state", "health_events", ["review_state"])
    op.create_index("ix_health_events_visibility_layer", "health_events", ["visibility_layer"])


def downgrade() -> None:
    op.drop_table("health_events")
    op.drop_table("topics")
    op.drop_table("user_assertions")
    op.drop_table("extracted_claims")
    op.drop_table("model_runs")
    op.drop_table("evidence_anchors")
    op.drop_table("source_documents")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
