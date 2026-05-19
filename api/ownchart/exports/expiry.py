"""Slice 4 export 72-hour TTL helpers (PM C-6, hardened 2026-05-19).

``expires_at`` is set when an export job transitions to ``completed``.
A periodic purge worker (defined here as an async helper; arq
scheduling is a separate wiring concern outside Slice 4 scope)
hard-deletes:

  1. The on-disk files under ``<data_dir>/exports/<job_id>/``
     (via the runner's ``delete_job_files_on_disk`` helper). Done
     FIRST so a row-delete failure mid-loop doesn't leave bytes
     pointing at a row that no longer exists.
  2. An immutable ``AuditEvent`` row with ``event_type=export_expired``
     and ``user_id=NULL`` (system-attributed). Records WHEN the file
     became unreachable, not just when the user requested deletion.
  3. The ``export_jobs`` row itself (which cascade-removes
     ``export_files`` via FK ON DELETE CASCADE).

once ``now() > expires_at``. Per-row loop — the old single-shot
``delete().where()`` couldn't satisfy (1) and (2) because it never
observed which rows were going to die.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    data_dir: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Hard-delete completed export jobs whose ``expires_at`` has
    passed. Returns count deleted.

    Per-row loop with three steps per row:
      1. Remove on-disk files (``delete_job_files_on_disk``). Files
         go FIRST so a later DB error doesn't strand bytes that no
         longer have a metadata anchor. Tolerant of FS errors —
         missing directory / partial cleanup is OK; the audit row
         still records the attempt.
      2. Emit ``EXPORT_EXPIRED`` AuditEvent (user_id=NULL).
      3. Hard-delete the ExportJob row (cascade removes ExportFile
         children via FK).

    ``data_dir`` defaults to None for callers that don't want the
    FS sweep — e.g. unit tests that just want to verify the SQL +
    audit shape. Production callers pass ``settings.data_dir`` so
    the on-disk cleanup actually runs.

    Intended to run periodically (PM C-6, ~hourly). Local imports
    keep this module importable in pure-function tests that never
    wire a DB.
    """
    from sqlalchemy import delete, select

    from ..models.audit_event import AuditEvent
    from ..models.export_job import ExportJob
    from .audit import EXPORT_EXPIRED, EXPORT_SUBJECT_TYPE
    from .runner import delete_job_files_on_disk

    cutoff = now or datetime.now(timezone.utc)
    expiring = (await db.execute(
        select(ExportJob).where(
            ExportJob.status == "completed",
            ExportJob.expires_at.is_not(None),
            ExportJob.expires_at < cutoff,
        )
    )).scalars().all()

    if not expiring:
        return 0

    for job in expiring:
        if data_dir is not None:
            try:
                delete_job_files_on_disk(
                    data_dir=Path(data_dir), job_id=job.id,
                )
            except Exception:  # noqa: BLE001
                # Tolerate FS errors during purge — the audit still
                # records the attempt; an operator can clean up by
                # hand if needed. We never let an FS issue block the
                # DB row delete (otherwise rows pile up forever).
                pass
        db.add(AuditEvent(
            id=uuid.uuid4(),
            user_id=None,  # system-attributed (purge worker)
            person_record_id=job.person_record_id,
            event_type=EXPORT_EXPIRED,
            subject_type=EXPORT_SUBJECT_TYPE,
            subject_id=str(job.id),
            detail={
                "expires_at": (
                    job.expires_at.isoformat() if job.expires_at else None
                ),
                "expired_at": cutoff.isoformat(),
                "requested_format": job.requested_format,
            },
            ip=None,
            user_agent=None,
            created_at=cutoff,
        ))

    job_ids = [j.id for j in expiring]
    result = await db.execute(
        delete(ExportJob).where(ExportJob.id.in_(job_ids))
    )
    return result.rowcount or 0
