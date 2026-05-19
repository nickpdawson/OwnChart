"""Slice 4 export 72-hour TTL helpers (PM C-6).

``expires_at`` is set when an export job transitions to ``completed``
(or, defensively, when a job is soft-deleted via DELETE — the TTL
clock keeps running for the file-on-disk regardless of UI state).

A periodic purge worker (defined here as a pure-ish helper; arq
scheduling is a separate wiring concern outside Slice 4 scope)
hard-deletes:
  - the on-disk file pointed at by ``storage_uri``
  - the ``export_files`` row(s) (via FK ON DELETE CASCADE from
    ``export_jobs``)
  - the ``export_jobs`` row itself

once ``now() > expires_at``.

The audit row (``EXPORT_DELETED``) is created BEFORE the hard
delete so the immutable log survives the row removal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

EXPORT_TTL_HOURS = 72


def compute_export_expiry(
    *, completed_at: datetime | None = None,
) -> datetime:
    """Compute when an export job's files become eligible for hard
    delete. Defaults to ``now + 72h`` when ``completed_at`` is None;
    callers should pass the actual completion timestamp once the
    worker finishes the snapshot + mapper pass.

    Pure function; ``now`` injectable for tests via the
    ``completed_at`` argument.
    """
    base = completed_at or datetime.now(timezone.utc)
    return base + timedelta(hours=EXPORT_TTL_HOURS)


async def purge_expired_exports(
    db: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Hard-delete completed export jobs whose ``expires_at`` has
    passed. Returns count deleted (cascade-removes export_files via
    FK).

    Intended to run periodically (PM C-6, ~hourly). Local imports
    keep this module importable in pure-function tests that never
    wire a DB.
    """
    from sqlalchemy import delete

    from ..models.export_job import ExportJob

    cutoff = now or datetime.now(timezone.utc)
    result = await db.execute(
        delete(ExportJob).where(
            ExportJob.status == "completed",
            ExportJob.expires_at.is_not(None),
            ExportJob.expires_at < cutoff,
        )
    )
    return result.rowcount or 0
