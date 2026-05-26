from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.config import get_settings
from .core.db import SessionLocal
from .core.logger import configure_logging, get_logger
from .core.demo_session import DemoSessionMiddleware
from .core.seed import seed_provider_connectors
from .core.upload_audit import upload_audit_from_request
from .core.demo_data_seed import (
    purge_stale_demo_state_if_needed,
    seed_demo_data_if_needed,
)
from .core.demo_calendar_seed import seed_demo_calendar_if_needed
from .core.demo_seed import seed_demo_user_if_needed
from .routes import (
    ask,
    audit,
    auth,
    auth_device,
    auto_export,
    calendar,
    calendar_google,
    connectors,
    consent,
    conversations,
    discover,
    episodes,
    exports,
    facts,
    health,
    healthkit_sync,
    home_ai,
    instance,
    invitations,
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
                seeded_cal = await seed_demo_calendar_if_needed(db)
                log.info("demo_calendar_seeded", events=seeded_cal)
                # Bound leakage window: drop visitor chats / saved
                # events older than 24h on every restart. Per-visitor
                # scoping in core/demo_session.py is the primary
                # defense; this is belt-and-suspenders + DB hygiene.
                purged = await purge_stale_demo_state_if_needed(db)
                log.info("demo_state_purged", **purged)
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
    "/api/ask",  # free-form Ask page — read-only LLM query over facts
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

# Per-visitor cookie issuer for the shared demo account. No-op
# outside demo mode. See core/demo_session.py for the rationale —
# without this, every visitor's Ask chat lands on the same shared
# demo user_id and ends up visible to subsequent visitors.
app.add_middleware(DemoSessionMiddleware)


# Catch-all exception handler. Without this, an unhandled exception in
# any route lets uvicorn return a plain-text "Internal Server Error"
# (21 bytes), which iOS can't decode into OCError.detail — the user
# sees only "Server error 500" with no actionable info. With this
# handler we always return JSON `{"detail": "..."}` and log the real
# exception with traceback. Triggered for the alpha by photo-upload
# 500s on 2026-05-15 — Nick reported repeated upload failures with no
# server-side context anywhere because nothing was logging.
#
# Upload-audit echo: when the request carries X-Client-Batch-Id /
# X-Client-Item-Id, mirror them into the response body so iOS can
# correlate a 500 back to the specific file in its local batch map.
# Without the echo, the user sees an opaque server error and the
# client has no way to know which of N parallel uploads it belongs to.
@app.exception_handler(Exception)
async def _structured_error_handler(request: Request, exc: Exception) -> JSONResponse:
    audit = upload_audit_from_request(request)
    log.exception(
        "unhandled_exception",
        method=request.method,
        path=request.url.path,
        exc_type=exc.__class__.__name__,
        client_batch_id=(audit or {}).get("client_batch_id"),
        client_item_id=(audit or {}).get("client_item_id"),
    )
    body: dict[str, object] = {
        "detail": (
            f"Server error: {exc.__class__.__name__}. "
            "The exception is logged server-side; share the timestamp "
            "with your administrator."
        ),
    }
    if audit:
        body["upload_audit"] = audit
    return JSONResponse(status_code=500, content=body)


# Same correlation for FastAPI's HTTPException path — without this,
# the 4xx responses iOS gets back (415/400/507/etc) carry only
# `{"detail": "..."}` and the client can't link them to its local
# batch state. iOS needs the echo on errors regardless of whether
# the error originated from our raise HTTPException(...) calls or
# from an unhandled exception.
@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException,
) -> JSONResponse:
    audit = upload_audit_from_request(request)
    body: dict[str, object] = {"detail": exc.detail}
    if audit:
        body["upload_audit"] = audit
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=getattr(exc, "headers", None) or {},
    )

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
app.include_router(calendar.router, prefix="/api/calendar", tags=["calendar"])
app.include_router(
    calendar_google.router,
    prefix="/api/calendar/google",
    tags=["calendar"],
)
app.include_router(exports.router, prefix="/api/exports", tags=["exports"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["settings"])
app.include_router(sensemaking.router, prefix="/api", tags=["sensemaking"])
app.include_router(conversations.router, prefix="/api/conversations", tags=["conversations"])
app.include_router(episodes.router, prefix="/api/episodes", tags=["episodes"])
app.include_router(home_ai.router, prefix="/api/home", tags=["home"])
app.include_router(llm_providers.router, prefix="/api/llm-providers", tags=["llm-providers"])
app.include_router(instance.router, prefix="/api/instance", tags=["instance"])
app.include_router(invitations.router, prefix="/api/invitations", tags=["invitations"])
