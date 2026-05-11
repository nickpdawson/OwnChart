"""Auto-classify provider-attested facts; defer template noise.

Revision ID: 0006_classify_template_noise
Revises: 0005_rename_claim_to_fact
Create Date: 2026-05-09

Existing FHIR + CCDA exports landed every fact in `review_state='needs_review'`,
including provider-attested clinical entries (Esotropia, Optic neuropathy,
medications) AND the hundreds of nursing-template observations that EHRs
emit ("Vitals", "Verification of Identity", "Do you have asthma?",
"STRABISMUS EXAM - METHOD - STRABISMUS TESTING METHOD 1", ...).

That made the review inbox unusable — 200+ items of mostly-operational
noise that violates the "AI as research partner" doctrine. The user
shouldn't have to triage billing-shaped documentation that the EHR's
own clinician already attested to.

This migration backfills the new defaults for existing data:

  • Operational/template noise (matched by SQL regex on label) → 'deferred'
  • Everything else still in 'needs_review' AND extraction_method in
    {fhir_resource, ccda_xpath} → 'confirmed' (provider-attested)
  • Claude vision extractions stay in 'needs_review' (low trust)

The patterns mirror api/ownchart/ingest/fact_classifier.py — kept in
SQL here so the migration is self-contained. Future ingests apply the
classifier in Python at write time.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006_classify_template_noise"
down_revision: Union[str, None] = "0005_rename_claim_to_fact"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Patterns that mark template/workflow/intake noise. Postgres regex is
# `~*` for case-insensitive. Keep in sync with fact_classifier.py.
_TEMPLATE_NOISE_SQL = r"""
UPDATE extracted_facts
SET review_state = 'deferred'
WHERE review_state = 'needs_review'
  AND (
    -- Question-shaped intake / questionnaire
    label LIKE '%?'
    OR label ~* '^(do|have|has|are|is|did|was|can|should|will|were)\s+\w+'

    -- Generic panel / category headers
    OR label IN (
      'Vitals', 'Vital Signs', 'Respiratory', 'Respiratory Assessment',
      'Pain', 'Anthropometrics', 'Adult Anthropometrics', 'Motor Assessment',
      'Oxygen Therapy', 'Symptomatic orthostasis', 'OSA Total',
      'IV Contrast Screening', 'Preop Discharge Plan', 'Periop Additional Notes',
      '% IBW', 'IBW/pounds', 'IBW/kg', 'Change in Weight (kg)',
      'Percent Weight Change Since Birth', 'Pain Procedure Screening',
      'Procedure Schedule', 'Procedure Time', 'Arrival Time',
      'Comments:', 'Care Plan Safety'
    )

    -- Workflow / checklist / consent / verification prefixes
    OR label LIKE 'Verification of %'
    OR label LIKE 'Pre-Procedure %'
    OR label LIKE 'Care Plan %'
    OR label LIKE 'Routine medications %'
    OR label LIKE 'Allergy band %'
    OR label LIKE 'Blood consent %'
    OR label LIKE 'Patient/family %'
    OR label LIKE 'Patient/Family %'
    OR label LIKE 'Pt/family %'
    OR label LIKE 'Pt/Family %'
    OR label LIKE 'Pt or surrogate %'
    OR label LIKE 'Pt''s or Surrogate''s %'
    OR label LIKE 'Pt receives %'
    OR label LIKE 'Identifies allergies%'
    OR label LIKE 'Observes and documents%'
    OR label LIKE 'Clinician has reviewed%'
    OR label LIKE 'Safety precautions %'
    OR label LIKE 'If pre-existing IV%'
    OR label LIKE 'Last Void time%'
    OR label LIKE 'Contact Phone Number%'
    OR label LIKE 'Ride Provided by%'
    OR label LIKE 'Responsible Party%'
    OR label LIKE 'UVA ID band%'
    OR label LIKE 'Automatically Populated%'
    OR label LIKE 'Questions for patient%'
    OR label LIKE 'Non-expired Type and Screen%'
    OR label LIKE 'OSA Diagnosis%'

    -- Epic exam-template breadcrumbs (3+ " - " separators)
    OR label ~ '(\s-\s[^-]+){3,}'
  )
"""

_AUTO_CONFIRM_PROVIDER_ATTESTED_SQL = """
UPDATE extracted_facts
SET review_state = 'confirmed'
WHERE review_state = 'needs_review'
  AND extraction_method IN ('fhir_resource', 'ccda_xpath')
"""


def upgrade() -> None:
    # Order matters: tag noise as deferred FIRST, then auto-confirm
    # whatever's still needs_review. Otherwise we'd auto-confirm noise.
    op.execute(_TEMPLATE_NOISE_SQL)
    op.execute(_AUTO_CONFIRM_PROVIDER_ATTESTED_SQL)


def downgrade() -> None:
    # Intentional no-op: we cannot tell apart facts auto-confirmed by
    # this migration vs. confirmed by user_assertions, so a blind revert
    # would destroy real user-canonical state. Roll forward instead by
    # writing a fresher migration if the heuristic needs to change.
    pass
