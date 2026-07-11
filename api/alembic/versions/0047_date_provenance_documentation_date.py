"""ExtractedFact date_provenance — add 'documentation_date' — Echo Phase 0.

Revision ID: 0047_date_provenance_documentation_date
Revises: 0046_pictal_json_export
Create Date: 2026-07-11

Echo Phase 0 (unify temporal provenance; stop the note-date leak).

Adds one value to the `date_provenance` CHECK constraint on
`extracted_facts`:

  - `documentation_date` — the `date_start` on this fact is when the
    fact was *documented* in a record, NOT a confirmed occurrence date
    for the underlying event. This is the LLM clinical-note "note-date"
    case: a same-day clinical finding whose only available date is the
    note's creation timestamp. It is the direct analog, for the
    LLM/CCDA/vision paths, of the FHIR `issued_approximate` /
    `encounter_proximate` softening added in migration 0044 — a date we
    carry for ordering but must not present as a confirmed event date.

Why a distinct value rather than reusing an existing one:
  - It is NOT `explicit` — the source never asserted this as the event
    date, so Home / Discover (which anchor on `date_provenance='explicit'`)
    must continue to exclude it, exactly like an echo.
  - It is NOT NULL — the fact does carry a usable `date_start` (the
    documentation date) for coarse ordering; NULL is reserved for facts
    with no date at all ("undated history").
  - It is NOT `issued_approximate` — that value is specific to the FHIR
    Observation/DiagnosticReport `issued` fallback and its display copy
    ("the underlying observation may pre-date it"). The documentation-date
    case wants its own copy ("the date reflects when it was documented").

Forward-only unification note: this is the point at which the
`date_provenance` COLUMN becomes the single source of truth for the
LLM path. The clinical-note extractor previously recorded date
semantics only in `coded_concepts.date_origin` (a parallel vocabulary
that no live retrieval/display surface reads). Echo Phase 0 populates
the column from the LLM/CCDA/vision paths and drops the `date_origin`
dual-write. No reader migration was needed — every live reader (Home,
Discover, FactContext, the Ask evidence block, Pictal export) already
consumes the column.

  - **NO blanket backfill.** Same rationale as 0044: legacy rows keep
    whatever `date_provenance` they have (almost always NULL for the
    LLM/CCDA/vision paths, which never set it before Phase 0). Marking
    old note-dated facts `documentation_date` retroactively is a
    reingest concern, not a migration concern. Home/Discover already
    exclude NULL-provenance rows, so the safety posture is unchanged.
    PM-approved as part of Echo Phase 0.
"""

from __future__ import annotations

from alembic import op

revision = "0047_date_provenance_documentation_date"
down_revision = "0046_pictal_json_export"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "extracted_facts_date_provenance_chk",
        "extracted_facts",
        type_="check",
    )
    op.create_check_constraint(
        "extracted_facts_date_provenance_chk",
        "extracted_facts",
        "date_provenance IS NULL OR date_provenance IN ("
        "'explicit','encounter_proximate','issued_approximate',"
        "'user_canonical','documentation_date')",
    )
    # NO BLANKET BACKFILL — see module docstring. Legacy LLM/CCDA/vision
    # rows stay date_provenance=NULL; they are already excluded from the
    # explicit-only Home/Discover surfaces.


def downgrade() -> None:
    op.drop_constraint(
        "extracted_facts_date_provenance_chk",
        "extracted_facts",
        type_="check",
    )
    op.create_check_constraint(
        "extracted_facts_date_provenance_chk",
        "extracted_facts",
        "date_provenance IS NULL OR date_provenance IN ("
        "'explicit','encounter_proximate','issued_approximate','user_canonical')",
    )
