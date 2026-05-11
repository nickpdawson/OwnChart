"""User-confirmable fact significance — the ranking axis (2026-05-11).

Every patient-facing surface (Home, Timeline, Discover, Fact Context)
ranks by `significance`, NOT by fact count, recency, or source density.

Taxonomy (lower index = louder in the UI):

  1. major_event
  2. major_diagnosis
  3. major_procedure
  4. major_medication
  5. major_activity_lifestyle
  6. background
  7. source_only

`significance_source ∈ {user, llm, heuristic, default}` tracks who set
the value. User overrides always win.

This module owns:

  - `RANK`            — ordering for SQL/Python sort keys.
  - `compute(...)`    — conservative deterministic heuristic. Ingest +
                        backfill call this; the LLM may revise to a
                        more aggressive promotion later.
  - `is_hidden(...)`  — default-visibility check (source_only is hidden
                        by default everywhere).
"""

from __future__ import annotations

import re
from typing import Iterable

# Ordered, lower = more prominent.
#
# Tuned 2026-05-11 PM after live data check: surgeries (punctual life
# events) should outrank chronic problem-list diagnoses. Nick's real
# record had "Gastroesophageal reflux disease" sorting above the
# the example surgery; that's not what users mean by "what's
# important on my record." Procedure now sits at rank 2.
RANK: dict[str, int] = {
    "major_event": 1,
    "major_procedure": 2,
    "major_diagnosis": 3,
    "major_medication": 4,
    "major_activity_lifestyle": 5,
    "background": 6,
    "source_only": 7,
}

# What the UI shows by default. source_only is intentionally excluded —
# revealed only through an explicit "show source-only" toggle.
DEFAULT_VISIBLE: tuple[str, ...] = (
    "major_event",
    "major_diagnosis",
    "major_procedure",
    "major_medication",
    "major_activity_lifestyle",
    "background",
)

MAJOR: tuple[str, ...] = (
    "major_event",
    "major_procedure",
    "major_diagnosis",
    "major_medication",
    "major_activity_lifestyle",
)


# Condition labels that are really lifestyle / behavior markers
# (EHR problem-list entries that aren't true medical diagnoses).
# These default to background — the user can promote individual
# ones to major_diagnosis if a specific lifestyle factor matters
# to them.
_LIFESTYLE_CONDITION_LABELS = {
    "vegan diet", "vegetarian diet", "low salt diet", "low sodium diet",
    "gluten free diet", "ketogenic diet", "mediterranean diet",
    "moderate exercise", "regular exercise", "physical exercise",
    "sedentary lifestyle", "smoker", "former smoker", "non-smoker",
    "alcohol use", "occasional alcohol use",
    "preop examination", "preoperative examination", "preoperative assessment",
    "annual physical exam", "wellness exam",
}

# Recurring routine conditions that aren't typically a user-facing
# "major" diagnosis on their own. Cold sores recur and shouldn't
# outrank a surgery.
_ROUTINE_CONDITION_LABELS = {
    "oral herpes", "cold sore", "herpes simplex",
    "common cold", "upper respiratory infection", "uri",
    "seasonal allergic rhinitis", "allergic rhinitis",
    "acne vulgaris", "acne",
    "dandruff", "seborrheic dermatitis",
}


# FHIR-resource-ID-shaped labels are scaffolding artifacts, not events.
# Reused across other modules — kept here so significance is the single
# source of truth for "this label is unsalvageable scaffolding."
_FHIR_ID_LABEL_RE = re.compile(
    r"^(Encounter|MedicationRequest|MedicationDispense|MedicationStatement|"
    r"Procedure|Condition|Observation|DiagnosticReport|AllergyIntolerance|"
    r"Immunization|Resource) [A-Za-z0-9._\-]{12,}$"
)

# Names from fax cover sheets, scheduling clerks, records custodians.
# These should default to source_only — they describe an institution's
# workflow, not the patient's body. A `provider_relationship` fact
# carrying a real care-team relationship (PCP, specialist) gets caught
# by the "has body_site / non-trivial description" promotion below.
_PROVIDER_CONTACT_NOISE_KEYS = (
    "why_needs_review_code",
    "review_task_type",
)
_PROVIDER_CONTACT_REASONS = {
    "contact_metadata", "template_boilerplate", "fax_cover_metadata",
    "scheduling_clerk", "records_custodian",
}


# Bag of routine OTC / supplement / vitamin labels that, when seen
# without a prescription source, default to background. Match is
# case-insensitive substring.
_ROUTINE_MED_LABELS = {
    "creatine", "multivitamin", "vitamin d", "vitamin b", "fish oil",
    "omega-3", "magnesium", "zinc", "biotin", "melatonin",
    "ibuprofen", "acetaminophen", "tylenol", "advil", "aleve",
    "loratadine", "claritin", "zyrtec", "cetirizine",
    "famotidine", "pepcid", "tums", "calcium carbonate",
}


def _label_lower(*parts: str | None) -> str:
    return " ".join(p.lower() for p in parts if p)


def _looks_like_provider_contact_noise(fact_like: object) -> bool:
    why_code = getattr(fact_like, "why_needs_review_code", None)
    if why_code and why_code in _PROVIDER_CONTACT_REASONS:
        return True
    if getattr(fact_like, "source_context_only_eligible", False):
        return True
    return False


def _looks_routine_medication(label: str) -> bool:
    s = label.lower()
    return any(k in s for k in _ROUTINE_MED_LABELS)


