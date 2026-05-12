"""Dedupe native_healthkit source_documents + add unique constraint.

Revision ID: 0024_native_hk_src_dedupe
Revises: 0023_conversation_fts
Create Date: 2026-05-12

The HealthKit sync route used to check-then-insert source_documents
without any unique constraint, so parallel iOS uploads for the same
(user, day) silently created duplicate rows. After commit fixing the
race, the route's lookup hit MultipleResultsFound on every retry
because the old duplicates were still there.

This migration:
  1. Repoints every evidence_anchor pointing at a duplicate to the
     canonical row (earliest created_at, ties broken by id text order)
     so downstream facts stay anchored.
  2. Deletes the now-orphaned duplicate rows.
  3. Adds a partial unique constraint on (owner_user_id, source_label)
     where source_type='native_healthkit' so the race can't
     re-introduce duplicates.

Postgres has no MIN(uuid) aggregate; we pick the canonical row with
DISTINCT ON + ORDER BY (created_at, id::text) instead.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0024_native_hk_src_dedupe"
down_revision: Union[str, None] = "0023_conversation_fts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Build mapping: every duplicate row → its canonical row id.
    #    Canonical = oldest by created_at within each (owner, label)
    #    group; ties broken by id text order for determinism.
    op.execute("""
        WITH canonical AS (
            SELECT DISTINCT ON (owner_user_id, source_label)
                owner_user_id,
                source_label,
                id AS canonical_id
            FROM source_documents
            WHERE source_type = 'native_healthkit'
            ORDER BY owner_user_id, source_label, created_at ASC, id::text ASC
        ),
        dupes AS (
            SELECT sd.id AS dupe_id, c.canonical_id
            FROM source_documents sd
            JOIN canonical c
              ON c.owner_user_id = sd.owner_user_id
             AND c.source_label = sd.source_label
            WHERE sd.source_type = 'native_healthkit'
              AND sd.id <> c.canonical_id
        )
        UPDATE evidence_anchors ea
        SET source_document_id = dupes.canonical_id
        FROM dupes
        WHERE ea.source_document_id = dupes.dupe_id;
    """)

    # 2. Delete duplicate source_documents (keep the canonical row).
    op.execute("""
        WITH canonical AS (
            SELECT DISTINCT ON (owner_user_id, source_label)
                owner_user_id,
                source_label,
                id AS canonical_id
            FROM source_documents
            WHERE source_type = 'native_healthkit'
            ORDER BY owner_user_id, source_label, created_at ASC, id::text ASC
        )
        DELETE FROM source_documents sd
        USING canonical c
        WHERE sd.source_type = 'native_healthkit'
          AND sd.owner_user_id = c.owner_user_id
          AND sd.source_label = c.source_label
          AND sd.id <> c.canonical_id;
    """)

    # 3. Partial unique index — prevents the race from re-introducing dupes.
    op.execute("""
        CREATE UNIQUE INDEX uq_source_documents_native_hk_day
        ON source_documents (owner_user_id, source_label)
        WHERE source_type = 'native_healthkit';
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_source_documents_native_hk_day;")
