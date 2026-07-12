"""Clinical-note extraction robustness (HIGH-2).

Two guards:
  - The failure handler must `rollback()` before touching the session
    so a DB error inside the extractor still stamps
    extraction_status='failed' + an AuditEvent (instead of raising
    PendingRollbackError and recording nothing).
  - The startup re-scan predicate correctly identifies notes stranded
    mid-extraction (plaintext present, no terminal status).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from ownchart.routes import connectors


# ---------------------------------------------------------------------------
# Stranded-note predicate (pure)


def test_needs_extraction_true_when_plaintext_and_no_status():
    rm = {"has_plaintext": True, "plaintext_length": 800}
    assert connectors._clinical_note_needs_extraction("clinical_note", rm) is True
    assert connectors._clinical_note_needs_extraction("ccda_xml", rm) is True


def test_needs_extraction_false_when_completed_or_failed():
    for st in ("completed", "failed", "running"):
        rm = {"has_plaintext": True, "plaintext_length": 800, "extraction_status": st}
        assert connectors._clinical_note_needs_extraction("clinical_note", rm) is False


def test_needs_extraction_false_when_no_or_short_plaintext():
    assert connectors._clinical_note_needs_extraction(
        "clinical_note", {"has_plaintext": False},
    ) is False
    assert connectors._clinical_note_needs_extraction(
        "clinical_note", {"has_plaintext": True, "plaintext_length": 10},
    ) is False
    assert connectors._clinical_note_needs_extraction("clinical_note", None) is False


def test_needs_extraction_false_for_other_source_types():
    rm = {"has_plaintext": True, "plaintext_length": 800}
    assert connectors._clinical_note_needs_extraction("pdf", rm) is False
    assert connectors._clinical_note_needs_extraction("photo", rm) is False


# ---------------------------------------------------------------------------
# Failure handler rolls back and records 'failed' instead of raising


class _FakeDB:
    def __init__(self, src, usr):
        self._objs = [src, usr, src]  # get(src), get(usr), get(src2)
        self.calls: list[str] = []
        self.added: list[object] = []

    async def get(self, _model, _pk):
        self.calls.append("get")
        return self._objs.pop(0)

    async def rollback(self):
        self.calls.append("rollback")

    async def commit(self):
        self.calls.append("commit")

    def add(self, obj):
        self.added.append(obj)


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_failed_extraction_records_failed_status(monkeypatch):
    src = SimpleNamespace(raw_metadata={"has_plaintext": True})
    usr = SimpleNamespace(id=uuid.uuid4())
    db = _FakeDB(src, usr)

    import ownchart.core.db as core_db
    import ownchart.extract.clinical_note as cn

    monkeypatch.setattr(core_db, "SessionLocal", lambda: _FakeSessionCtx(db))

    async def _boom(_db, _usr, _src):
        # Simulate a DB error inside the extractor — the session would be
        # left in pending-rollback state in production.
        raise RuntimeError("simulated DB error during extraction")

    monkeypatch.setattr(cn, "extract_clinical_note", _boom)

    # Must NOT raise.
    await connectors._extract_clinical_note_in_background(
        source_id=uuid.uuid4(), user_id=usr.id,
    )

    # rollback happened before the second get (the src2 refetch).
    assert "rollback" in db.calls
    assert db.calls.index("rollback") < db.calls.index("get", 2)
    # failed status stamped + committed.
    assert src.raw_metadata["extraction_status"] == "failed"
    assert "commit" in db.calls
    # an AuditEvent was added.
    assert any(type(o).__name__ == "AuditEvent" for o in db.added)
