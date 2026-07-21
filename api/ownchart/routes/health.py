"""Liveness + readiness probes.

``/healthz`` is the trivial liveness probe — the process is up enough to
answer, nothing more. It must never depend on the DB, Redis, or the worker
so an orchestrator can tell "process alive" from "process ready".

``/readyz`` is the honest readiness probe (P1-7). It returns **503** whenever
the instance cannot safely serve a request:

  - the database is unreachable, OR
  - the schema is behind the migration head baked into this image, OR
  - the evidence-data directory is not writable.

Redis and the background worker are *async lanes*: they back the queue and
cron sweeps, not the synchronous request path. Their degradation is REPORTED
in the ``components`` map (so a monitor can see it) but does not, on its own,
fail readiness — a load balancer shouldn't pull a node that still serves
reads/writes just because the worker is momentarily down.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Response
from sqlalchemy import text

from ..core.config import get_settings
from ..core.db import engine
from ..core.logger import get_logger

router = APIRouter()
log = get_logger("ownchart.routes.health")

# Worker liveness. The arq worker refreshes this key once a minute (see
# ``workers/vision_extract_job.py::worker_heartbeat_cron``) with a TTL just
# over the cron interval, so a dead worker's key expires on its own and
# ``/readyz`` reports the worker degraded without any explicit teardown.
WORKER_HEARTBEAT_KEY = "ownchart:worker:heartbeat"
WORKER_HEARTBEAT_TTL_SECONDS = 180

# Components whose degradation means the instance cannot safely serve.
# Redis + worker are async lanes and deliberately excluded — they degrade
# the map without failing readiness.
_CRITICAL_COMPONENTS = ("db", "migrations", "storage")


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness only — no dependencies. 200 means the process answers."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Pure decision core (unit-tested without a DB / Redis / FS)


def overall_readiness(components: dict[str, str]) -> tuple[bool, int]:
    """(ready, http_status) from a component→state map. Pure.

    Ready (200) requires every CRITICAL component == ``"ok"``. A degraded
    critical component ⇒ not ready (503). Async-lane components
    (redis/worker) never fail readiness on their own — a missing critical
    key is treated as not-ok, so an incomplete map fails closed.
    """
    ready = all(components.get(c) == "ok" for c in _CRITICAL_COMPONENTS)
    return ready, (200 if ready else 503)


# ---------------------------------------------------------------------------
# Component probes


@lru_cache(maxsize=1)
def _expected_head() -> str | None:
    """The alembic migration head baked into this image (parsed once).

    ``None`` when the scripts can't be read OR the tree has multiple heads
    (``get_current_head`` raises) — both mean "we can't prove we're at a
    single known head", so the migrations component degrades.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        # health.py -> routes -> ownchart -> <api root>/alembic
        alembic_dir = Path(__file__).resolve().parents[2] / "alembic"
        cfg = Config()
        cfg.set_main_option("script_location", str(alembic_dir))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception as exc:  # noqa: BLE001
        log.warning("readyz_head_unresolved", error=f"{type(exc).__name__}: {exc}")
        return None


async def _check_db_and_migrations() -> tuple[str, str]:
    """(db_state, migrations_state). Separate connects so a missing
    ``alembic_version`` table degrades *migrations* only, not the DB."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        log.warning("readyz_db_unreachable", error=f"{type(exc).__name__}: {exc}")
        return "degraded", "degraded"
    # DB reachable — is the schema at head?
    try:
        async with engine.connect() as conn:
            current = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        # Table absent (never migrated) or unreadable → schema not at head.
        log.warning("readyz_migration_unreadable", error=f"{type(exc).__name__}: {exc}")
        return "ok", "degraded"
    head = _expected_head()
    migrations_state = "ok" if head is not None and current == head else "degraded"
    return "ok", migrations_state


def _check_storage() -> str:
    """Evidence-data dir writable. Non-destructive — ``os.access`` on the
    configured ``data_dir`` (the evidence vault lives under it)."""
    try:
        data_dir = get_settings().data_dir
        if data_dir.exists() and os.access(data_dir, os.W_OK):
            return "ok"
        return "degraded"
    except Exception as exc:  # noqa: BLE001
        log.warning("readyz_storage_check_failed", error=f"{type(exc).__name__}: {exc}")
        return "degraded"


async def _check_redis_and_worker() -> tuple[str, str]:
    """(redis_state, worker_state). A dead Redis degrades both; a live Redis
    with no fresh heartbeat key degrades only the worker."""
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            get_settings().redis_url, encoding="utf-8", decode_responses=True,
        )
        try:
            await client.ping()
            heartbeat = await client.get(WORKER_HEARTBEAT_KEY)
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        log.warning("readyz_redis_unreachable", error=f"{type(exc).__name__}: {exc}")
        return "degraded", "degraded"
    return "ok", ("ok" if heartbeat else "degraded")


@router.get("/readyz")
async def readyz(response: Response) -> dict:
    """Deep readiness. 503 when a CRITICAL component (db / migrations /
    storage) is degraded; 200 otherwise, with every component's state
    reported. Values are non-sensitive (``"ok"`` / ``"degraded"``)."""
    db_state, migrations_state = await _check_db_and_migrations()
    components = {
        "db": db_state,
        "migrations": migrations_state,
        "storage": _check_storage(),
    }
    redis_state, worker_state = await _check_redis_and_worker()
    components["redis"] = redis_state
    components["worker"] = worker_state

    ready, http_status = overall_readiness(components)
    response.status_code = http_status
    return {"status": "ok" if ready else "degraded", "components": components}
