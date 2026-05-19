"""Export routes (M02 Slice 4).

Five endpoints, all under /api/exports/*, all wired through the
Slice 1 perimeter (AuthContext + require_role + person_record_id
stamping + cross-record 404):

  POST   /api/exports                 caregiver+ — request a new export
  GET    /api/exports                 viewer+    — list active exports
  GET    /api/exports/{id}            viewer+    — one export's metadata
  GET    /api/exports/{id}/download   viewer+    — stream a rendered file
  DELETE /api/exports/{id}            caregiver+ — soft-delete (deleted_at)

PM revised Group-C: owner/caregiver only on writes; viewer can list
+ download (their own record's exports). The role split mirrors
Slice 3's calendar split.

The skeleton runs the export INLINE from the POST handler — a real
arq enqueue lands in M03 wiring. The runner function signature is
already arq-serializable (db session + job_id), so this is a
one-line swap when scheduling is wired.

Five audit events emitted across the lifecycle:
  POST /api/exports               → EXPORT_REQUESTED (always)
                                    + EXPORT_COMPLETED or
                                    EXPORT_FAILED (on inline run)
  GET  /api/exports/{id}/download → EXPORT_DOWNLOADED
  DELETE /api/exports/{id}        → EXPORT_DELETED
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth_context import AuthContext, require_role
from ..core.config import get_settings
from ..core.db import get_session
from ..exports import (
    EXPORT_COMPLETED,
    EXPORT_DELETED,
    EXPORT_DOWNLOADED,
    EXPORT_FAILED,
    EXPORT_REQUESTED,
)
from ..exports.audit import EXPORT_SUBJECT_TYPE
from ..exports.runner import (
    delete_job_files_on_disk,
    resolve_file_path_for_download,
    run_export_job,
)
from ..models.audit_event import AuditEvent
from ..models.export_file import ExportFile
from ..models.export_job import REQUESTED_FORMATS, ExportJob

router = APIRouter()
log = logging.getLogger("ownchart.routes.exports")


# ---------------------------------------------------------------------------
# IO shapes


class CreateExportRequest(BaseModel):
    requested_format: Literal["ownchart_json", "txt", "all"] = "all"


class ExportFileOut(BaseModel):
    id: str
    file_type: str
    byte_size: int | None
    sha256: str | None


class ExportJobOut(BaseModel):
    id: str
    requested_format: str
    status: str
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    expires_at: datetime | None
    error_message: str | None
    files: list[ExportFileOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers


def _job_to_out(
    job: ExportJob, files: list[ExportFile],
) -> ExportJobOut:
    return ExportJobOut(
        id=str(job.id),
        requested_format=job.requested_format,
        status=job.status,
        requested_at=job.requested_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        failed_at=job.failed_at,
        expires_at=job.expires_at,
        error_message=job.error_message,
        files=[
            ExportFileOut(
                id=str(f.id),
                file_type=f.file_type,
                byte_size=f.byte_size,
                sha256=f.sha256,
            )
            for f in files
        ],
    )


async def _emit_audit(
    db: AsyncSession,
    *,
    event_type: str,
    user_id: uuid.UUID,
    person_record_id: uuid.UUID,
    subject_id: uuid.UUID,
    detail: dict | None = None,
    request: Request | None = None,
) -> None:
    """Append-only AuditEvent insert. The export lifecycle emits five
    distinct event_types via this helper so the audit query layer
    has a uniform shape to filter on."""
    db.add(AuditEvent(
        id=uuid.uuid4(),
        user_id=user_id,
        person_record_id=person_record_id,
        event_type=event_type,
        subject_type=EXPORT_SUBJECT_TYPE,
        subject_id=str(subject_id),
        detail=detail or {},
        ip=(request.client.host if request and request.client else None),
        user_agent=(
            request.headers.get("user-agent") if request else None
        ),
        created_at=datetime.now(timezone.utc),
    ))


# ---------------------------------------------------------------------------
# POST /api/exports — create + (skeleton) run inline


@router.post(
    "",
    response_model=ExportJobOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_export(
    body: CreateExportRequest,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> ExportJobOut:
    """Request a new export.

    The skeleton runs the export INLINE — for any realistic dataset
    this will block the HTTP request for a beat. M03 wiring swaps
    in an arq enqueue + returns 202 with status=pending; the runner
    function signature is already arq-serializable.

    Three audit events emit per call:
      EXPORT_REQUESTED  — always
      EXPORT_COMPLETED — on successful inline run
      EXPORT_FAILED    — on runner exception (runner re-raises;
                          route catches, transitions job to failed,
                          surfaces 500 with bounded detail)
    """
    now = datetime.now(timezone.utc)
    job = ExportJob(
        id=uuid.uuid4(),
        person_record_id=ctx.active_record_id,
        user_id=ctx.user.id,
        requested_at=now,
        requested_format=body.requested_format,
        status="pending",
    )
    db.add(job)
    await db.flush()  # establish job.id

    await _emit_audit(
        db,
        event_type=EXPORT_REQUESTED,
        user_id=ctx.user.id,
        person_record_id=ctx.active_record_id,
        subject_id=job.id,
        detail={"requested_format": body.requested_format},
        request=request,
    )

    settings = get_settings()
    try:
        await run_export_job(
            db, job_id=job.id, data_dir=Path(settings.data_dir),
        )
    except Exception:  # noqa: BLE001
        # The runner already transitioned the job to ``failed`` and
        # set error_message before re-raising. Surface a clean 500
        # with bounded detail (no PHI in the message).
        await _emit_audit(
            db,
            event_type=EXPORT_FAILED,
            user_id=ctx.user.id,
            person_record_id=ctx.active_record_id,
            subject_id=job.id,
            detail={"status": "failed"},
            request=request,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Export failed; see job error_message",
        )

    await _emit_audit(
        db,
        event_type=EXPORT_COMPLETED,
        user_id=ctx.user.id,
        person_record_id=ctx.active_record_id,
        subject_id=job.id,
        detail={"requested_format": body.requested_format},
        request=request,
    )

    # Reload with files for the response.
    files = (await db.execute(
        select(ExportFile)
        .where(ExportFile.export_job_id == job.id)
        .where(ExportFile.person_record_id == ctx.active_record_id)
    )).scalars().all()
    await db.commit()
    log.info(
        "export_completed",
        extra={
            "export_job_id": str(job.id),
            "requested_format": body.requested_format,
            "person_record_id": str(ctx.active_record_id),
            "file_count": len(files),
        },
    )
    return _job_to_out(job, list(files))


# ---------------------------------------------------------------------------
# GET /api/exports


@router.get("", response_model=list[ExportJobOut])
async def list_exports(
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> list[ExportJobOut]:
    """List the active (non-deleted) exports for the active record,
    most-recent first."""
    jobs = (await db.execute(
        select(ExportJob)
        .where(ExportJob.person_record_id == ctx.active_record_id)
        .where(ExportJob.deleted_at.is_(None))
        .order_by(ExportJob.requested_at.desc())
    )).scalars().all()
    if not jobs:
        return []
    job_ids = [j.id for j in jobs]
    files = (await db.execute(
        select(ExportFile)
        .where(ExportFile.export_job_id.in_(job_ids))
        .where(ExportFile.person_record_id == ctx.active_record_id)
    )).scalars().all()
    by_job: dict[uuid.UUID, list[ExportFile]] = {}
    for f in files:
        by_job.setdefault(f.export_job_id, []).append(f)
    return [_job_to_out(j, by_job.get(j.id, [])) for j in jobs]


# ---------------------------------------------------------------------------
# GET /api/exports/{id}


@router.get("/{export_id}", response_model=ExportJobOut)
async def get_export(
    export_id: uuid.UUID,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> ExportJobOut:
    job = (await db.execute(
        select(ExportJob)
        .where(ExportJob.id == export_id)
        .where(ExportJob.person_record_id == ctx.active_record_id)
        .where(ExportJob.deleted_at.is_(None))
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    files = (await db.execute(
        select(ExportFile)
        .where(ExportFile.export_job_id == job.id)
        .where(ExportFile.person_record_id == ctx.active_record_id)
    )).scalars().all()
    return _job_to_out(job, list(files))


# ---------------------------------------------------------------------------
# GET /api/exports/{id}/download


@router.get("/{export_id}/download")
async def download_export(
    export_id: uuid.UUID,
    request: Request,
    file_type: Literal["ownchart_json", "txt"] = Query(...),
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
):
    """Stream one rendered file. Emits EXPORT_DOWNLOADED audit event
    on success.

    Five rejection paths (Slice 4 hardening per PM 2026-05-19):
      404 — cross-record probe, missing row, or soft-deleted.
      409 — job not yet completed (still pending or running).
      410 — past expires_at (TTL elapsed; purge worker hasn't run
            yet but the file is conceptually gone).
      404 — requested file_type not produced by this job.
      410 — file on disk missing (purged early or never written).
    """
    job = (await db.execute(
        select(ExportJob)
        .where(ExportJob.id == export_id)
        .where(ExportJob.person_record_id == ctx.active_record_id)
        .where(ExportJob.deleted_at.is_(None))
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if job.status != "completed":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"export status is {job.status!r}; not ready for download",
        )
    # Expired-but-not-yet-purged: the purge worker runs hourly, so
    # there's a window between expires_at < now() and the worker's
    # next pass where the row is technically still here. Refuse the
    # download — the user-visible contract is "72 hours then gone."
    if job.expires_at is not None and job.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_410_GONE,
            detail="export has expired (past 72-hour TTL)",
        )

    file_row = (await db.execute(
        select(ExportFile)
        .where(ExportFile.export_job_id == job.id)
        .where(ExportFile.person_record_id == ctx.active_record_id)
        .where(ExportFile.file_type == file_type)
    )).scalar_one_or_none()
    if file_row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"file_type {file_type!r} not produced for this export",
        )

    settings = get_settings()
    path = resolve_file_path_for_download(
        data_dir=Path(settings.data_dir),
        job_id=job.id,
        file_type=file_type,
    )
    if not path.exists():
        # File on disk gone (purged early?) — surface 410 so the
        # client knows the metadata still exists but the bytes don't.
        raise HTTPException(
            status.HTTP_410_GONE,
            detail="export file no longer on disk",
        )

    media_type = (
        "application/json" if file_type == "ownchart_json" else "text/plain"
    )
    filename_for_user = (
        f"ownchart-{job.id}.{'json' if file_type == 'ownchart_json' else 'txt'}"
    )

    await _emit_audit(
        db,
        event_type=EXPORT_DOWNLOADED,
        user_id=ctx.user.id,
        person_record_id=ctx.active_record_id,
        subject_id=job.id,
        detail={"file_type": file_type},
        request=request,
    )
    await db.commit()

    return FileResponse(
        path=path,
        media_type=media_type,
        filename=filename_for_user,
    )


# ---------------------------------------------------------------------------
# DELETE /api/exports/{id}


@router.delete("/{export_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_export(
    export_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Soft-delete an export.

    Hard delete (DB row + on-disk files removed) happens via the
    72h purge worker (PM C-6); soft-delete just hides the export
    from the UI and starts the on-disk cleanup. The audit row stays
    forever — it's append-only.
    """
    job = (await db.execute(
        select(ExportJob)
        .where(ExportJob.id == export_id)
        .where(ExportJob.person_record_id == ctx.active_record_id)
        .where(ExportJob.deleted_at.is_(None))
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    now = datetime.now(timezone.utc)
    await db.execute(
        update(ExportJob)
        .where(ExportJob.id == job.id)
        .values(deleted_at=now, updated_at=now)
    )

    # Clean the on-disk directory up-front so the file bytes are
    # gone immediately at the user-visible delete (the DB row stays
    # for the audit window; the purge worker removes it after TTL).
    settings = get_settings()
    delete_job_files_on_disk(
        data_dir=Path(settings.data_dir), job_id=job.id,
    )

    await _emit_audit(
        db,
        event_type=EXPORT_DELETED,
        user_id=ctx.user.id,
        person_record_id=ctx.active_record_id,
        subject_id=job.id,
        detail={},
        request=request,
    )
    await db.commit()
