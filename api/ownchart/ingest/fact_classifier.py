"""Classify whether an extracted fact is clinical signal or operational noise.

Real-world EHR FHIR exports (Epic in particular) emit one Observation per
nursing-template field, intake questionnaire item, and workflow checkbox:
"Vitals" (no value, no LOINC), "Pre-Procedure Verification", "Do you have
asthma?", "UVA ID band on and name matches all documentation", and dozens
of "STRABISMUS EXAM - METHOD - STRABISMUS TESTING METHOD 1"-shaped
breadcrumbs from exam templates.

These are *operational* records — they exist to satisfy nursing
documentation requirements and billing audit, not to chronicle the
patient's longitudinal health story. Asking the user to triage hundreds
of them in the review inbox violates the doctrine: "AI as research
partner... make institutional blind spots visible: fragmented records,
missing context, *billing-shaped documentation*".

This classifier identifies obvious operational/template noise from the
fact's label so we can land it in `review_state='deferred'` at ingest
time. The data is preserved (raw FHIR snapshot is untouched, the
ExtractedFact row exists), it's just kept out of the default review
queue and dossier view. Users who want to dig can query
`/api/facts?review_state=deferred`.

Heuristic categories (intentionally conservative — false negatives keep
real signal in the inbox; false positives hide a fact):

  1. Question-shaped labels (questionnaire/intake): ends with "?",
     starts with "Do you / Have you / Is your / ...".
  2. Workflow vocabulary (verification/consent/checklist phrases).
  3. Generic panel-header categories ("Vitals", "Respiratory", ...).
  4. Epic exam-template breadcrumbs (3+ " - " separators).
"""

from __future__ import annotations

import re

# Generic single-phrase panel/category headers that appear without values.
# Many of these repeat dozens of times in a single nursing-encounter export.
_GENERIC_PANEL_LABELS: frozenset[str] = frozenset(
    {
        "Vitals",
        "Vital Signs",
        "Respiratory",
        "Respiratory Assessment",
        "Pain",
        "Anthropometrics",
        "Adult Anthropometrics",
        "Motor Assessment",
        "Oxygen Therapy",
        "Symptomatic orthostasis",
        "OSA Total",
        "IV Contrast Screening",
        "Preop Discharge Plan",
        "Periop Additional Notes",
        "% IBW",
        "IBW/pounds",
        "IBW/kg",
        "Change in Weight (kg)",
        "Percent Weight Change Since Birth",
        "Pain Procedure Screening",
        "Procedure Schedule",
        "Procedure Time",
        "Arrival Time",
        "Comments:",
        "Care Plan Safety",
    }
)

# Label PREFIXES that mark workflow/checklist/intake noise.
_WORKFLOW_PREFIXES: tuple[str, ...] = (
    "Verification of ",
    "Pre-Procedure ",
    "Care Plan ",
    "Routine medications ",
    "Allergy band ",
    "Blood consent ",
    "Patient/family ",
    "Patient/Family ",
    "Pt/family ",
    "Pt/Family ",
    "Pt or surrogate ",
    "Pt's or Surrogate's ",
    "Pt receives ",
    "Identifies allergies",
    "Observes and documents",
    "Clinician has reviewed",
    "Safety precautions ",
    "If pre-existing IV",
    "Last Void time",
    "Contact Phone Number",
    "Ride Provided by",
    "Responsible Party",
    "UVA ID band",
    "Automatically Populated",
    "Questions for patient",
    "Non-expired Type and Screen",
    "OSA Diagnosis",
)

# Question-shaped intake (case-insensitive prefix match on first word).
_QUESTION_INTAKE_RE = re.compile(
    r"^\s*(do|have|has|are|is|did|was|can|should|will|were)\s+\w+",
    re.IGNORECASE,
)

# Epic exam-template breadcrumbs use 3+ " - " separators in the label,
# e.g. "FINDINGS - TESTS - EYES - STRABISMUS EXAM - METHOD - ...".
_EPIC_BREADCRUMB_RE = re.compile(r"(\s-\s[^-]+){3,}")


def is_template_noise(label: str | None, description: str | None = None) -> bool:
    """Return True if the label looks like operational/template noise.

    Conservative by design: only flags labels that match well-known
    workflow/intake/panel-header patterns. Real clinical findings
    (conditions, procedures, medications, lab values) pass through
    untouched.
    """
    if not label:
        return False
    s = label.strip()
    if not s:
        return False

    if s.endswith("?"):
        return True
    if s in _GENERIC_PANEL_LABELS:
        return True
    if _QUESTION_INTAKE_RE.match(s):
        return True
    for pref in _WORKFLOW_PREFIXES:
        if s.startswith(pref):
            return True
    if _EPIC_BREADCRUMB_RE.search(s):
        return True
    return False


def review_state_for_fhir(label: str | None, description: str | None = None) -> str:
    """Default review state for a FHIR-derived fact.

    FHIR resources are provider-attested in a clinical encounter — the EHR's
    clinician entered "a clinical term" in the EHR; we don't need the user
    to re-confirm that. We auto-classify:

      template noise        → 'deferred'  (kept out of inbox + dossier)
      clinically-meaningful → 'confirmed' (shown on dossier, not in inbox)

    Users retain full power to correct or reject anything from any view —
    the inbox just stops nagging by default. A future tighter heuristic
    can flag specific edge cases (e.g., abnormal lab outliers) for
    proactive review.
    """
    return "deferred" if is_template_noise(label, description) else "confirmed"


def review_state_for_vision(
    label: str | None,
    confidence_label: str | None,
    description: str | None = None,
) -> str:
    """Default review state for a Claude-Vision-extracted fact.

    Vision is doing the same job a clinician does when transcribing a
    fax: read the page, extract the facts. When Claude self-reports
    'high' confidence on something it could clearly read off the page
    ("Bruce T. Carter, M.D.", "Left lateral rectus recession 5 mm",
    "a clinical term (10 prism diopter)"), there's no value in making the
    user click "Confirm" on each one. The doctrine is "research
    partner, not gatekeeper".

      template noise        → 'deferred'  (hidden)
      confidence high|medium → 'confirmed' (shown, not in inbox)
      confidence low|possible → 'needs_review' (genuine ambiguity)
      confidence missing     → 'needs_review' (be cautious)

    Confidence labels come from the Claude prompt's tool schema (see
    extract_fax_vision.v1.yaml).
    """
    if is_template_noise(label, description):
        return "deferred"
    cl = (confidence_label or "").strip().lower()
    if cl in {"high", "medium"}:
        return "confirmed"
    return "needs_review"
