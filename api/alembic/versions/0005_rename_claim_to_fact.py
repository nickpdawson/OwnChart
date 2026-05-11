"""Rename `claim` → `fact` (billing terminology mismatch)

Revision ID: 0005_rename_claim_to_fact
Revises: 0004_provider_connectors
Create Date: 2026-05-09

In healthcare data, "claim" unambiguously means "insurance billing claim"
(FHIR `Claim` resource). OwnChart's ExtractedClaim is a clinical
assertion/fact, NOT a billing artifact. The doctrine in docs/00 + docs/04
explicitly rejects billing-shaped institutional framing, so the name had
to change.

  extracted_claims          → extracted_facts
  extracted_claims.claim_type → extracted_facts.fact_type
  user_assertions.related_claim_id → user_assertions.related_fact_id
  health_events.source_claim_ids → health_events.source_fact_ids

Indexes follow the table/column rename. The data is unchanged; this is
purely a rename pass. UserAssertion / EvidenceAnchor / SourceDocument /
ModelRun / Topic / HealthEvent table names stay as-is — those names
aren't billing-overloaded.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0005_rename_claim_to_fact"
down_revision: Union[str, None] = "0004_provider_connectors"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("extracted_claims", "extracted_facts")
    op.alter_column("extracted_facts", "claim_type", new_column_name="fact_type")

    # Indexes were created with names like ix_extracted_claims_*. Rename to
    # match the new table name.
    op.execute("ALTER INDEX IF EXISTS ix_extracted_claims_claim_type RENAME TO ix_extracted_facts_fact_type")
    op.execute("ALTER INDEX IF EXISTS ix_extracted_claims_review_state RENAME TO ix_extracted_facts_review_state")
    # Trigram indexes from migration 0003.
    op.execute("ALTER INDEX IF EXISTS ix_extracted_claims_label_trgm RENAME TO ix_extracted_facts_label_trgm")
    op.execute("ALTER INDEX IF EXISTS ix_extracted_claims_description_trgm RENAME TO ix_extracted_facts_description_trgm")

    # FK column on user_assertions
    op.alter_column("user_assertions", "related_claim_id", new_column_name="related_fact_id")
    op.execute("ALTER INDEX IF EXISTS ix_user_assertions_related_claim_id RENAME TO ix_user_assertions_related_fact_id")

    # UUID array on health_events
    op.alter_column("health_events", "source_claim_ids", new_column_name="source_fact_ids")


def downgrade() -> None:
    op.alter_column("health_events", "source_fact_ids", new_column_name="source_claim_ids")
    op.execute("ALTER INDEX IF EXISTS ix_user_assertions_related_fact_id RENAME TO ix_user_assertions_related_claim_id")
    op.alter_column("user_assertions", "related_fact_id", new_column_name="related_claim_id")
    op.execute("ALTER INDEX IF EXISTS ix_extracted_facts_description_trgm RENAME TO ix_extracted_claims_description_trgm")
    op.execute("ALTER INDEX IF EXISTS ix_extracted_facts_label_trgm RENAME TO ix_extracted_claims_label_trgm")
    op.execute("ALTER INDEX IF EXISTS ix_extracted_facts_review_state RENAME TO ix_extracted_claims_review_state")
    op.execute("ALTER INDEX IF EXISTS ix_extracted_facts_fact_type RENAME TO ix_extracted_claims_claim_type")
    op.alter_column("extracted_facts", "fact_type", new_column_name="claim_type")
    op.rename_table("extracted_facts", "extracted_claims")
