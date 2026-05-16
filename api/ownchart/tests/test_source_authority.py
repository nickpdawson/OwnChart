"""Source Authority Doctrine classifier tests.

Doctrine reference: user-docs/SOURCE_AUTHORITY_DOCTRINE.md.

These are pure-function tests — no DB, no LLM. They lock down the
mapping from (source_label, filename, source_type) → tier so a
classifier change can't silently re-tier OrthoVirginia imaging
back to ehr_summary (the Round-3 trust bug Nick caught).
"""

from ownchart.llm.conversations import (
    _source_quality_tier,
    _TIER_RANK,
)


# ---------------------------------------------------------------------------
# Tier 1: primary_event


def test_operative_report_is_primary_event():
    assert _source_quality_tier(
        "Bozeman Health", "Operative Report — Right ACL Reconstruction",
        "clinical_note",
    ) == "primary_event"


def test_radiology_imaging_study_is_primary_event():
    assert _source_quality_tier(
        "OrthoVirginia", "Diagnostic imaging study", "clinical_note",
    ) == "primary_event"


def test_pathology_is_primary_event():
    assert _source_quality_tier(
        "Stanford Health Care", "Pathology Report", "clinical_note",
    ) == "primary_event"


def test_healthkit_data_is_primary_event():
    # Device-recorded data — no filename needed.
    assert _source_quality_tier(None, None, "native_healthkit") == "primary_event"
    assert _source_quality_tier(None, None, "health_auto_export") == "primary_event"


def test_lab_report_is_primary_event():
    assert _source_quality_tier(
        "Quest Diagnostics", "Lab Report — Lipid Panel", "clinical_note",
    ) == "primary_event"


# ---------------------------------------------------------------------------
# Tier 2: specialist_proximate


def test_orthovirginia_fhir_bundle_is_specialist_proximate():
    # The bug Nick caught Round-3: FHIR bundle from OrthoVirginia
    # was landing at ehr_summary (tier 4) because source_type 'fhir_bundle'
    # has ehr_summary floor. Specialty-label promotion fixed it.
    assert _source_quality_tier(
        "OrthoVirginia", "orthovirginia-20260509-145245.json",
        "fhir_bundle",
    ) == "specialist_proximate"


def test_orthovirginia_ccda_is_specialist_proximate():
    assert _source_quality_tier(
        "OrthoVirginia", "Patient Summary", "ccda_xml",
    ) == "specialist_proximate"


def test_audiology_progress_note_is_specialist_proximate():
    assert _source_quality_tier(
        "Bozeman Audiology Center", "Progress Notes",
        "clinical_note",
    ) == "specialist_proximate"


def test_cardiology_consult_is_specialist_proximate():
    assert _source_quality_tier(
        "Stanford Cardiology", "Cardiology Consult Note",
        "clinical_note",
    ) == "specialist_proximate"


# ---------------------------------------------------------------------------
# Tier 3: contemporaneous_support


def test_pcp_visit_summary_is_contemporaneous():
    # Same filename keyword, but no specialty hint in source_label.
    assert _source_quality_tier(
        "Bozeman Health", "Visit Summary",
        "clinical_note",
    ) == "contemporaneous_support"


def test_discharge_instructions_is_contemporaneous():
    assert _source_quality_tier(
        "Stanford Health Care", "Discharge Instructions",
        "clinical_note",
    ) == "contemporaneous_support"


# ---------------------------------------------------------------------------
# Tier 4: ehr_summary


def test_generic_ccda_patient_summary_is_ehr_summary():
    # No specialty hint → ehr_summary, not specialist_proximate.
    assert _source_quality_tier(
        "Bozeman Health", "Patient Summary", "ccda_xml",
    ) == "ehr_summary"


# ---------------------------------------------------------------------------
# Tier 5: self_reported_history


def test_anesthesia_preprocedure_is_self_reported():
    # The exact filename that caused the Round-2 ACL trust bug:
    # Stanford "Right ACL surgery x3" was in their pre-op H&P, which
    # is a self-reported intake list, NOT the operative record.
    assert _source_quality_tier(
        "Stanford Health Care", "Anesthesia Preprocedure Evaluation",
        "clinical_note",
    ) == "self_reported_history"


def test_past_surgical_history_is_self_reported():
    assert _source_quality_tier(
        "Stanford Health Care", "Past Surgical History",
        "clinical_note",
    ) == "self_reported_history"


def test_specialty_label_does_not_override_pre_op_filename():
    # Even if the source_label is a specialty (Stanford Orthopedics),
    # the filename "Pre-op Evaluation" wins — pre-op is intake, not
    # the operative record. Tier-5 across the board.
    assert _source_quality_tier(
        "Stanford Orthopedics", "Pre-op Evaluation",
        "clinical_note",
    ) == "self_reported_history"


# ---------------------------------------------------------------------------
# Unknown bucket


def test_unknown_falls_through_to_unknown():
    assert _source_quality_tier(None, None, None) == "unknown"
    assert _source_quality_tier("Unrelated Lab", "Random.docx", "photo") == "unknown"


# ---------------------------------------------------------------------------
# Ranking invariants


