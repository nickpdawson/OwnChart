"""Per-record uniqueness for extracted_facts.client_sample_key.

Revision ID: 0048_client_sample_key_per_record
Revises: 0047_date_provenance_documentation_date
Create Date: 2026-07-11

CRIT-1 (live cross-record data loss). Migration 0015 created a
GLOBAL partial unique index on `client_sample_key`
(`ix_extracted_facts_client_sample_key`, WHERE client_sample_key IS
NOT NULL). At the time this was fine — V1 was single-tenant, one
person_record. It is now wrong: HealthKit aggregate keys are
`agg-<identifier>-<YYYY-MM-DD>` and Auto-Export medication keys are
`ae-med-<sha>` — neither carries a user/record component. Two paired
family devices (Mom's iPhone + Dad's iPhone) that both post
`agg-HKQuantityTypeIdentifierStepCount-2026-07-11` collide on the
GLOBAL index; the second record's sample is silently swallowed by
`ON CONFLICT DO NOTHING` and counted as "deduplicated". Cross-record
data loss.

Fix: make uniqueness per-record. Drop the global partial unique index
and replace it with a COMPOSITE partial unique index on
`(person_record_id, client_sample_key) WHERE client_sample_key IS NOT
NULL`. Same real-world sample from the same record still dedups;
identical keys on DIFFERENT records now coexist.

NULL handling:
  - `person_record_id` is NOT NULL (enforced by migration 0031 after
    the 0030 backfill), so the leading column of the composite is
    always determinate — there is no "NULLs are distinct" surprise on
    that side.
  - `client_sample_key` NULL rows (CCDA / photos / vision / manual
    uploads) remain EXCLUDED from the index entirely by the partial
    `WHERE client_sample_key IS NOT NULL` predicate, exactly as before.
    Those paths never relied on the index and are unaffected.

Every ON CONFLICT / pre-filter insert path that targeted the old
single-column index is updated in the same change to target
`(person_record_id, client_sample_key)`:
  - routes/healthkit_sync.py  (pg_insert .on_conflict_do_nothing)
  - workers/auto_export_job.py (cross-push existing-key pre-filter)
  - routes/sources.py         (legacy Auto-Export UI upload)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_client_sample_key_per_record"
down_revision = "0047_date_provenance_documentation_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_extracted_facts_client_sample_key",
        table_name="extracted_facts",
    )
    op.create_index(
        "ix_extracted_facts_record_client_sample_key",
        "extracted_facts",
        ["person_record_id", "client_sample_key"],
        unique=True,
        postgresql_where=sa.text("client_sample_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extracted_facts_record_client_sample_key",
        table_name="extracted_facts",
    )
    op.create_index(
        "ix_extracted_facts_client_sample_key",
        "extracted_facts",
        ["client_sample_key"],
        unique=True,
        postgresql_where=sa.text("client_sample_key IS NOT NULL"),
    )
