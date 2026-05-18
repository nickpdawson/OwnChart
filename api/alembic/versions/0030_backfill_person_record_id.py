"""Multi-person foundations — backfill person_record_id on every record-bearing row.

Revision ID: 0030_backfill_person_record_id
Revises: 0029_person_record_id_columns
Create Date: 2026-05-17

Beta 1 Milestone 02, Slice 1, Phase A step 4.

For each table that got a nullable `person_record_id` in 0029,
populate it from the right source. After this runs, no row should
have a NULL `person_record_id`; 0031 then flips NOT NULL + adds
the composite indexes.

Backfill logic per table:

  Direct-owned (currently scope by user_id or owner_user_id; we
  point at the user's self-record from 0028):
    - source_documents      → from owner_user_id
    - conversations         → from user_id
    - conversation_messages → from user_id
    - episodes              → from user_id
    - sensemaking_jobs      → from user_id
    - sensemaking_candidates → from user_id
    - extraction_jobs       → from user_id
    - brief_messages        → from user_id
    - user_assertions       → from user_id
    - audit_events          → from user_id (nullable today; rows
                              with NULL user_id stay NULL → caught
                              + handled in 0031, see below)

  Denormalized from parent:
    - evidence_anchors      → join source_documents
    - extracted_facts       → join evidence_anchors → source_documents
                              via the first id in evidence_anchor_ids[]
    - health_events         → join extracted_facts via the first id
                              in source_fact_ids[]

This step has zero schema change; pure DML. Reversible by
`UPDATE ... SET person_record_id = NULL` per table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_backfill_person_record_id"
down_revision: Union[str, None] = "0029_person_record_id_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Direct-owned tables that carry user_id -------------------------
    _direct = [
        ("conversations", "user_id"),
        ("conversation_messages", "user_id"),
        ("episodes", "user_id"),
        ("sensemaking_jobs", "user_id"),
        ("sensemaking_candidates", "user_id"),
        ("extraction_jobs", "user_id"),
        ("brief_messages", "user_id"),
        ("user_assertions", "user_id"),
        ("source_documents", "owner_user_id"),
    ]
    for table, user_col in _direct:
        op.execute(
            f"""
            UPDATE {table} t
            SET person_record_id = pr.id
            FROM person_records pr
            WHERE pr.created_by_user_id = t.{user_col}
              AND pr.is_self = true
              AND t.person_record_id IS NULL
            """
        )

    # --- audit_events: user_id is nullable today --------------------------
    # Rows with non-NULL user_id get backfilled; rows with NULL user_id
    # (system audit entries with no actor) stay NULL — they get a
    # NOT NULL exception worked around in 0031 by making the column
    # remain nullable on audit_events specifically. See 0031 for the
    # explicit rationale.
    op.execute(
        """
        UPDATE audit_events ae
        SET person_record_id = pr.id
        FROM person_records pr
        WHERE pr.created_by_user_id = ae.user_id
          AND pr.is_self = true
          AND ae.person_record_id IS NULL
        """
    )

    # --- evidence_anchors: join source_documents ------------------------
    op.execute(
        """
        UPDATE evidence_anchors ea
        SET person_record_id = sd.person_record_id
        FROM source_documents sd
        WHERE ea.source_document_id = sd.id
          AND ea.person_record_id IS NULL
        """
    )

    # --- extracted_facts: join via first evidence_anchor_ids element ----
    # Postgres array indexing is 1-based. Using LIMIT 1 in a lateral
    # subquery so any-anchor matches; in practice every fact has at
    # least one anchor.
    op.execute(
        """
        UPDATE extracted_facts ef
        SET person_record_id = ea.person_record_id
        FROM evidence_anchors ea
        WHERE ea.id = (
            SELECT unnest(ef.evidence_anchor_ids) LIMIT 1
        )
        AND ef.person_record_id IS NULL
        """
    )

    # --- health_events: join via first source_fact_ids element ----------
    op.execute(
        """
        UPDATE health_events he
        SET person_record_id = ef.person_record_id
        FROM extracted_facts ef
        WHERE ef.id = (
            SELECT unnest(he.source_fact_ids) LIMIT 1
        )
        AND he.person_record_id IS NULL
        """
    )

    # --- Sanity check: every record-bearing row except certain
    # nullable-user_id audit events must now have a person_record_id.
    # If anything is unbackfilled, the migration raises and 0031 won't
    # run. Operator must investigate (probably an orphan row).
    #
    # Offline (`alembic upgrade --sql`) mode short-circuits this check —
    # there's no live connection to query and the SQL we'd emit (a bare
    # SELECT COUNT) doesn't tell the operator anything useful in a
    # script-only render. Operators applying online get the safety
    # check; operators reviewing the rendered SQL see the upgrade
    # statements without the runtime guard.
    from alembic import context as _ctx
    if _ctx.is_offline_mode():
        return
    _required_filled = [
        "source_documents",
        "conversations",
        "conversation_messages",
        "episodes",
        "sensemaking_jobs",
        "sensemaking_candidates",
        "extraction_jobs",
        "brief_messages",
        "user_assertions",
        "evidence_anchors",
        "extracted_facts",
        "health_events",
    ]
    for table in _required_filled:
        result = op.get_bind().execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE person_record_id IS NULL")
        ).scalar()
        if result and result > 0:
            raise RuntimeError(
                f"0030 backfill: {table} has {result} rows with "
                "NULL person_record_id; investigate before re-running. "
                "Most likely cause: an orphan row whose parent chain "
                "lost its user binding."
            )


def downgrade() -> None:
    # Pure DML reversal — set everything back to NULL. Note: doesn't
    # restore any pre-existing NULL state; if some rows had NULL
    # values prior to this migration (impossible for the direct
    # tables, possible for audit_events), they would re-acquire a
    # spurious NULL. Acceptable for downgrade.
    _all = [
        "source_documents", "conversations", "conversation_messages",
        "episodes", "sensemaking_jobs", "sensemaking_candidates",
        "extraction_jobs", "brief_messages",
        "user_assertions", "audit_events", "evidence_anchors",
        "extracted_facts", "health_events",
    ]
    for table in _all:
        op.execute(f"UPDATE {table} SET person_record_id = NULL")
