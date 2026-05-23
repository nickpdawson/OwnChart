"""ExtractedFact date_provenance + historical_status — Beta 1 Section C Phase 1.

Revision ID: 0044_extracted_fact_date_provenance
Revises: 0043_invitations
Create Date: 2026-05-23

Section C, Phase 1 (canonical-vs-echo event dates).

Adds two nullable columns to `extracted_facts`:

  - `date_provenance` — how confident we are in `date_start`:
      'explicit'             FHIR onsetDateTime / performedDateTime /
                             effectiveDateTime / CCDA effectiveTime /
                             vision-extracted date_start /
                             auto-export payload date.
      'encounter_proximate'  Resource lacked its own occurrence date;
                             the value was inherited from a linked
                             Encounter's period. Display surfaces add
                             "from this visit" qualifier.
      'issued_approximate'   Observation/DiagnosticReport with no
                             effective[x] — `issued` is the report
                             timestamp, not the clinical event time.
      'user_canonical'       Set when a UserAssertion overrides the
                             extracted date. The UserAssertion row
                             remains the source of truth; this is the
                             marker that surfaces should treat the
                             value as user-confirmed.
      NULL                   No date at all (e.g. a Condition with only
                             recordedDate after the Section C
                             refactor dropped recordedDate from the
                             Condition event-date priority list). UI
                             shows "Undated history" group; Chat says
                             "mentioned in a {YYYY} record."

  - `historical_status` — for Conditions only, mirror of the FHIR
    `clinicalStatus.coding.code`:
      'resolved' | 'inactive' | 'remission' | NULL.
    Display surfaces add a small low-contrast pill; facts remain
    retrievable (NOT marked source_only) — they're real history.

Per Section C audit:
  - The schema is otherwise already correct (date_start vs acquired_at
    were never collapsed). These two columns add the missing
    classification layer rather than restructuring the date model.
  - Backfill assumes existing non-NULL date_start rows are 'explicit'.
    This may misclassify UVA-bug rows that came in via recordedDate
    pre-refactor; PM-approved forward-only posture (users correct via
    UserAssertion or via the deferred FU-FHIR-REINGEST-WITH-DATE-PROVENANCE).

Composite index added on (person_record_id, date_provenance, date_start)
to keep Home / Discover / Timeline queries filtering by provenance
cheap — these surfaces query `WHERE person_record_id = ? AND
date_provenance = 'explicit' ORDER BY date_start DESC` on every load.
Without the index, that's a per-record seq scan.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_extracted_fact_date_provenance"
down_revision = "0043_invitations"
branch_labels = None
depends_on = None


_PROVENANCES = ("explicit", "encounter_proximate", "issued_approximate", "user_canonical")
_HISTORICAL_STATUSES = ("resolved", "inactive", "remission")


def upgrade() -> None:
    op.add_column(
        "extracted_facts",
        sa.Column("date_provenance", sa.String(32), nullable=True),
    )
    op.add_column(
        "extracted_facts",
        sa.Column("historical_status", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "extracted_facts_date_provenance_chk",
        "extracted_facts",
        "date_provenance IS NULL OR date_provenance IN ("
        "'explicit','encounter_proximate','issued_approximate','user_canonical')",
    )
    op.create_check_constraint(
        "extracted_facts_historical_status_chk",
        "extracted_facts",
        "historical_status IS NULL OR historical_status IN ("
        "'resolved','inactive','remission')",
    )

    # Forward-only backfill: pre-existing rows with a non-NULL date
    # are presumed 'explicit'. PM-approved: do not retroactively
    # classify the UVA-bug rows; the deferred reingest FU handles
    # them when it lands.
    op.execute(
        "UPDATE extracted_facts SET date_provenance = 'explicit' "
        "WHERE date_start IS NOT NULL"
    )

    # Composite index for the per-record provenance-filtered ORDER BY
    # date_start queries Home / Discover / Timeline issue on every load.
    op.create_index(
        "ix_extracted_facts_record_provenance_date",
        "extracted_facts",
        ["person_record_id", "date_provenance", "date_start"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extracted_facts_record_provenance_date",
        table_name="extracted_facts",
    )
    op.drop_constraint(
        "extracted_facts_historical_status_chk",
        "extracted_facts",
        type_="check",
    )
    op.drop_constraint(
        "extracted_facts_date_provenance_chk",
        "extracted_facts",
        type_="check",
    )
    op.drop_column("extracted_facts", "historical_status")
    op.drop_column("extracted_facts", "date_provenance")
