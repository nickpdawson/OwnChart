from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .core.config import get_settings
from .core.db import SessionLocal
from .core.logger import configure_logging, get_logger
from .core.seed import seed_provider_connectors
from .core.demo_data_seed import seed_demo_data_if_needed
from .core.demo_seed import seed_demo_user_if_needed
from .routes import (
    ask,
    audit,
    auth,
    auth_device,
    auto_export,
    connectors,
    consent,
    conversations,
    discover,
    episodes,
    facts,
    health,
    healthkit_sync,
    home_ai,
    instance,
    llm_providers,
    sensemaking,
    settings as settings_routes,
    sources,
    stats,
    timeline,
    topics,
)

configure_logging()
log = get_logger("ownchart.main")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log.info("ownchart_api_starting", env=settings.env, demo_mode=settings.demo_mode)
    try:
        async with SessionLocal() as db:
            count = await seed_provider_connectors(db)
            log.info("startup_seed_done", connectors_upserted=count)
            if settings.demo_mode:
                seeded_user = await seed_demo_user_if_needed(db)
                log.info("demo_user_seeded", seeded=seeded_user)
                seeded_data = await seed_demo_data_if_needed(db)
                log.info("demo_data_seeded", sources=seeded_data)
    except Exception as e:  # noqa: BLE001
        log.warning("startup_seed_failed", error=str(e))
    yield
    log.info("ownchart_api_stopping")


# Demo-mode write guard. When OWNCHART_DEMO_MODE=true, every mutating
# request outside an allowlist gets a 403. The allowlist is short:
# auth (so demo user can sign in), consent (so they can grant PHI to
# play with the LLM), and conversation/episode/sensemaking POSTs (the
# whole point of the demo). These all write per-session state that
# gets reset when we rebuild the demo DB; they never mutate sample
# evidence.
_DEMO_WRITE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
    "/api/consent",
    "/api/conversations",
    "/api/episodes",
    "/api/sensemaking",
    "/api/home",
    "/api/facts/significance-backfill",  # admin task; user-scoped
)

# Extra writes permitted ONLY when demo_mode + demo_allow_ingest are
# both True. Used by the operator to SMART-on-FHIR connect a sandbox
# account or upload sample data through the standard UI flow. Banner
# stays visible so users still know it's a demo.
_DEMO_INGEST_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/api/connectors",
    "/api/sources",
    "/api/healthkit",
    "/api/auto-export",
    "/api/facts",
    "/api/topics",
    "/api/settings",
    "/api/llm-providers",
)
_DEMO_BLOCKED_METHODS: frozenset[str] = frozenset({"POST", "PATCH", "PUT", "DELETE"})


async def _demo_readonly_middleware(request: Request, call_next):
    if not settings.demo_mode:
        return await call_next(request)
    if request.method not in _DEMO_BLOCKED_METHODS:
        return await call_next(request)
    path = request.url.path
    if any(path.startswith(p) for p in _DEMO_WRITE_ALLOWED_PREFIXES):
        return await call_next(request)
    if settings.demo_allow_ingest and any(
        path.startswith(p) for p in _DEMO_INGEST_ALLOWED_PREFIXES
    ):
        return await call_next(request)
    return JSONResponse(
        status_code=403,
        content={
            "detail": (
                "Demo mode: this endpoint is read-only on the public "
                "demo. Stand up your own instance to write data."
            ),
        },
    )


app = FastAPI(
    title="OwnChart API",
    version="0.1.0",
    description="Patient-owned longitudinal health intelligence.",
    lifespan=lifespan,
)

if settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Demo-mode read-only guard. Registered unconditionally; the
# middleware itself short-circuits when settings.demo_mode is False.
app.middleware("http")(_demo_readonly_middleware)

app.include_router(health.router)
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(auth_device.router, prefix="/api/auth/device", tags=["auth-device"])
app.include_router(consent.router, prefix="/api/consent", tags=["consent"])
app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(topics.router, prefix="/api/topics", tags=["topics"])
app.include_router(facts.router, prefix="/api/facts", tags=["facts"])
app.include_router(ask.router, prefix="/api/ask", tags=["ask"])
app.include_router(audit.router, prefix="/api/audit", tags=["audit"])
app.include_router(connectors.router, prefix="/api/connectors", tags=["connectors"])
app.include_router(stats.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(auto_export.router, prefix="/api/auto-export", tags=["auto-export"])
app.include_router(timeline.router, prefix="/api/timeline", tags=["timeline"])
app.include_router(discover.router, prefix="/api/discover", tags=["discover"])
app.include_router(healthkit_sync.router, prefix="/api/healthkit", tags=["healthkit"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["settings"])
app.include_router(sensemaking.router, prefix="/api", tags=["sensemaking"])
app.include_router(conversations.router, prefix="/api/conversations", tags=["conversations"])
app.include_router(episodes.router, prefix="/api/episodes", tags=["episodes"])
app.include_router(home_ai.router, prefix="/api/home", tags=["home"])
app.include_router(llm_providers.router, prefix="/api/llm-providers", tags=["llm-providers"])
app.include_router(instance.router, prefix="/api/instance", tags=["instance"])
