"""Tests for the Pictal Health Record v1.0 export mapper + plumbing.

Two layers:

  1. **Pure-mapper tests** — build a synthetic ExportSnapshot in memory
     and verify the mapper produces the documented v1.0 shape, with
     deterministic ordering, preserved date precision, honest
     active/resolved classification, body-signal exclusion, and the
     no-internal-UUID / no-secret invariants.

  2. **Static-source pins** — assertions that the route Literal, the
     model tuple, the runner FILENAME_FOR_TYPE map, and migration
     0046's CHECK constraints all reference ``pictal_json``. These
     catch a future refactor that drops one of the wiring touches.

Live-DB round-trip via the runner is exercised in
test_exports_slice4.py's parametrized suite once the migration runs
in the integration env.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pytest

from ownchart.exports.mappers import (
    _PICTAL_SECTIONS,
    pictal_health_json_mapper,
)
from ownchart.exports.snapshot import ExportSnapshot, _SnapshotFact, _SnapshotRecord


# ---------------------------------------------------------------------------
# Helpers


def _record(**overrides) -> _SnapshotRecord:
    base: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "display_name": "Avery Walker",
        "given_names": "Avery",
        "family_name": "Walker",
        "birth_date": date(1984, 4, 12),
        "gender": None,
        "is_self": True,
    }
    base.update(overrides)
    return _SnapshotRecord(**base)


def _fact(**overrides) -> _SnapshotFact:
    base: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000abc",
        "fact_type": "condition",
        "label": "Type 2 diabetes",
        "description": None,
        "date_start": None,
        "date_end": None,
        "date_precision": None,
        "coded_concepts": None,
        "confidence": None,
        "review_state": "pending",
        "significance": None,
        "significance_source": None,
        "extraction_method": "claude_clinical_note_v1",
        "date_provenance": None,
        "historical_status": None,
        "created_at": datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return _SnapshotFact(**base)


def _snapshot(*, record=None, facts=None) -> ExportSnapshot:
    return ExportSnapshot(
        generated_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        record=record or _record(),
        sources=[],
        facts=facts or [],
        calendar_sources=[],
        calendar_events=[],
    )


def _render(snapshot: ExportSnapshot) -> dict:
    return json.loads(pictal_health_json_mapper(snapshot).decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. Top-level shape


def test_top_level_keys_match_pictal_v1_contract():
    out = _render(_snapshot())
    assert out["_format"] == "Pictal Health Record v1.0"
    assert "patient" in out
    for section in _PICTAL_SECTIONS:
        assert section in out, f"missing section {section!r}"
        assert isinstance(out[section], list), section


def test_unused_sections_are_empty_arrays_not_missing_keys():
    """v1.0 shape must be stable — consumers can't tell empty from absent."""
    out = _render(_snapshot())
    for section in _PICTAL_SECTIONS:
        assert out[section] == [], section


# ---------------------------------------------------------------------------
# 2. Patient block


def test_patient_name_prefers_given_family():
    out = _render(_snapshot(record=_record(
        display_name="Should not be used",
        given_names="Avery",
        family_name="Walker",
    )))
    assert out["patient"]["name"] == "Avery Walker"


def test_patient_name_falls_back_to_display_name():
    out = _render(_snapshot(record=_record(
        display_name="Me",
        given_names=None,
        family_name=None,
    )))
    assert out["patient"]["name"] == "Me"


def test_patient_birth_date_renders_iso():
    out = _render(_snapshot(record=_record(birth_date=date(1984, 4, 12))))
    assert out["patient"]["date_of_birth"] == "1984-04-12"


def test_patient_birth_date_null_when_missing():
    out = _render(_snapshot(record=_record(birth_date=None)))
    assert out["patient"]["date_of_birth"] is None


# ---------------------------------------------------------------------------
# 3. Fact bucketing (one per section)


