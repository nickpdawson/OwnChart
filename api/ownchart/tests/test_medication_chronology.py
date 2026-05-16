"""Medication chronology classification tests.

Doctrine: a medication-chronology question ("when did I start
taking X?") must distinguish five evidence classes:

  1. earliest tracker log              (Auto Export / HealthKit)
  2. first adherence-marked log         ("Taken" / "Skipped")
  3. first EHR med-list mention         (encounter summary)
  4. first MedicationRequest            (actual prescription)
  5. first prescriber                   (MedicationRequest.requester)

The Ask LLM prompt enforces presentation rules; these tests pin the
deterministic input-shape helpers — the source-quality classifier
must put each evidence class on the right authority tier so the
prompt's "lead with the strongest source" rule produces the right
answer.

No DB, no LLM. Pure-function tests against the same classifier
shipped to retrieval + the Ask prompt.
"""

from ownchart.llm.conversations import _source_quality_tier, _TIER_RANK


# Each test pair: (label, filename, source_type) → expected tier.
# Mirrors the medication-chronology cases the Ask LLM is told to
# distinguish.


def test_tracker_log_is_primary_event_device():
    """Auto Export / HealthKit medication logs are device-emitted
    (the user tapped 'Taken' on their phone). They're tier-1
    primary_event by source_type — NOT 'started taking' evidence,
    but high-authority for adherence chronology."""
    assert _source_quality_tier(None, None, "health_auto_export") == "primary_event"
    assert _source_quality_tier(None, None, "native_healthkit") == "primary_event"


def test_encounter_summary_med_list_is_contemporaneous():
    """A medication appearing on an encounter summary is OFTEN a
    reconciliation (med carried forward), NOT an originating Rx.
    Lives at tier-3 contemporaneous_support — explicitly weaker
    than a MedicationRequest source. The Ask prompt is required to
    phrase these as 'first appeared on a med list (likely a
    reconciliation, not the original prescription).'"""
    tier = _source_quality_tier(
        "Bozeman Health", "Encounter Summary", "clinical_note",
    )
    assert tier == "contemporaneous_support"


def test_prescription_label_outranks_self_reported_history():
    """The keyword 'prescription' currently lands at contemporaneous
    (tier-3) — co-located with med-list reconciliations because the
    classifier can't tell a pharmacy label apart from a 'prescriptions'
    EHR section by filename alone. What MUST be true for chronology
    is that a prescription source outranks a pre-op PSH list — Nick's
    ACL bug rule applied to medications. If a future doctrine refinement
    promotes pharmacy-label photos to primary_event, this test stays
    correct (primary_event < self_reported_history in _TIER_RANK)."""
    rx = _source_quality_tier(
        "CVS Pharmacy", "Prescription Label", "photo",
    )
    psh = _source_quality_tier(
        "Stanford Health Care", "Past Surgical History",
        "clinical_note",
    )
    assert _TIER_RANK[rx] < _TIER_RANK[psh]


def test_patient_summary_is_ehr_summary_when_not_specialty():
    """A generic patient summary is a problem/med list copy —
    tier-4 ehr_summary. Below an actual encounter note."""
    tier = _source_quality_tier(
        "Bozeman Health", "Patient Summary", "ccda_xml",
    )
    assert tier == "ehr_summary"


def test_pre_op_psh_lower_than_encounter_summary():
    """Pre-op surgical history is what the patient TOLD the
    surgeon during intake — not a record of the event. Must rank
    BELOW even a contemporaneous encounter summary."""
    psh = _source_quality_tier(
        "Stanford Health Care", "Anesthesia Preprocedure Evaluation",
        "clinical_note",
    )
    encounter = _source_quality_tier(
        "Bozeman Health", "Encounter Summary", "clinical_note",
    )
    assert _TIER_RANK[encounter] < _TIER_RANK[psh]


def test_tracker_outranks_psh_for_chronology():
    """Counter-intuitive but correct: a tracker log (tier-1
    primary_event) outranks a patient-reported PSH list (tier-5).
    Tracker dates ARE what they say they are — the patient
    actually tapped 'Taken' on that date. PSH is a recollection
    given during intake. For chronology questions the tracker is
    higher authority."""
    tracker = _source_quality_tier(None, None, "native_healthkit")
    psh = _source_quality_tier(
        "Stanford Health Care", "Past Surgical History",
        "clinical_note",
    )
    assert _TIER_RANK[tracker] < _TIER_RANK[psh]


def test_concept_token_for_brand_label_in_description():
    """The vision-extracted prescription label might be brand-only
    ('Trulicity 1.5 mg/0.5 mL, Lot ABC123'). The retrieval relies
    on the description field carrying the medical concept so that
    a question like 'when did I start taking GLP-1 agonists' hits
    the row. This test pins the doctrine; the prompt encodes the
    actual behavior. We assert the convention by checking the
    prompt YAML mentions both the brand and the concept-required
    rule."""
    from pathlib import Path
    prompt = Path(__file__).parent.parent / "prompts" / "personal_photo_vision.v1.yaml"
    text = prompt.read_text()
    # The description guidance must mention the brand→concept
    # convention so the prompt teaches the model to include both.
    assert "medical concept" in text.lower()
    assert "GLP-1" in text or "covid" in text.lower(), (
        "vision prompt should show a concept example so the model "
        "extracts 'COVID-19 mRNA vaccine' alongside the brand"
    )
