"""Lazily-constructed Arq Redis pool for enqueueing background jobs.

The api process needs to push jobs to Redis but doesn't run the workers.
A single pool is shared across requests; Arq's `create_pool` is async
and we want to do it once.
"""

from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from .config import get_settings

_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def enqueue_extraction_job(job_id: str) -> str:
    """Enqueue a vision-extraction job. Returns the Arq job id."""
    pool = await get_arq_pool()
    arq_job = await pool.enqueue_job("extract_pages_task", job_id)
    return arq_job.job_id if arq_job is not None else ""


async def enqueue_auto_export_processing(source_id: str) -> str:
    """Enqueue parsing of an Auto Export push that's already on disk."""
    pool = await get_arq_pool()
    arq_job = await pool.enqueue_job("process_auto_export_push", source_id)
    return arq_job.job_id if arq_job is not None else ""


async def enqueue_personal_photo_vision(source_id: str) -> str:
    """Enqueue Claude-vision content extraction for a personal photo
    upload. Worker enriches the photo's life_context_event fact with a
    body-parts/devices/setting description and (when relevance is low)
    flips the fact to source_only so casual photos don't pollute
    clinical retrieval surfaces."""
    pool = await get_arq_pool()
    arq_job = await pool.enqueue_job("process_personal_photo", source_id)
    return arq_job.job_id if arq_job is not None else ""