@pytest.mark.parametrize(
    "fact_type, expected_section",
    [
        ("condition", "diagnoses"),
        ("diagnosis", "diagnoses"),
        ("problem", "diagnoses"),
        ("allergy_intolerance", "diagnoses"),
        ("medication", "medications_and_treatments"),
        ("medication_request", "medications_and_treatments"),
        ("treatment", "medications_and_treatments"),
        ("procedure", "surgeries_and_procedures"),
        ("surgery", "surgeries_and_procedures"),
        ("immunization", "surgeries_and_procedures"),
        ("hospitalization", "hospitalizations"),
        ("admission", "hospitalizations"),
        ("encounter_inpatient", "hospitalizations"),
        ("lab", "tests_and_imaging"),
        ("imaging", "tests_and_imaging"),
        ("diagnostic_report", "tests_and_imaging"),
        ("observation", "tests_and_imaging"),
        ("injury", "injuries_and_illnesses"),
        ("illness", "injuries_and_illnesses"),
        ("infection", "injuries_and_illnesses"),
        ("symptom", "symptoms"),
        ("tobacco", "substance_use"),
        ("alcohol", "substance_use"),
        ("life_event", "life_events"),
    ],
)
def test_fact_type_maps_to_pictal_section(fact_type, expected_section):
    f = _fact(fact_type=fact_type, label=f"Sample {fact_type}")
    out = _render(_snapshot(facts=[f]))
    assert len(out[expected_section]) == 1
    assert out[expected_section][0]["label"] == f"Sample {fact_type}"
    # All other sections empty.
    for section in _PICTAL_SECTIONS:
        if section != expected_section:
            assert out[section] == [], section


def test_unmapped_fact_type_is_dropped_not_dumped():
    """Conservative bucketing — unknown fact_type means omit, not guess."""
    out = _render(_snapshot(facts=[_fact(fact_type="some_future_thing")]))
    for section in _PICTAL_SECTIONS:
        assert out[section] == []


# ---------------------------------------------------------------------------
# 4. Date precision preservation


@pytest.mark.parametrize(
    "precision, dt, expected",
    [
        ("year",       datetime(2018, 1, 1, tzinfo=timezone.utc), "2018"),
        ("month",      datetime(2018, 3, 1, tzinfo=timezone.utc), "2018-03"),
        ("day",        datetime(2018, 3, 14, tzinfo=timezone.utc), "2018-03-14"),
        ("full",       datetime(2018, 3, 14, 9, 30, tzinfo=timezone.utc), "2018-03-14"),
        (None,         datetime(2018, 3, 14, tzinfo=timezone.utc), "2018-03-14"),
    ],
)
def test_date_precision_preserved(precision, dt, expected):
    f = _fact(fact_type="condition", date_start=dt, date_precision=precision)
    out = _render(_snapshot(facts=[f]))
    assert out["diagnoses"][0]["date"] == expected


def test_date_null_when_no_date_start():
    f = _fact(fact_type="condition", date_start=None)
    out = _render(_snapshot(facts=[f]))
    assert out["diagnoses"][0]["date"] is None


# ---------------------------------------------------------------------------
# 5. Active vs resolved classification


def test_status_active_for_default_buckets_without_resolved_signal():
    """diagnoses, meds, symptoms, substance_use default to active."""
    for fact_type, expected_section in [
        ("condition", "diagnoses"),
        ("medication", "medications_and_treatments"),
        ("symptom", "symptoms"),
        ("alcohol", "substance_use"),
    ]:
        f = _fact(fact_type=fact_type, historical_status=None, date_end=None)
        out = _render(_snapshot(facts=[f]))
        assert out[expected_section][0]["status"] == "active", expected_section


def test_status_resolved_when_historical_status_says_so():
    for hist in ("resolved", "inactive", "remission", "history_of"):
        f = _fact(fact_type="condition", historical_status=hist)
        out = _render(_snapshot(facts=[f]))
        assert out["diagnoses"][0]["status"] == "resolved", hist


def test_status_resolved_when_date_end_set():
    f = _fact(
        fact_type="condition",
        date_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        date_end=datetime(2022, 6, 1, tzinfo=timezone.utc),
    )
    out = _render(_snapshot(facts=[f]))
    assert out["diagnoses"][0]["status"] == "resolved"


