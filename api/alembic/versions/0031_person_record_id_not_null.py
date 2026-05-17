"""Multi-person foundations — NOT NULL constraints + composite indexes.

Revision ID: 0031_person_record_id_not_null
Revises: 0030_backfill_person_record_id
Create Date: 2026-05-17

Beta 1 Milestone 02, Slice 1, Phase A step 5.

After 0030 backfilled every record-bearing row, flip
`person_record_id` to NOT NULL (except on `audit_events`, which
retains nullability — see rationale below) and add the composite
indexes the perimeter queries will use.

Tables flipped to NOT NULL:
  source_documents, conversations, conversation_messages, episodes,
  sensemaking_jobs, sensemaking_candidates, extraction_jobs,
  brief_messages, healthkit_cursors, user_assertions,
  evidence_anchors, extracted_facts, health_events.

Tables that stay nullable:
  audit_events.person_record_id — system audit entries (e.g. a
  failed login at /api/auth/login) have no target record and no
  user_id. Forcing person_record_id NOT NULL would break these
  rows. Acceptable per PM A-5: the audit row records what
  happened; record scoping is a filter, not a foreign key.

Composite indexes added (hot retrieval paths):
  - source_documents       (person_record_id, acquired_at DESC)
  - extracted_facts        (person_record_id, date_start DESC) WHERE date_start IS NOT NULL
  - extracted_facts        (person_record_id, fact_type)
  - conversations          (person_record_id, last_message_at DESC NULLS LAST)
  - episodes               (person_record_id, date_start DESC) WHERE merged_into_id IS NULL
  - health_events          (person_record_id, date_start DESC) WHERE date_start IS NOT NULL
  - evidence_anchors       (person_record_id)
  - audit_events           (person_record_id, created_at DESC) — partial,
                            WHERE person_record_id IS NOT NULL.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0031_person_record_id_not_null"
down_revision: Union[str, None] = "0030_backfill_person_record_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NOT_NULL_TABLES: tuple[str, ...] = (
    "source_documents",
    "conversations",
    "conversation_messages",
    "episodes",
    "sensemaking_jobs",
    "sensemaking_candidates",
    "extraction_jobs",
    "brief_messages",
    "healthkit_cursors",
    "user_assertions",
    "evidence_anchors",
    "extracted_facts",
    "health_events",
)


def upgrade() -> None:
    # Flip NOT NULL on every required table. If any row is still NULL,
    # Postgres raises and we surface a clear failure (0030 has a
    # pre-check, so this should not happen in normal operation).
    for table in _NOT_NULL_TABLES:
        op.alter_column(table, "person_record_id", nullable=False)

    # Composite indexes for hot perimeter queries.
    op.execute(
        "CREATE INDEX source_documents_record_acquired_idx "
        "ON source_documents(person_record_id, acquired_at DESC)"
    )
    op.execute(
        "CREATE INDEX extracted_facts_record_date_idx "
        "ON extracted_facts(person_record_id, date_start DESC) "
        "WHERE date_start IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX extracted_facts_record_type_idx "
        "ON extracted_facts(person_record_id, fact_type)"
    )
    op.execute(
        "CREATE INDEX conversations_record_active_idx "
        "ON conversations(person_record_id, last_message_at DESC NULLS LAST)"
    )
    op.execute(
        "CREATE INDEX episodes_record_active_idx "
        "ON episodes(person_record_id, date_start DESC) "
        "WHERE merged_into_id IS NULL"
    )
    op.execute(
        "CREATE INDEX health_events_record_date_idx "
        "ON health_events(person_record_id, date_start DESC) "
        "WHERE date_start IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX evidence_anchors_record_idx "
        "ON evidence_anchors(person_record_id)"
    )
    op.execute(
        "CREATE INDEX audit_events_record_idx "
        "ON audit_events(person_record_id, created_at DESC) "
        "WHERE person_record_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS audit_events_record_idx")
    op.execute("DROP INDEX IF EXISTS evidence_anchors_record_idx")
    op.execute("DROP INDEX IF EXISTS health_events_record_date_idx")
    op.execute("DROP INDEX IF EXISTS episodes_record_active_idx")
    op.execute("DROP INDEX IF EXISTS conversations_record_active_idx")
    op.execute("DROP INDEX IF EXISTS extracted_facts_record_type_idx")
    op.execute("DROP INDEX IF EXISTS extracted_facts_record_date_idx")
    op.execute("DROP INDEX IF EXISTS source_documents_record_acquired_idx")
    for table in reversed(_NOT_NULL_TABLES):
        op.alter_column(table, "person_record_id", nullable=True)