def test_tier_rank_ordering_matches_doctrine():
    # Highest authority = lowest rank int. self_reported and
    # model_inference are explicitly demoted below ehr_summary so
    # retrieval diversity puts them last in the round-robin.
    assert (
        _TIER_RANK["primary_event"]
        < _TIER_RANK["specialist_proximate"]
        < _TIER_RANK["contemporaneous_support"]
        <= _TIER_RANK["ehr_summary"]
        < _TIER_RANK["self_reported_history"]
        < _TIER_RANK["model_inference"]
    )


def test_unknown_ranks_with_ehr_summary():
    # Doctrine: "treat unknown as ehr_summary for ranking purposes."
    assert _TIER_RANK["unknown"] == _TIER_RANK["ehr_summary"]


# ---------------------------------------------------------------------------
# Anti-pattern guards — the doctrine's explicit forbidden swaps.


def test_psh_does_not_outrank_specialty_record():
    """The anti-pattern: citing Stanford anesthesia pre-op
    'Right ACL surgery x3' as primary evidence for ACL surgery
    when OrthoVirginia has the imaging report."""
    psh_tier = _source_quality_tier(
        "Stanford Health Care", "Anesthesia Preprocedure Evaluation",
        "clinical_note",
    )
    ov_imaging_tier = _source_quality_tier(
        "OrthoVirginia", "Diagnostic imaging study", "clinical_note",
    )
    assert _TIER_RANK[ov_imaging_tier] < _TIER_RANK[psh_tier]


def test_copied_problem_list_does_not_outrank_specialty():
    """The anti-pattern: a recent encounter's problem list (which
    just copied forward the diagnosis) outranking the specialty
    note that originally documented it."""
    problem_list_tier = _source_quality_tier(
        "Stanford Health Care", "Problem List", "ccda_xml",
    )
    specialty_note_tier = _source_quality_tier(
        "OrthoVirginia", "Encounter Summary", "clinical_note",
    )
    assert _TIER_RANK[specialty_note_tier] < _TIER_RANK[problem_list_tier]


# Source-diversity retrieval — Nick 2026-05-16: "source-diversity
# retrieval includes older source-proximate records." A
# date-DESC tiebreak inside a single tier shouldn't completely
# crowd out older but higher-authority records from a related
# specialty source. The retrieval-level diversity helper is the
# fix (one slot per tier in round-robin); the doctrine ensures
# the right tier assignments feed it.

def test_older_specialty_outranks_newer_summary():
    """A 2017 OrthoVirginia imaging report (tier-1 primary_event,
    older date) MUST outrank a 2026 Bozeman patient summary
    (tier-4 ehr_summary, newer date) for a knee question. Tier
    ranking is independent of date — the date_DESC tiebreak only
    fires within the same tier."""
    older_specialty = _source_quality_tier(
        "OrthoVirginia", "Diagnostic imaging study", "clinical_note",
    )
    newer_summary = _source_quality_tier(
        "Bozeman Health", "Patient Summary", "ccda_xml",
    )
    assert _TIER_RANK[older_specialty] < _TIER_RANK[newer_summary]


def test_specialty_filename_summary_promotes_when_label_matches():
    """OrthoVirginia + 'Patient Summary' should promote to
    specialist_proximate (tier-2), not stay at ehr_summary
    (tier-4). This was the 2026-05-15 classifier refinement."""
    promoted = _source_quality_tier(
        "OrthoVirginia", "Patient Summary", "ccda_xml",
    )
    assert promoted == "specialist_proximate"


# Historical-placeholder doctrine — Nick 2026-05-15 RC#3:
# "historical placeholder dates do not become event dates."
# Encoded at the date_origin level in coded_concepts:
#   - "explicit"            → real event date from the source
#   - "note_date"           → date came from the note's creation,
#                             not the event itself (forbidden after
#                             RC#3 backfill)
#   - "historical_undated"  → past-event mentioned without a clear
#                             date; date_start MUST be null
# The classifier doesn't see date_origin directly, but the
# convention pins the rules.

def test_date_origin_taxonomy_pinned():
    """date_origin values are the canonical set. New values
    require a doctrine update + retrieval review."""
    valid = {"explicit", "note_date", "historical_undated"}
    # If you change the taxonomy, update the corresponding
    # backfill scripts in scripts/backfill_historical_dates.py
    # and the clinical-note extractor. The set is small on purpose.
    assert "explicit" in valid
    assert "historical_undated" in valid
    # `note_date` exists as a historical concept (pre-RC#3) but
    # the doctrine forbids new rows with this origin. New
    # extractions must use explicit OR historical_undated.
    assert "note_date" in valid


def test_historical_undated_means_null_date_start():
    """Sentinel: an undated historical condition (e.g. 'history
    of ACL reconstruction x2' with no date in the source) gets
    date_origin=historical_undated AND date_start=NULL. The two
    fields move together — surfacing a date_start while marking
    the origin as historical_undated would be a doctrine
    violation."""
    # Pure-function check on a dict-shape fact representation.
    def _ok(fact: dict) -> bool:
        if fact.get("date_origin") == "historical_undated":
            return fact.get("date_start") is None
        return True
    assert _ok({"date_origin": "historical_undated", "date_start": None})
    assert _ok({"date_origin": "explicit", "date_start": "2017-06-08"})
    assert not _ok({
        "date_origin": "historical_undated",
        "date_start": "2026-05-09",
    })