def test_status_null_for_buckets_without_active_concept():
    """Tests, procedures, hospitalizations don't have active/resolved.
    Don't claim a status we can't honestly derive."""
    for fact_type, expected_section in [
        ("lab", "tests_and_imaging"),
        ("imaging", "tests_and_imaging"),
        ("procedure", "surgeries_and_procedures"),
        ("hospitalization", "hospitalizations"),
        ("injury", "injuries_and_illnesses"),
    ]:
        f = _fact(fact_type=fact_type, historical_status=None, date_end=None)
        out = _render(_snapshot(facts=[f]))
        assert out[expected_section][0]["status"] is None, expected_section


# ---------------------------------------------------------------------------
# 6. Body-signal exclusion


def test_body_signal_facts_excluded_even_in_observation_type():
    """Native HealthKit observations would otherwise file under
    tests_and_imaging. Pictal isn't a quantified-self dump target —
    drop them silently."""
    f_clinical = _fact(
        fact_type="observation",
        label="Hemoglobin A1c 7.1%",
        extraction_method="claude_clinical_note_v1",
    )
    f_body = _fact(
        fact_type="observation",
        label="Heart rate 67 bpm",
        extraction_method="native_healthkit",
    )
    f_auto = _fact(
        fact_type="observation",
        label="Steps 8,432",
        extraction_method="health_auto_export",
    )
    out = _render(_snapshot(facts=[f_clinical, f_body, f_auto]))
    labels = [item["label"] for item in out["tests_and_imaging"]]
    assert "Hemoglobin A1c 7.1%" in labels
    assert "Heart rate 67 bpm" not in labels
    assert "Steps 8,432" not in labels


# ---------------------------------------------------------------------------
# 7. Rejected facts excluded


def test_rejected_facts_excluded():
    """User said 'no, this isn't true' — out."""
    f = _fact(fact_type="condition", review_state="rejected")
    out = _render(_snapshot(facts=[f]))
    assert out["diagnoses"] == []


# ---------------------------------------------------------------------------
# 8. Notes / provenance — never expose internal UUIDs


def test_notes_combines_provenance_hint_and_description():
    f = _fact(
        fact_type="condition",
        date_provenance="user_confirmed",
        description="Diagnosed by Dr. Lee after labs.",
    )
    out = _render(_snapshot(facts=[f]))
    notes = out["diagnoses"][0]["notes"]
    assert notes is not None
    assert "Patient-confirmed date." in notes
    assert "Diagnosed by Dr. Lee after labs." in notes


def test_notes_null_when_no_signal():
    f = _fact(fact_type="condition", description=None, date_provenance=None)
    out = _render(_snapshot(facts=[f]))
    assert out["diagnoses"][0]["notes"] is None


# ---------------------------------------------------------------------------
# 9. No-internal-UUID / no-secret invariants


def test_output_contains_no_internal_uuids():
    """Snapshot has UUIDs everywhere (record.id, fact.id, source.id).
    The Pictal output must not surface any of them — Pictal v1.0 has
    no provenance field and we promised not to invent one."""
    rec = _record(id="11111111-1111-1111-1111-111111111111")
    f = _fact(
        id="22222222-2222-2222-2222-222222222222",
        fact_type="condition",
    )
    raw = pictal_health_json_mapper(_snapshot(record=rec, facts=[f]))
    body = raw.decode("utf-8")
    assert "11111111" not in body
    assert "22222222" not in body
    assert "person_record_id" not in body
    assert "evidence_anchor_ids" not in body
    assert "id" not in {k for k in json.loads(body).keys()}


def test_output_contains_no_credential_or_token_strings():
    """Defensive: even though the snapshot never holds tokens, pin
    that the mapper hasn't started spilling something secret-shaped."""
    f = _fact(fact_type="condition")
    body = pictal_health_json_mapper(_snapshot(facts=[f])).decode("utf-8")
    for needle in ("access_token", "refresh_token", "client_secret",
                   "session", "Bearer", "sk-ant-api"):
        assert needle not in body, needle


