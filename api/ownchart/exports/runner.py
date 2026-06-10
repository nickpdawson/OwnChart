"""Slice 4 export runner — the build → map → persist pipeline.

A single async function that takes an ExportJob and runs:
  1. Build the snapshot (record-scoped, read-only).
  2. Render mapper output(s) per ``job.requested_format``.
  3. Write files to ``<data_dir>/exports/<job_id>/<filename>``.
  4. Create ExportFile rows with byte_size + sha256.
  5. Transition job to ``completed`` + set ``expires_at``.

On failure: job transitions to ``failed`` with error_message; no
ExportFile rows written. The caller (route layer for the skeleton,
arq worker in a later wiring) handles the AuditEvent insert — runner
is pure pipeline.

The skeleton runs INLINE from the POST handler. Future wiring can
enqueue this same function via arq without changing the contract;
the function signature already takes a session + job id, both arq-
serializable.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .expiry import compute_export_expiry
from .mappers import (
    canonical_ownchart_json_mapper,
    human_readable_txt_mapper,
    pictal_health_json_mapper,
)
from .snapshot import build_export_snapshot

log = logging.getLogger("ownchart.exports.runner")


_FILENAME_FOR_TYPE: dict[str, str] = {
    "ownchart_json": "ownchart_json.json",
    "txt": "packet.txt",
    "pictal_json": "pictal_health.json",
}


def _exports_root(data_dir: Path) -> Path:
    return Path(data_dir) / "exports"


def _job_dir(data_dir: Path, job_id: uuid.UUID) -> Path:
    return _exports_root(data_dir) / str(job_id)


async def run_export_job(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    data_dir: Path,
    now: datetime | None = None,
) -> None:
    """Execute one export job end-to-end. Mutates the job row to
    reflect lifecycle (running → completed / failed) and writes
    ExportFile rows on success.

    Idempotent on retry: if the job is already ``completed`` or
    ``failed``, returns without re-running. Re-issuing a failed
    job requires explicit status reset (out of scope for Slice 4).
    """
    from ..models.export_file import ExportFile
    from ..models.export_job import ExportJob

    job = (await db.execute(
        select(ExportJob).where(ExportJob.id == job_id)
    )).scalar_one()

    if job.status in ("completed", "failed"):
        log.info("export_run_skipped_terminal status=%s job=%s",
                 job.status, job_id)
        return

    now_dt = now or datetime.now(timezone.utc)
    job.status = "running"
    job.started_at = now_dt
    await db.flush()

    try:
        snapshot = await build_export_snapshot(
            db,
            person_record_id=job.person_record_id,
            now=now_dt,
            filters=job.filters,
        )

        out_dir = _job_dir(data_dir, job.id)
        out_dir.mkdir(parents=True, exist_ok=True)

        types_to_render: list[str]
        if job.requested_format == "all":
            types_to_render = ["ownchart_json", "txt"]
        else:
            types_to_render = [job.requested_format]

        for file_type in types_to_render:
            if file_type == "ownchart_json":
                payload = canonical_ownchart_json_mapper(snapshot)
            elif file_type == "txt":
                payload = human_readable_txt_mapper(snapshot)
            elif file_type == "pictal_json":
                payload = pictal_health_json_mapper(snapshot)
            else:
                raise ValueError(f"unknown file_type {file_type!r}")

            filename = _FILENAME_FOR_TYPE[file_type]
            file_path = out_dir / filename
            file_path.write_bytes(payload)

            db.add(ExportFile(
                id=uuid.uuid4(),
                export_job_id=job.id,
                person_record_id=job.person_record_id,
                file_type=file_type,
                storage_uri=f"file://{file_path}",
                byte_size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ))

        completion = datetime.now(timezone.utc) if now is None else now
        job.status = "completed"
        job.completed_at = completion
        job.expires_at = compute_export_expiry(completed_at=completion)
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        failed_at = datetime.now(timezone.utc) if now is None else now
        job.status = "failed"
        job.failed_at = failed_at
        # Keep the message bounded — never leak unbounded PHI text.
        job.error_message = f"{type(exc).__name__}: {exc}"[:512]
        await db.flush()
        log.warning(
            "export_run_failed job=%s exc_type=%s",
            job_id, type(exc).__name__, exc_info=True,
        )
        raise


def resolve_file_path_for_download(
    *, data_dir: Path, job_id: uuid.UUID, file_type: str,
) -> Path:
    """Compute the on-disk path the route's download handler streams
    from. Pure helper so the route doesn't reach into the runner's
    private layout. Raises KeyError on unknown file_type."""
    return _job_dir(data_dir, job_id) / _FILENAME_FOR_TYPE[file_type]


def delete_job_files_on_disk(
    *, data_dir: Path, job_id: uuid.UUID,
) -> int:
    """Remove an export job's on-disk directory and every file
    inside. Returns the number of files unlinked. Used by both the
    DELETE /api/exports/{id} soft-delete (which still leaves the
    DB row around with deleted_at set) and the 72h hard-delete
    worker (which then deletes the DB row).

    Tolerant: missing files / directory are not errors. The function
    is called from teardown paths where partial state can exist.
    """
    job_dir = _job_dir(data_dir, job_id)
    if not job_dir.exists():
        return 0
    unlinked = 0
    for p in job_dir.iterdir():
        if p.is_file():
            try:
                p.unlink()
                unlinked += 1
            except FileNotFoundError:
                pass
    try:
        job_dir.rmdir()
    except OSError:
        # Non-empty (e.g. partial run); leave it for the purge worker.
        pass
    return unlinked


__all__ = [
    "delete_job_files_on_disk",
    "resolve_file_path_for_download",
    "run_export_job",
]
