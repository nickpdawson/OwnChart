"""HIGH-6: export purge removes expired completed jobs AND failed-job
partial files.

Before the fix, ``purge_expired_exports`` had zero non-test callers and
only ever touched completed+expired jobs. Failed runs left partial
files on disk with no ``expires_at``, so they accrued forever. These
tests drive the purge with a fake DB session + a real temp
``data_dir`` and assert both classes of on-disk garbage are removed.

Pure-ish: a hand-rolled fake session stands in for AsyncSession so the
SQL + FS behavior is exercised without a live Postgres.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ownchart.exports.expiry import purge_expired_exports


class _Result:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Returns canned rows for the completed/failed selects and a
    rowcount for the delete. Records ``add`` calls (audit events)."""

    def __init__(self, completed, failed):
        self._completed = completed
        self._failed = failed
        self.added: list = []

    async def execute(self, stmt):
        text = str(stmt)
        if text.strip().upper().startswith("DELETE"):
            return _Result(rowcount=len(self._completed))
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        if "'failed'" in compiled:
            return _Result(rows=self._failed)
        return _Result(rows=self._completed)

    def add(self, obj):
        self.added.append(obj)


def _job(status: str, *, expires_at=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
        status=status,
        expires_at=expires_at,
        requested_format="ownchart_json",
    )


def _seed_file(data_dir: Path, job_id: uuid.UUID, name: str) -> Path:
    d = data_dir / "exports" / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"synthetic export bytes")
    return p


async def test_purge_removes_expired_completed_and_failed_partials(tmp_path):
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    completed = _job(
        "completed", expires_at=now - timedelta(hours=1),  # already expired
    )
    failed = _job("failed")

    completed_file = _seed_file(tmp_path, completed.id, "ownchart.json")
    failed_file = _seed_file(tmp_path, failed.id, "partial.json")
    assert completed_file.exists() and failed_file.exists()

    db = _FakeSession([completed], [failed])
    cleaned = await purge_expired_exports(db, data_dir=tmp_path, now=now)

    # One completed row + one failed job with removed bytes.
    assert cleaned == 2
    # Both on-disk directories are gone.
    assert not completed_file.exists()
    assert not failed_file.exists()
    # Audit trail: one per completed row, one per failed job cleaned.
    statuses = sorted(a.detail.get("job_status") for a in db.added)
    assert statuses == ["completed", "failed"]


async def test_purge_noop_when_nothing_expired(tmp_path):
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    db = _FakeSession([], [])
    cleaned = await purge_expired_exports(db, data_dir=tmp_path, now=now)
    assert cleaned == 0
    assert db.added == []


async def test_purge_failed_job_with_no_files_not_counted(tmp_path):
    """A failed job whose dir was already cleaned (or never wrote a
    file) isn't re-counted or re-audited — so repeated hourly runs
    don't spam the audit log."""
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    failed = _job("failed")  # no file seeded
    db = _FakeSession([], [failed])
    cleaned = await purge_expired_exports(db, data_dir=tmp_path, now=now)
    assert cleaned == 0
    assert db.added == []