# ---------------------------------------------------------------------------
# 10. Determinism


def test_same_snapshot_yields_byte_identical_output():
    f1 = _fact(fact_type="condition", label="A",
               date_start=datetime(2024, 1, 1, tzinfo=timezone.utc))
    f2 = _fact(fact_type="condition", label="B",
               date_start=datetime(2023, 1, 1, tzinfo=timezone.utc))
    snap = _snapshot(facts=[f1, f2])
    a = pictal_health_json_mapper(snap)
    b = pictal_health_json_mapper(snap)
    assert a == b


def test_section_ordering_within_is_by_date_then_label():
    """Older first, ties broken by case-insensitive label."""
    f_old = _fact(fact_type="condition", label="Zebra",
                  date_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
                  date_precision="day")
    f_mid = _fact(fact_type="condition", label="apple",
                  date_start=datetime(2022, 1, 1, tzinfo=timezone.utc),
                  date_precision="day")
    f_new = _fact(fact_type="condition", label="Mango",
                  date_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                  date_precision="day")
    f_undated = _fact(fact_type="condition", label="No date",
                      date_start=None)
    out = _render(_snapshot(facts=[f_new, f_undated, f_mid, f_old]))
    labels = [it["label"] for it in out["diagnoses"]]
    # Dated rows in chronological order; undated last.
    assert labels == ["Zebra", "apple", "Mango", "No date"]


# ---------------------------------------------------------------------------
# 11. Static-source pins: wiring touch-points reference pictal_json


def test_route_request_literal_includes_pictal_json():
    import inspect
    from ownchart.routes import exports as routes_exports
    src = inspect.getsource(routes_exports)
    assert '"pictal_json"' in src, (
        "CreateExportRequest.requested_format Literal must include "
        "'pictal_json' so /api/exports accepts it."
    )


def test_route_download_literal_includes_pictal_json():
    import inspect
    from ownchart.routes import exports as routes_exports
    src = inspect.getsource(routes_exports)
    assert 'file_type: Literal[' in src and '"pictal_json"' in src, (
        "download_export's file_type Literal must include 'pictal_json' "
        "so /api/exports/{id}/download?file_type=pictal_json is reachable."
    )


def test_model_export_job_tuple_includes_pictal_json():
    from ownchart.models.export_job import REQUESTED_FORMATS
    assert "pictal_json" in REQUESTED_FORMATS


def test_model_export_file_tuple_includes_pictal_json():
    from ownchart.models.export_file import FILE_TYPES
    assert "pictal_json" in FILE_TYPES


def test_runner_filename_map_includes_pictal_json():
    from ownchart.exports import runner
    assert runner._FILENAME_FOR_TYPE["pictal_json"] == "pictal_health.json"


def test_runner_dispatches_pictal_json_branch():
    import inspect
    from ownchart.exports import runner
    src = inspect.getsource(runner.run_export_job)
    assert 'file_type == "pictal_json"' in src
    assert "pictal_health_json_mapper(snapshot)" in src


# ---------------------------------------------------------------------------
# 12. Migration 0046 — static check on CHECK-constraint widening


def _migration_0046_source() -> str:
    """Read the alembic version file by path — its filename starts
    with a digit so a normal import won't work."""
    from pathlib import Path
    here = Path(__file__).resolve().parents[2]  # api/
    return (here / "alembic" / "versions"
            / "0046_pictal_json_export.py").read_text(encoding="utf-8")


def test_migration_0046_widens_export_jobs_requested_format_chk():
    src = _migration_0046_source()
    assert "export_jobs_requested_format_chk" in src
    assert "'ownchart_json','txt','pictal_json','all'" in src


def test_migration_0046_widens_export_files_file_type_chk():
    src = _migration_0046_source()
    assert "export_files_file_type_chk" in src
    assert "'ownchart_json','txt','pictal_json'" in src


def test_migration_0046_follows_0045():
    """Linear history: 0046 must revise 0045."""
    src = _migration_0046_source()
    assert 'revision = "0046_pictal_json_export"' in src
    assert 'down_revision = "0045_export_job_filters"' in src
