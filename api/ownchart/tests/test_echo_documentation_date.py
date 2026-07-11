"""Echo Phase 0 — documentation-date provenance + note-date leak pins.

The canonical echo: a clinical note dated 2024 mentions a historical
ACL tear (the patient tore it ~2003). The 2024 note is where it was
*documented*; the tear did not happen in 2024. Pre-Phase-0 the LLM
clinical-note extractor default-dated same-day findings to the note's
creation date but left the `date_provenance` COLUMN NULL, so the leak
was invisible to the readers that gate on provenance.

Phase 0 makes the COLUMN authoritative for every extraction path and
adds a `documentation_date` value meaning "this date is when the fact
was documented, not a confirmed event date." These pins are all
pure-function (no DB, no LLM):

  1. `_resolve_note_date` — the LLM-path date resolver. The ACL echo
     must NEVER come back as ('explicit', 2024).
  2. `_looks_historical` — the past-medical-history heuristic that
     routes "history of ACL tear" to the undated branch.
  3. The migration adds `documentation_date` to the CHECK constraint.
  4. The Ask evidence block phrases `documentation_date` honestly.
  5. Home/Discover still gate on `== 'explicit'`, so documentation-date
     facts are excluded like echoes (NOT broadened).
  6. Forward-only unification: the LLM extractor no longer writes the
     parallel `coded_concepts.date_origin` vocabulary.
  7. CCDA + vision paths stamp the COLUMN ('explicit' when dated).
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from pathlib import Path

from ownchart.extract import clinical_note as clinical_note_mod
from ownchart.extract import vision as vision_mod
from ownchart.extract.clinical_note import _looks_historical, _resolve_note_date
from ownchart.llm import conversations as conversations_mod
from ownchart.routes import discover as discover_route
from ownchart.routes import home_ai as home_ai_route
from ownchart.routes import sources as sources_route


_NOTE_DATE_2024 = datetime(2024, 6, 1, tzinfo=timezone.utc)
_ACL_ONSET_2003 = datetime(2003, 9, 1, tzinfo=timezone.utc)


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0047_date_provenance_documentation_date.py"
)


# ---------------------------------------------------------------------------
# 1. The ACL echo — the headline regression


def test_acl_echo_history_of_is_not_explicit_2024():
    """A 2024 note saying 'History of ACL tear' with no explicit date
    must resolve to (None, None) — undated history, NOT an explicit
    2024 event. This is the echo the whole phase exists to stop."""
    is_hist = _looks_historical("History of ACL tear", None)
    assert is_hist is True
    resolved_date, prov = _resolve_note_date(
        None, is_hist, _NOTE_DATE_2024,
    )
    assert resolved_date is None, "historical echo must not inherit the note date"
    assert prov is None, "historical echo must not be stamped 'explicit'"
    # And it certainly must not be dated 2024.
    assert resolved_date != _NOTE_DATE_2024


def test_acl_with_explicit_onset_keeps_that_date_explicit():
    """If the LLM *does* extract the real onset (~2003) from the prose,
    that date wins and is provenance='explicit'."""
    resolved_date, prov = _resolve_note_date(
        _ACL_ONSET_2003, False, _NOTE_DATE_2024,
    )
    assert resolved_date == _ACL_ONSET_2003
    assert prov == "explicit"


def test_same_day_finding_is_documentation_date_not_explicit():
    """A same-day clinical finding (non-historical, no explicit date)
    inherits the note date but is stamped 'documentation_date' so it is
    treated like an echo, never as a confirmed 2024 event."""
    resolved_date, prov = _resolve_note_date(
        None, False, _NOTE_DATE_2024,
    )
    assert resolved_date == _NOTE_DATE_2024
    assert prov == "documentation_date"
    assert prov != "explicit"


def test_no_datable_input_stays_null():
    resolved_date, prov = _resolve_note_date(None, False, None)
    assert resolved_date is None
    assert prov is None


# ---------------------------------------------------------------------------
# 2. Historical heuristic covers the ACL phrasings


def test_looks_historical_prefixes():
    for label in (
        "History of ACL tear",
        "h/o anterior cruciate ligament repair",
        "s/p ACL reconstruction",
        "Status post ACL reconstruction x2",
        "Prior ACL tear, left knee",
    ):
        assert _looks_historical(label, None) is True, label


def test_active_finding_not_historical():
    assert _looks_historical("Left knee effusion", None) is False
    # Resolved status still routes to history even without a prefix.
    assert _looks_historical("ACL tear", "resolved") is True


# ---------------------------------------------------------------------------
# 3. Migration adds documentation_date to the CHECK constraint


def test_migration_0047_adds_documentation_date_value():
    src = _MIGRATION_PATH.read_text()
    assert "documentation_date" in src
    # Both the up-constraint and the down-constraint recreation must
    # name the constraint we are swapping.
    assert "extracted_facts_date_provenance_chk" in src


def test_migration_0047_does_not_blanket_backfill():
    """Same safety posture as 0044 — no UPDATE promoting legacy rows."""
    src = _MIGRATION_PATH.read_text()
    pattern = re.compile(
        r"UPDATE\s+extracted_facts[\s\S]{0,400}?SET\s+date_provenance",
        re.IGNORECASE,
    )
    assert pattern.search(src) is None


# ---------------------------------------------------------------------------
# 4. Evidence block phrases documentation_date honestly


def test_evidence_block_has_documentation_date_case():
    src = inspect.getsource(conversations_mod._evidence_block)
    assert 'prov == "documentation_date"' in src
    assert "documented" in src


# ---------------------------------------------------------------------------
# 5. Home/Discover still gate on explicit — documentation_date excluded


def test_home_still_gates_explicit_only():
    src = inspect.getsource(home_ai_route)
    assert 'ExtractedFact.date_provenance == "explicit"' in src
    # Must NOT have been broadened to admit documentation_date.
    assert "documentation_date" not in src


def test_discover_still_gates_explicit_only():
    src = inspect.getsource(discover_route)
    assert src.count('ExtractedFact.date_provenance == "explicit"') >= 3
    assert "documentation_date" not in src


# ---------------------------------------------------------------------------
# 6. Forward-only unification — the parallel date_origin write is gone


def test_clinical_note_no_longer_writes_date_origin():
    """The `date_provenance` COLUMN is authoritative; the LLM extractor
    must not also stamp the old coded_concepts.date_origin vocabulary
    (no dual-write / compat shim)."""
    src = inspect.getsource(clinical_note_mod)
    # The only historical writer was `cc.setdefault("date_origin", ...)`.
    # Checking the write pattern (not any mention) so explanatory
    # comments about the retired vocabulary don't trip the pin.
    assert 'setdefault("date_origin"' not in src, (
        "clinical_note.py must stop writing coded_concepts.date_origin; "
        "the date_provenance COLUMN is the single source of truth."
    )
    assert 'cc["date_origin"]' not in src
    # And it must set the column instead.
    assert "date_provenance=date_provenance" in src


# ---------------------------------------------------------------------------
# 7. CCDA + vision stamp the column


def test_ccda_stamps_explicit_when_dated():
    src = inspect.getsource(sources_route)
    assert 'date_provenance="explicit" if cc.date_start is not None else None' in src


def test_vision_stamps_explicit_when_dated():
    src = inspect.getsource(vision_mod)
    assert (
        'date_provenance="explicit" if kwargs.get("date_start") is not None else None'
        in src
    )
