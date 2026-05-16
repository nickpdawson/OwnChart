"""Medication ingest dedup key tests.

Doctrine reference: docs/DEVELOPMENT_LOG.md 2026-05-15 entry,
`api/ownchart/ingest/auto_export.py::_emit_medication`.

The Health Auto Export iOS app re-pushes the full medication history
on every push. Without a deterministic `client_sample_key`, the same
scheduled dose lands N times. The key is sha256(drug, exact
scheduled ts to second, status). Pure-function tests against the
key-emit logic protect against accidental regressions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ownchart.ingest.auto_export import AutoExportIngest, _emit_medication


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc) if "+" not in iso else datetime.fromisoformat(iso)


def _emit(displayText: str, status: str, start_iso: str) -> str | None:
    out = AutoExportIngest()
    _emit_medication(out, {
        "displayText": displayText,
        "status": status,
        "start": start_iso,
    })
    if not out.facts:
        return None
    return out.facts[0].client_sample_key


def test_same_dose_pushed_twice_gets_same_key():
    """The core dedup invariant: identical inputs hash identically."""
    k1 = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T08:00:00+00:00")
    k2 = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T08:00:00+00:00")
    assert k1 is not None
    assert k1 == k2


def test_different_status_same_dose_gets_different_keys():
    """Morning Taken + Skipped on the same scheduled time should be
    treated as distinct events (rare in practice, but the data shape
    allows it)."""
    k_taken = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T08:00:00+00:00")
    k_skip = _emit("Celebrex 100mg capsule", "Skipped", "2026-05-13T08:00:00+00:00")
    assert k_taken != k_skip


def test_different_scheduled_times_get_different_keys():
    """Two real distinct doses at 8am and 8pm same day should both
    survive the dedup (NOT collapse to one)."""
    k_morning = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T08:00:00+00:00")
    k_evening = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T20:00:00+00:00")
    assert k_morning != k_evening


def test_microsecond_jitter_does_not_create_dupes():
    """If iOS jitters the scheduled timestamp by microseconds between
    pushes, the key must still dedup. We truncate to second precision
    by replace(microsecond=0)."""
    k_a = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T08:00:00.123456+00:00")
    k_b = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T08:00:00.987654+00:00")
    assert k_a == k_b


def test_label_case_insensitive():
    """Drug-name case must not break dedup ('Celebrex' vs 'celebrex'
    in different push formats)."""
    k1 = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T08:00:00+00:00")
    k2 = _emit("CELEBREX 100mg capsule", "Taken", "2026-05-13T08:00:00+00:00")
    assert k1 == k2


def test_key_starts_with_namespace_prefix():
    """Keys are prefixed with 'ae-med-' so we can tell at a glance
    what shape they are and what subsystem owns them."""
    k = _emit("Celebrex 100mg capsule", "Taken", "2026-05-13T08:00:00+00:00")
    assert k is not None and k.startswith("ae-med-")
    # SHA-256 truncated to 32 hex chars + prefix.
    assert len(k) == len("ae-med-") + 32


def test_missing_status_still_emits_key():
    """Status absent → unknown adherence. Key still emits so the
    fact lands; status component of the hash is the empty string."""
    out = AutoExportIngest()
    _emit_medication(out, {
        "displayText": "Celebrex 100mg capsule",
        "start": "2026-05-13T08:00:00+00:00",
    })
    assert len(out.facts) == 1
    assert out.facts[0].client_sample_key is not None


def test_missing_displaytext_drops_fact():
    """The doctrine: no name, no fact. The ingest path drops it
    with a parse warning."""
    out = AutoExportIngest()
    _emit_medication(out, {
        "status": "Taken",
        "start": "2026-05-13T08:00:00+00:00",
    })
    assert len(out.facts) == 0
    assert out.medication_count == 0
    assert any("displayText" in w for w in out.parse_warnings)


def test_missing_start_date_drops_fact():
    """Same — no date, no fact. Tracker entries without a
    scheduled time are useless for chronology."""
    out = AutoExportIngest()
    _emit_medication(out, {
        "displayText": "Celebrex 100mg capsule",
        "status": "Taken",
    })
    assert len(out.facts) == 0
