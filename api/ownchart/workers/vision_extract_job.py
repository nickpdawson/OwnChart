"""Background-job worker for Claude vision extraction.

Doctrine: a 6-minute foreground extraction is "spinner of mystery" UX
and tempts double-clicks. The worker pattern fixes both — POST returns
immediately, the worker processes pages serially, commits per-page so
the UI can show real progress, and survives browser disconnects.

The worker is launched as a separate Docker service:

    docker compose run worker
    # or via the compose service: ownchart-worker-1

It connects to the same Postgres + Redis the api uses; it does NOT
import the FastAPI app (no HTTP, no auth — it operates by job_id and
trusts the row already passed the consent gate when the api enqueued).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings
from sqlalchemy import select

from ..core.config import get_settings
from ..core.db import SessionLocal
from ..core.logger import get_logger
from ..extract import vision as vision_extract
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.extraction_job import ExtractionJob
from ..models.source_document import SourceDocument
from ..models.user import User

log = get_logger("ownchart.workers.vision")


async def extract_pages_task(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Run vision extraction for one ExtractionJob.

    Idempotent enough: if the job is already 'completed' / 'failed',
    returns its current state without re-running. Reads only_pages /
    patient_context from the row.
    """
    job_uuid = uuid.UUID(job_id)
    async with SessionLocal() as db:
        job = await db.get(ExtractionJob, job_uuid)
        if job is None:
            log.warning("vision_job_not_found", job_id=job_id)
            return {"error": "job not found"}
        if job.status in ("completed", "failed", "cancelled"):
            log.info("vision_job_already_terminal", job_id=job_id, status=job.status)
            return {"status": job.status}

        source = await db.get(SourceDocument, job.source_document_id)
        user = await db.get(User, job.user_id)
        if source is None or user is None:
            job.status = "failed"
            job.error = "source or user missing"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"status": "failed"}

        # Discover the pages to process from the source's render manifest.
        page_renders = (source.raw_metadata or {}).get("page_renders", []) or []
        page_entries: list[dict[str, Any]] = []
        for entry in page_renders:
            try:
                pn = int(entry.get("page", 0))
            except (TypeError, ValueError):
                continue
            if job.only_pages and pn not in job.only_pages:
                continue
            page_entries.append({"page": pn, "image_path": entry.get("image_path")})

        # Look up the per-page anchors (one PDF render anchor per page,
        # written by the upload pipeline).
        anc_q = await db.execute(
            select(EvidenceAnchor)
            .where(EvidenceAnchor.source_document_id == source.id)
            .where(EvidenceAnchor.anchor_type == "pdf_page")
        )
        by_page: dict[int, EvidenceAnchor] = {}
        for a in anc_q.scalars().all():
            if a.page_number is not None:
                by_page[a.page_number] = a

        # Idempotency (#42): skip pages that already have at least one
        # claude_vision_v1 fact. Lets a re-enqueued job — orphan
        # recovery on worker startup, or manual retry — only do the
        # missing work without writing duplicates.
        #
        # Vision extraction writes one EvidenceAnchor per fact (with
        # the per-fact excerpt for "why do you think that?"), in
        # addition to the one base pdf_page anchor per page from the
        # PDF render. So the same page is referenced via many anchors;
        # we have to consider all of them, not just by_page's one.
        src_anchor_q = await db.execute(
            select(EvidenceAnchor.id, EvidenceAnchor.page_number)
            .where(EvidenceAnchor.source_document_id == source.id)
            .where(EvidenceAnchor.page_number.isnot(None))
        )
        anchor_to_page: dict[uuid.UUID, int] = {
            aid: pn for (aid, pn) in src_anchor_q.all()
        }
        already_done: set[int] = set()
        if anchor_to_page:
            all_source_anchor_ids = list(anchor_to_page.keys())
            fact_q = await db.execute(
                select(ExtractedFact.evidence_anchor_ids)
                .where(ExtractedFact.extraction_method == "claude_vision_v1")
                .where(ExtractedFact.evidence_anchor_ids.op("&&")(all_source_anchor_ids))
            )
            for (arr,) in fact_q.all():
                for aid in (arr or []):
                    pn = anchor_to_page.get(aid)
                    if pn is not None:
                        already_done.add(pn)
        if already_done:
            before = len(page_entries)
            page_entries = [e for e in page_entries if e["page"] not in already_done]
            skipped = before - len(page_entries)
            if skipped:
                log.info(
                    "vision_skipping_already_extracted",
                    source_id=str(source.id),
                    skipped_pages=skipped,
                    remaining=len(page_entries),
                )

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.total_pages = len(page_entries)
        await db.commit()

    log.info("vision_job_started", job_id=job_id, total_pages=len(page_entries))

    page_errors: list[dict[str, Any]] = []
    facts_added = 0

    for entry in page_entries:
        page_n = entry["page"]
        image_path = entry["image_path"]

        async with SessionLocal() as db:
            source = await db.get(SourceDocument, job.source_document_id)
            user = await db.get(User, job.user_id)
            anchor = by_page.get(page_n)
            if anchor is None or source is None or user is None:
                page_errors.append({"page": page_n, "error": "page anchor missing"})
                continue
            anchor = await db.merge(anchor)
            try:
                res = await vision_extract.extract_page(
                    db, user, source,
                    page_number=page_n,
                    image_path=Path(str(image_path)),
                    page_anchor=anchor,
                    patient_context=job.patient_context,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "vision_page_failed",
                    job_id=job_id,
                    source_id=str(job.source_document_id),
                    page=page_n,
                    error=str(e),
                )
                page_errors.append({"page": page_n, "error": str(e)})
                # Refresh job and increment completed_pages
                live = await db.get(ExtractionJob, job_uuid)
                if live is not None:
                    live.completed_pages += 1
                    live.page_errors = list(page_errors)
                    await db.commit()
                continue

            if res.error:
                page_errors.append({"page": page_n, "error": res.error})
            facts_added += res.fact_count

            live = await db.get(ExtractionJob, job_uuid)
            if live is not None:
                live.completed_pages += 1
                live.facts_added = facts_added
                live.page_errors = list(page_errors)
                await db.commit()

    async with SessionLocal() as db:
        live = await db.get(ExtractionJob, job_uuid)
        if live is None:
            return {"status": "missing"}
        live.status = "completed"
        live.completed_at = datetime.now(timezone.utc)
        live.facts_added = facts_added
        live.page_errors = list(page_errors)
        await db.commit()

    log.info(
        "vision_job_completed",
        job_id=job_id,
        facts_added=facts_added,
        page_errors=len(page_errors),
    )
    return {"status": "completed", "facts_added": facts_added}


