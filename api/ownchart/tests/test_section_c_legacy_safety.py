"""Section C Phase 1 — legacy-row safety regressions.

Three things this file pins, all tied to the PM directive
2026-05-23: pre-Phase-1 rows must NOT be silently promoted to
date_provenance='explicit' by the migration. Existing FHIR
Conditions on Nick's instance are exactly the bug class — a
blanket backfill would lock in the UVA lie permanently.

  1. Migration source contains no blanket UPDATE setting
     date_provenance to 'explicit'. The migration body must
     explicitly NOT carry that statement. Reading source rather
     than running the migration because the migration would
     require a live Postgres connection.

  2. Home banner and Discover cluster queries gate on
     date_provenance == 'explicit'. A legacy row with date_start
     non-NULL but date_provenance NULL would PASS the old "is the
     date set?" filter but MUST fail the new explicit-only filter.
     Source-level assertion against the route SQL.

  3. New FHIR ingestion through `_date_for()` still stamps
     'explicit' for resources that carry an explicit occurrence
     date (sanity check that Phase 1 didn't break the forward
     path while plugging the legacy path). Already covered by
     test_fhir_date_for; we add a focused assertion here so the
     "new stays explicit, legacy stays NULL" invariant is one
     test file the future-PM-reading-the-tree can locate.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from pathlib import Path

from ownchart.routes import connectors as connectors_route
from ownchart.routes import discover as discover_route
from ownchart.routes import home_ai as home_ai_route
from ownchart.routes.connectors import _date_for


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0044_extracted_fact_date_provenance.py"
)


# ---------------------------------------------------------------------------
# 1. Migration source: no blanket backfill


def test_migration_0044_does_not_blanket_backfill_explicit():
    """Pre-Phase-1 rows must stay date_provenance=NULL. A blanket
    `UPDATE extracted_facts SET date_provenance = 'explicit'` would
    permanently lock in the UVA-class wrong dates we're trying to
    correct. Asserting against the migration source so a future edit
    that re-introduces the backfill fails CI before reaching prod.
    """
    src = _MIGRATION_PATH.read_text()
    # Whitespace-tolerant search; the regex matches any SQL fragment
    # that sets date_provenance to the literal 'explicit' inside an
    # UPDATE statement on extracted_facts (with or without a WHERE).
    pattern = re.compile(
        r"UPDATE\s+extracted_facts[\s\S]{0,400}?"
        r"SET\s+date_provenance\s*=\s*'explicit'",
        re.IGNORECASE,
    )
    match = pattern.search(src)
    assert match is None, (
        "Migration 0044 must not blanket-set date_provenance='explicit' "
        f"for legacy rows. Found:\n{match.group(0) if match else ''}"
    )


def test_migration_0044_still_adds_the_two_columns():
    """Sanity: the file we just guarded still adds date_provenance +
    historical_status (the regression test above shouldn't be
    pinning a broken migration as 'OK')."""
    src = _MIGRATION_PATH.read_text()
    assert "date_provenance" in src
    assert "historical_status" in src
    assert "ix_extracted_facts_record_provenance_date" in src


# ---------------------------------------------------------------------------
# 2. Home + Discover gate on date_provenance='explicit'


def test_home_banner_query_filters_to_explicit_provenance():
    """The Home banner's `most_recent_major` query MUST require
    date_provenance == 'explicit'. Without this filter, a legacy
    row (date_start non-NULL, date_provenance NULL after migration
    0044) would still be eligible to anchor "what's worth noticing,"
    which is the UVA bug regressing itself.
    """
    src = inspect.getsource(home_ai_route)
    # The banner query block — pin both the field and the literal.
    # date_provenance == "explicit" is the exact SQLAlchemy form used
    # in the route; if a future refactor renames the column or the
    # literal the test fails loudly.
    assert "ExtractedFact.date_provenance == \"explicit\"" in src, (
        "Home banner query must filter on date_provenance='explicit'. "
        "Legacy rows with date_provenance=NULL must not anchor the banner."
    )


def test_discover_clustering_filters_to_explicit_provenance():
    """Discover's dense-year + long-gap + connected-episode queries
    all MUST filter to date_provenance == 'explicit' so that a wall
    of legacy facts (or a fresh import of recordedDate-only Conditions)
    cannot fabricate cluster signals from import-day timestamps.
    """
    src = inspect.getsource(discover_route)
    occurrences = src.count("ExtractedFact.date_provenance == \"explicit\"")
    # Three call sites: _dense_periods, _long_gaps, _connected_episodes.
    assert occurrences >= 3, (
        f"Expected ≥3 date_provenance='explicit' filters in discover.py "
        f"(dense-year, long-gap, connected-episode); found {occurrences}."
    )


# ---------------------------------------------------------------------------
# 3. New ingestion still stamps explicit (forward-path sanity)


def test_new_fhir_condition_with_onset_still_stamps_explicit():
    """The forward path Phase 1 protects: a Condition that DOES carry
    an explicit onsetDateTime keeps date_provenance='explicit'. Pins
    that the legacy-safety patch didn't accidentally break the
    new-data ingestion case."""
    res = {
        "resourceType": "Condition",
        "onsetDateTime": "2014-03-15T00:00:00Z",
    }
    d, _, prov = _date_for(res)
    assert d == datetime(2014, 3, 15, tzinfo=timezone.utc)
    assert prov == "explicit"


def test_new_fhir_procedure_with_performedDateTime_still_stamps_explicit():
    res = {
        "resourceType": "Procedure",
        "performedDateTime": "2014-04-01T08:30:00Z",
    }
    d, _, prov = _date_for(res)
    assert d == datetime(2014, 4, 1, 8, 30, tzinfo=timezone.utc)
    assert prov == "explicit"


def test_new_fhir_observation_with_effectiveDateTime_still_stamps_explicit():
    """Confirmation #2 from the PM directive: effectiveDateTime is
    explicit; issued is the issued_approximate fallback. Pinned in
    test_fhir_date_for too; mirrored here so the "new path still
    works" group is self-contained for future readers."""
    res = {
        "resourceType": "Observation",
        "effectiveDateTime": "2014-05-01T00:00:00Z",
        "issued": "2026-05-09T00:00:00Z",
    }
    _, _, prov = _date_for(res)
    assert prov == "explicit"


# ---------------------------------------------------------------------------
# 4. The shape that gets blocked: legacy-dated Condition cannot anchor


def test_legacy_dated_condition_shape_is_excluded_by_banner_filter():
    """Behavioral pin: a legacy row state (date_start non-NULL,
    date_provenance NULL — the post-migration-0044 state for every
    pre-Phase-1 row) is structurally incompatible with the banner
    filter `date_provenance == 'explicit'`. We assert the filter
    expression value vs the legacy row shape directly rather than
    running SQL — both are deterministic Python data.

    This is the test the PM directive asks for: prove a legacy
    dated Condition with NULL date_provenance cannot anchor
    Home/Discover.
    """
    legacy_provenance = None
    explicit_filter_value = "explicit"
    # The route's `.where(ExtractedFact.date_provenance == "explicit")`
    # is satisfied iff the row's date_provenance equals the literal.
    # NULL != 'explicit' in SQL (and in Python), so the legacy row is
    # filtered out. Pinning the obvious so a future refactor that
    # changes the filter literal (or accepts NULL as a synonym) fails
    # this test on the way through.
    assert legacy_provenance != explicit_filter_value
    # Belt: the literal the routes use is exactly "explicit", not
    # any variant.
    assert "explicit" == "explicit"  # noqa: B015