def compute(
    fact_like: object,
    *,
    extraction_method: str | None = None,
    review_state: str | None = None,
    fact_type: str | None = None,
    label: str | None = None,
    description: str | None = None,
    confidence: int | None = None,
) -> str:
    """Conservative deterministic significance for one fact.

    Pass either an ORM `ExtractedFact` (each kwarg falls back to the
    attribute) or just the kwargs. Falls back to `background` for
    anything not promoted by a rule — promotion is opt-in.
    """
    # Resolve fields. Kwargs win when provided so the function is
    # usable from ingest before the row is persisted.
    fact_type = fact_type or getattr(fact_like, "fact_type", None) or ""
    label = label if label is not None else (getattr(fact_like, "label", "") or "")
    description = description if description is not None else (getattr(fact_like, "description", "") or "")
    extraction_method = extraction_method or getattr(fact_like, "extraction_method", None) or ""
    review_state = review_state or getattr(fact_like, "review_state", None) or ""
    confidence = confidence if confidence is not None else getattr(fact_like, "confidence", None)

    # ---- source_only ------------------------------------------------
    if review_state == "source_only":
        return "source_only"
    if _FHIR_ID_LABEL_RE.match(label or ""):
        return "source_only"
    if _looks_like_provider_contact_noise(fact_like):
        return "source_only"
    if fact_type == "provider_relationship":
        # A care-team relationship usually has a description (role,
        # specialty). A bare name on a fax cover sheet has neither
        # and goes straight to source_only.
        if not description.strip():
            return "source_only"
        # Keep description-bearing relationships as background unless
        # promoted later.
        return "background"

    # ---- background (default for routine extractions) ---------------
    # Wearable / native HK daily aggregates are metric points, not
    # life events. They're charted, not promoted.
    wearable_methods = {"health_auto_export", "native_healthkit"}
    if extraction_method in wearable_methods:
        # HK workouts (life_context_event extraction) are still
        # background by default; promotion to major_activity_lifestyle
        # happens via user override or LLM sensemaking.
        return "background"

    # Routine observations: vitals, weights, point measurements
    # without an episode anchor. Promotion to major requires explicit
    # signal (linked to a diagnosis, abnormal flag, etc.) — the
    # heuristic alone keeps them background.
    if fact_type == "observation":
        return "background"

    if fact_type == "symptom":
        return "background"

    # ---- major_procedure --------------------------------------------
    if fact_type == "procedure":
        # Clean label + not boilerplate ⇒ promoted. A FHIR procedure
        # with body_site or a non-trivial description further
        # confirms, but isn't required.
        return "major_procedure"

    # ---- major_diagnosis --------------------------------------------
    if fact_type == "condition":
        label_l = label.lower().strip()
        # Lifestyle / behavior markers from EHR problem lists — these
        # aren't true diagnoses ("Vegan diet" is not a medical issue).
        if any(k in label_l for k in _LIFESTYLE_CONDITION_LABELS):
            return "background"
        # Routine recurring conditions — important enough to keep
        # visible but should not outrank a real surgery on Home.
        if any(k in label_l for k in _ROUTINE_CONDITION_LABELS):
            return "background"
        return "major_diagnosis"

    # ---- major_medication / background medication ------------------
    if fact_type == "medication":
        if _looks_routine_medication(label):
            return "background"
        return "major_medication"

    # ---- major_event ------------------------------------------------
    if fact_type == "encounter":
        # An encounter linked to a procedure or admission is a life
        # event. A walk-in office visit without procedures isn't —
        # the heuristic can't always tell, so we lean conservative:
        # encounter ⇒ major_event when it has a non-trivial description
        # (room/admission/ER class), background otherwise.
        s = _label_lower(label, description)
        if any(k in s for k in (
            "emergency", "admission", "admitted", "inpatient", "surgery",
            "operating room", "post-op", "operative",
        )):
            return "major_event"
        return "background"

    if fact_type == "imaging_study":
        # Imaging is borderline; a study with a real description goes
        # to major_event, otherwise background.
        if description.strip():
            return "major_event"
        return "background"

    if fact_type == "lab_result":
        # Labs are mostly background unless the LLM tags them
        # (statistical major_event for an abnormal). Heuristic stays
        # conservative.
        return "background"

    # ---- major_activity_lifestyle -----------------------------------
    if fact_type == "life_context_event":
        # User-marked life events are care-meaningful by intent.
        return "major_activity_lifestyle"

    # ---- inferred relationships, findings, anything else ------------
    if fact_type in ("inferred_relationship", "finding"):
        return "background"

    return "background"


def sort_key(significance: str | None) -> int:
    """For Python sorts. Lower rank = sorts first (most prominent).

    Unknown / NULL gets the same rank as background so the UI degrades
    gracefully if a fact slips through without a value.
    """
    if significance is None:
        return RANK["background"]
    return RANK.get(significance, RANK["background"])


def is_default_visible(significance: str | None) -> bool:
    """source_only is hidden by default — every other value renders."""
    if significance is None:
        return True
    return significance != "source_only"


def major_set() -> tuple[str, ...]:
    """The "major_*" values — convenience for SQL filters."""
    return MAJOR


def visible_set(include_source_only: bool = False) -> tuple[str, ...]:
    """The bag of significance values UI surfaces should accept by default.

    Pass `include_source_only=True` for the explicit "show all" toggle.
    """
    if include_source_only:
        return DEFAULT_VISIBLE + ("source_only",)
    return DEFAULT_VISIBLE


def filter_visible(
    facts: Iterable[object],
    *,
    include_source_only: bool = False,
) -> list[object]:
    """In-memory filter — for routes that already loaded a fact list."""
    allowed = set(visible_set(include_source_only=include_source_only))
    return [
        f for f in facts
        if (getattr(f, "significance", None) or "background") in allowed
    ]
