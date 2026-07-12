"""Legacy Auto-Export UI upload path honors parser overrides (HIGH-3).

`routes/sources.py::upload_auto_export` used to hand-roll its INSERT
loop, hardcoding review_state='confirmed' /
extraction_method='health_auto_export' and dropping the parser's
client_sample_key + equivalence_key — so re-uploading the same JSON
duplicated every dose and marked Skipped doses confirmed. Both the UI
route and the REST-push worker now build rows through
`ingest.auto_export_persist.build_anchor_and_fact`, so these guard that
the shared builder preserves every per-fact override.
"""

from __future__ import annotations

import uuid

from ownchart.ingest.auto_export import AutoExportIngest, _emit_medication
from ownchart.ingest.auto_export_persist import build_anchor_and_fact


def _one_med(displayText, status, start_iso):
    out = AutoExportIngest()
    _emit_medication(out, {
        "displayText": displayText, "status": status, "start": start_iso,
    })
    return out.facts[0]


def test_skipped_dose_is_needs_review_not_confirmed():
    """The core HIGH-3 bug: Skipped doses must NOT be auto-confirmed."""
    f = _one_med("Celebrex 100mg", "Skipped", "2026-05-13T20:00:00+00:00")
    src_id = uuid.uuid4()
    rec_id = uuid.uuid4()
    _anchor, fact = build_anchor_and_fact(f, source_id=src_id, person_record_id=rec_id)
    assert fact.review_state == "needs_review"
    assert fact.extraction_method == "patient_self_report"
    assert fact.why_needs_review_code == "auto_export_dose_skipped"
    assert fact.person_record_id == rec_id


def test_medication_carries_client_sample_key_for_dedup():
    """Without a client_sample_key on the row there is no dedup; the old
    UI path wrote none, so re-uploads duplicated."""
    f = _one_med("Celebrex 100mg", "Taken", "2026-05-13T08:00:00+00:00")
    _anchor, fact = build_anchor_and_fact(
        f, source_id=uuid.uuid4(), person_record_id=uuid.uuid4(),
    )
    assert fact.client_sample_key is not None
    assert fact.client_sample_key.startswith("ae-med-")
    assert fact.extraction_method == "patient_self_report"


def test_medication_anchor_type_is_medication_lane():
    f = _one_med("Celebrex 100mg", "Taken", "2026-05-13T08:00:00+00:00")
    anchor, _fact = build_anchor_and_fact(
        f, source_id=uuid.uuid4(), person_record_id=uuid.uuid4(),
    )
    assert anchor.anchor_type == "auto_export_medication"


def test_metric_fact_defaults_are_wearable_confirmed():
    """A plain metric AutoExportFact (no overrides) keeps the wearable
    defaults through the shared builder."""
    from datetime import datetime, timezone
    from ownchart.ingest.auto_export import AutoExportFact

    f = AutoExportFact(
        fact_type="observation",
        label="Daily steps: 8,523",
        description=None,
        date_start=datetime(2026, 5, 13, tzinfo=timezone.utc),
        date_end=None,
        coded_concepts={"hkquantitytype": ["HKQuantityTypeIdentifierStepCount"]},
    )
    anchor, fact = build_anchor_and_fact(
        f, source_id=uuid.uuid4(), person_record_id=uuid.uuid4(),
    )
    assert fact.review_state == "confirmed"
    assert fact.extraction_method == "health_auto_export"
    assert anchor.anchor_type == "auto_export_metric"
