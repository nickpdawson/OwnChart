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
    """Hard-delete expired completed exports AND sweep failed jobs'
    partial files. Returns the number of jobs cleaned.

    Two classes of on-disk garbage accrue forever without this:

      A. **Completed + expired** jobs (``now() > expires_at``). Full-
         record PHI snapshots. Per row: remove files
         (``delete_job_files_on_disk``) → emit an ``EXPORT_EXPIRED``
         AuditEvent (user_id=NULL) → hard-delete the ExportJob row
         (cascade removes ExportFile children via FK). Files go FIRST so
         a later DB error can't strand bytes with no metadata anchor.

      B. **Failed** jobs. A run that dies mid-render leaves partial
         files in ``<data_dir>/exports/<job_id>/`` and NEVER gets an
         ``expires_at`` (that's only set at completion), so class A
         would never reach them. We remove the partial files but KEEP
         the row — the user still sees the failure, and the bounded,
         PHI-free ``error_message`` is all that remains. An
         ``EXPORT_EXPIRED`` audit (``job_status='failed'``) is emitted
         only when bytes were actually removed, so repeated hourly runs
         don't spam the audit log once a job's dir is already clean.

    ``data_dir`` defaults to None for callers that only want the class-A
    SQL + audit shape (unit tests). Production callers pass
    ``settings.data_dir`` so both on-disk sweeps run.

    Intended to run periodically (PM C-6, hourly via the arq
    ``WorkerSettings.cron_jobs``). Local imports keep this module
    importable in pure-function tests that never wire a DB.
    """
    from sqlalchemy import delete, select

    from ..models.audit_event import AuditEvent
    from ..models.export_job import ExportJob
    from .audit import EXPORT_EXPIRED, EXPORT_SUBJECT_TYPE
    from .runner import delete_job_files_on_disk

    cutoff = now or datetime.now(timezone.utc)

    # --- Class A: completed + expired -----------------------------------
    expiring = (await db.execute(
        select(ExportJob).where(
            ExportJob.status == "completed",
            ExportJob.expires_at.is_not(None),
            ExportJob.expires_at < cutoff,
        )
    )).scalars().all()

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
                "job_status": "completed",
            },
            ip=None,
            user_agent=None,
            created_at=cutoff,
        ))

    if expiring:
        job_ids = [j.id for j in expiring]
        await db.execute(
            delete(ExportJob).where(ExportJob.id.in_(job_ids))
        )

    # --- Class B: failed jobs' partial files ----------------------------
    failed_files_cleaned = 0
    if data_dir is not None:
        failed = (await db.execute(
            select(ExportJob).where(ExportJob.status == "failed")
        )).scalars().all()
        for job in failed:
            try:
                removed = delete_job_files_on_disk(
                    data_dir=Path(data_dir), job_id=job.id,
                )
            except Exception:  # noqa: BLE001
                removed = 0
            if removed > 0:
                failed_files_cleaned += 1
                db.add(AuditEvent(
                    id=uuid.uuid4(),
                    user_id=None,  # system-attributed (purge worker)
                    person_record_id=job.person_record_id,
                    event_type=EXPORT_EXPIRED,
                    subject_type=EXPORT_SUBJECT_TYPE,
                    subject_id=str(job.id),
                    detail={
                        "expired_at": cutoff.isoformat(),
                        "requested_format": job.requested_format,
                        "job_status": "failed",
                        "files_removed": removed,
                    },
                    ip=None,
                    user_agent=None,
                    created_at=cutoff,
                ))

    return len(expiring) + failed_files_cleaned
