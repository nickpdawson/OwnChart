"""Multi-person foundations — add nullable person_record_id to record-bearing tables.

Revision ID: 0029_person_record_id_columns
Revises: 0028_seed_self_records
Create Date: 2026-05-17

Beta 1 Milestone 02, Slice 1, Phase A step 3.

Adds `person_record_id UUID REFERENCES person_records(id)` (nullable
for now) to every record-bearing table. NOT NULL + composite indexes
land in 0031, after 0030 backfills every row.

Scope (14 tables, per BE-2 audit):

  Direct-owned (today filters by user_id / owner_user_id):
    - source_documents
    - conversations
    - conversation_messages
    - episodes
    - sensemaking_jobs
    - sensemaking_candidates
    - extraction_jobs
    - brief_messages
    - healthkit_cursors
    - user_assertions
    - audit_events

  Indirectly scoped (denormalized for query simplicity):
    - extracted_facts        (currently chained via evidence_anchor_ids
                              → source_documents.owner_user_id)
    - evidence_anchors       (currently chained via source_documents)

  Currently global, becoming record-scoped:
    - health_events          (composes extracted_facts; backfilled
                              via the fact path in 0030)

Tables NOT touched here:
  - topics + topic_briefs  → handled in migration 0032 (per-record
                              uniqueness migration; topics need a
                              clone-per-record step, not a simple
                              backfill)
  - device_tokens, oauth_sessions, provider_connections,
    llm_provider_credentials, user_settings → stay user-scoped per
    BE-1; user identity not record identity.
  - users, model_runs, provider_connectors → global vocabulary +
    audit catalogs; no scoping change.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0029_person_record_id_columns"
down_revision: Union[str, None] = "0028_seed_self_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# All tables that need person_record_id added in this phase.
# Ordered by approximate row volume (smallest first) so a partial
# failure leaves the largest tables either fully migrated or
# untouched.
_TABLES_TO_SCOPE: tuple[str, ...] = (
    "brief_messages",
    "user_assertions",
    "extraction_jobs",
    "sensemaking_jobs",
    "sensemaking_candidates",
    "healthkit_cursors",
    "conversation_messages",
    "conversations",
    "episodes",
    "health_events",
    "evidence_anchors",
    "audit_events",
    "source_documents",
    "extracted_facts",
)


def upgrade() -> None:
    for table in _TABLES_TO_SCOPE:
        op.add_column(
            table,
            sa.Column(
                "person_record_id",
                UUID(as_uuid=True),
                sa.ForeignKey("person_records.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    # Reverse order so FKs unwind cleanly.
    for table in reversed(_TABLES_TO_SCOPE):
        op.drop_column(table, "person_record_id")
