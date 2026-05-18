"""ExtractedFact raw_metadata JSONB — HealthKit workout payloads (M02 Slice 2).

Revision ID: 0035_extracted_fact_raw_metadata
Revises: 0034_oauth_flow_record_binding
Create Date: 2026-05-18

Beta 1 Milestone 02, Slice 2 — HealthKit workout runtime wiring.

Adds a nullable `raw_metadata` JSONB column to `extracted_facts`.
The column carries sample-level numeric/structured data that does
not belong in `coded_concepts`. The current callsite is the
HealthKit workout path (BE-3 contract, commit `077a10a`) which
splits its storage shape into:

  coded_concepts  →  small identifying fields suitable for query
                     (workout_activity_type, healthkit_identifier,
                      source_bundle_id, raw activity enum)
  raw_metadata    →  the full numeric payload + nested objects
                     (duration_s, distance_m, energy_kcal,
                      source: {name, bundle_id, version},
                      device: {name, model, manufacturer},
                      sync_mode, sample_metadata)

Why a separate column rather than packing into coded_concepts:
  - coded_concepts is keyed for retrieval (workout_activity_type,
    source_bundle_id); mixing a `duration_s: 2160.5` row into the
    same bag breaks the "concept code" mental model.
  - Future ingest paths (HealthKit body/sleep, Auto Export numeric
    payloads, EventKit timing) will reuse this column for the same
    reason. Slice 2 only wires the workout path; the column is
    intentionally permissive.

Why NULLABLE:
  - Backward-compat: every pre-Slice-2 fact has no payload to put
    here. Default NULL is the truthful state.
  - No backfill needed. coded_concepts stays the source of truth
    for retrieval / Ask; raw_metadata is observational provenance.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic revision identifiers.
revision = "0035_extracted_fact_raw_metadata"
down_revision = "0034_oauth_flow_record_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extracted_facts",
        sa.Column(
            "raw_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("extracted_facts", "raw_metadata")