from .auto_export_job import process_auto_export_push  # noqa: E402


async def _reenqueue_stranded_jobs(ctx: dict[str, Any]) -> None:
    """On worker startup, re-queue extraction jobs the previous worker
    didn't finish.

    A vision job is "stranded" if its DB row says status='running' or
    status='pending' but no Arq worker is currently processing it.
    Causes we've actually hit: worker container rebuilt mid-extraction
    (#42 trigger), Arq's old default 300s job_timeout cancelled the
    task before completion. Without recovery, the job sits at
    "running" forever and the partial unique index on
    `(source_document_id) WHERE status IN ('pending','running')`
    blocks any retry.

    The extract_pages_task has page-level idempotency (skips pages
    that already have facts), so re-enqueuing is safe — the worker
    only does the missing pages.
    """
    pool = ctx.get("redis")
    if pool is None:
        log.warning("orphan_recovery_skipped_no_redis_pool")
        return
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(ExtractionJob)
            .where(ExtractionJob.status.in_(("pending", "running")))
            .where(ExtractionJob.completed_at.is_(None))
        )).scalars().all()
        if not rows:
            log.info("orphan_recovery_clean")
            return
        for job in rows:
            try:
                await pool.enqueue_job("extract_pages_task", str(job.id))
                log.info(
                    "orphan_recovery_reenqueued",
                    job_id=str(job.id),
                    source_id=str(job.source_document_id),
                    status_was=job.status,
                    completed_pages=job.completed_pages,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "orphan_recovery_failed",
                    job_id=str(job.id),
                    error=f"{type(e).__name__}: {e}",
                )


class WorkerSettings:
    """Arq config — `arq ownchart.workers.vision_extract_job.WorkerSettings`.

    Single worker container handles both vision extraction and Auto
    Export push processing. Different concurrency profiles are
    irrelevant in V1; if Auto Export pushes ever back up the queue
    behind a long vision extraction we'll split workers later.
    """

    functions = [extract_pages_task, process_auto_export_push]
    on_startup = _reenqueue_stranded_jobs
    max_jobs = 2  # one extraction is plenty heavy; cap concurrency
    # Default Arq job_timeout is 300s, which is comically short for our
    # use case — a 100-page PDF at ~30s/page takes ~50 min. Bump to
    # 4 hours to cover any reasonable PDF + Anthropic latency spike.
    # (Found the hard way when a 125-page Alpine PDF got cancelled
    # mid-run on the default timeout.)
    job_timeout = 14400  # seconds — 4h
    keep_result = 3600   # keep job results in Redis for 1h after success
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
