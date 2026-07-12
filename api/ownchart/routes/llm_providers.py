"""LLM provider credential routes (Q-C1, 2026-05-11 PM).

Per-user BYOK + an admin-managed default. The encrypted_secret column
uses the same AES-256-GCM envelope that protects OAuth tokens
(core/crypto.py).

Endpoints:

  GET    /api/llm-providers              — provider catalog + status
                                           (re-exports providers.available_providers
                                            with credential-row info merged in)
  GET    /api/llm-providers/credentials  — list current user's credentials
                                           (NEVER returns the secret)
  POST   /api/llm-providers/credentials  — create or replace
  DELETE /api/llm-providers/credentials/{id}
                                         — revoke

The Settings UI calls these via Settings → Providers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import encrypt
from ..core.db import get_session
from ..core.logger import get_logger
from ..llm.budget import (
    ceiling_from_setting,
    month_start_utc,
    next_month_start_utc,
)
from ..llm.pricing import estimate_cost_usd, price_for
from ..llm.providers import available_providers
from ..models.audit_event import AuditEvent
from ..models.llm_provider_credential import LlmProviderCredential
from ..models.user import User
from ..settings.registry import effective as setting_effective
from .auth import get_current_user

router = APIRouter()
log = get_logger("ownchart.routes.llm_providers")


SUPPORTED_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "gemini", "local", "azure_openai")
SUPPORTED_AUTH_KINDS: tuple[str, ...] = ("api_key", "oauth", "local_endpoint")


class ProviderShape(BaseModel):
    key: str
    label: str
    configured: bool
    capabilities: dict[str, Any]
    user_credential_count: int = 0


class ProviderCatalogResponse(BaseModel):
    providers: list[ProviderShape]


class CredentialOut(BaseModel):
    id: str
    provider: str
    auth_kind: str
    label: str | None
    default_model: str | None
    endpoint_url: str | None
    capabilities: dict[str, Any]
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    has_secret: bool


class CreateCredentialRequest(BaseModel):
    provider: str
    auth_kind: str = "api_key"
    label: str | None = None
    default_model: str | None = None
    secret: str | None = None         # api_key OR oauth refresh token
    endpoint_url: str | None = None   # local_endpoint
    capabilities: dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=ProviderCatalogResponse)
async def list_providers_catalog(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ProviderCatalogResponse:
    """Provider catalog merged with the user's own credential rows.

    `configured` is True iff EITHER the deployment default works OR
    the user has a non-revoked credential row for that provider.
    """
    catalog = available_providers()
    counts: dict[str, int] = {}
    rows = list((await db.execute(
        select(LlmProviderCredential.provider)
        .where(LlmProviderCredential.user_id == user.id)
        .where(LlmProviderCredential.revoked_at.is_(None))
    )).scalars().all())
    for p in rows:
        counts[p] = counts.get(p, 0) + 1
    out: list[ProviderShape] = []
    for p in catalog:
        key = str(p["key"])
        out.append(ProviderShape(
            key=key,
            label=str(p["label"]),
            configured=bool(p["configured"]) or counts.get(key, 0) > 0,
            capabilities=dict(p["capabilities"] or {}),
            user_credential_count=counts.get(key, 0),
        ))
    return ProviderCatalogResponse(providers=out)


def _to_out(c: LlmProviderCredential) -> CredentialOut:
    return CredentialOut(
        id=str(c.id),
        provider=c.provider,
        auth_kind=c.auth_kind,
        label=c.label,
        default_model=c.default_model,
        endpoint_url=c.endpoint_url,
        capabilities=c.capabilities or {},
        last_used_at=c.last_used_at,
        revoked_at=c.revoked_at,
        created_at=c.created_at,
        has_secret=c.encrypted_secret is not None
        or c.encrypted_refresh_token is not None,
    )


@router.get("/credentials", response_model=list[CredentialOut])
async def list_credentials(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[CredentialOut]:
    rows = list((await db.execute(
        select(LlmProviderCredential)
        .where(LlmProviderCredential.user_id == user.id)
        .order_by(LlmProviderCredential.created_at.desc())
    )).scalars().all())
    return [_to_out(c) for c in rows]


@router.post("/credentials", response_model=CredentialOut,
             status_code=status.HTTP_201_CREATED)
async def create_credential(
    body: CreateCredentialRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> CredentialOut:
    if body.provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"provider must be one of {list(SUPPORTED_PROVIDERS)}",
        )
    if body.auth_kind not in SUPPORTED_AUTH_KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"auth_kind must be one of {list(SUPPORTED_AUTH_KINDS)}",
        )
    if body.auth_kind == "api_key" and not body.secret:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="api_key auth_kind requires `secret`",
        )
    if body.auth_kind == "local_endpoint" and not body.endpoint_url:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="local_endpoint auth_kind requires `endpoint_url`",
        )
    now = datetime.now(timezone.utc)
    encrypted_secret: bytes | None = None
    if body.secret:
        encrypted_secret = encrypt(body.secret)

    row = LlmProviderCredential(
        user_id=user.id,
        provider=body.provider,
        auth_kind=body.auth_kind,
        encrypted_secret=encrypted_secret,
        endpoint_url=body.endpoint_url,
        label=body.label,
        default_model=body.default_model,
        capabilities=body.capabilities or {},
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.flush()

    db.add(AuditEvent(
        user_id=user.id,
        event_type="llm_credential_created",
        subject_type="llm_provider_credential",
        subject_id=str(row.id),
        detail={
            "provider": body.provider,
            "auth_kind": body.auth_kind,
            "has_secret": encrypted_secret is not None,
            "endpoint_url": body.endpoint_url,
            "label": body.label,
        },
    ))
    await db.commit()
    return _to_out(row)


@router.delete("/credentials/{cred_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_credential(
    cred_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    row = await db.get(LlmProviderCredential, cred_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if row.revoked_at is not None:
        return  # idempotent
    row.revoked_at = datetime.now(timezone.utc)
    db.add(AuditEvent(
        user_id=user.id,
        event_type="llm_credential_revoked",
        subject_type="llm_provider_credential",
        subject_id=str(row.id),
        detail={"provider": row.provider},
    ))
    await db.commit()


# ---------------------------------------------------------------------------
# Cost attribution (#109) + per-user scoping (Batch B, closes MED-5)
#
# /api/llm-providers/usage returns model_runs rows with derived
# per-row cost so the UI can answer: "where did my spend go this
# week?" Filters by date range / provider / model / purpose /
# billed_to. CSV export via ?format=csv for paste-into-spreadsheet
# workflows.
#
# Pricing lives in llm/pricing.py (one source of truth, shared with the
# ModelRun stamping path). Per-row cost prefers the stored
# `estimated_cost_usd` column and falls back to recomputing for legacy
# rows written before the column existed. When a model isn't priced,
# `estimated_usd_cost` is null and the UI shows "—".
#
# SCOPING (MED-5): every row is filtered to the CALLING user's own
# model_runs (`ModelRun.user_id == user.id`). A second user's activity
# is never returned here. A cross-user admin surface, if needed, is a
# separate (Batch C) endpoint — this one is caller-scoped, period.


def _row_cost(model: str | None, stored: Any, usage: dict[str, Any] | None) -> float | None:
    """Per-row cost for the usage view.

    Prefer the stored ``estimated_cost_usd`` column (authoritative,
    computed at write time). Fall back to recomputing from usage for
    legacy rows predating the column. None → unpriced model ("—").
    """
    if price_for(model) is None:
        return None
    if stored is not None:
        return round(float(stored), 6)
    return estimate_cost_usd(model, usage if isinstance(usage, dict) else None)


class UsageRow(BaseModel):
    id: str
    created_at: datetime
    provider: str
    model: str
    purpose: str
    prompt_version: str | None
    billed_to: str | None         # "user_byok" | "deployment_default" | None (legacy)
    billed_credential_id: str | None
    billed_credential_label: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    latency_ms: int | None
    estimated_usd_cost: float | None
    error: str | None


class UsageAggregate(BaseModel):
    total_runs: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_creation_tokens: int
    total_estimated_usd_cost: float
    runs_with_unknown_cost: int


class UsageBreakdownItem(BaseModel):
    key: str                      # model id or purpose
    runs: int
    estimated_usd_cost: float


class MonthToDateScope(BaseModel):
    """Current-calendar-month (UTC) spend for the calling user.

    Independent of the row date filters — always the live month-to-date
    view the monthly ceiling is enforced against, so the UI can show
    "you've spent $X of $Y, $Z left, resets on <date>".
    """
    month_start: datetime
    resets_at: datetime
    total_estimated_usd_cost: float
    ceiling_usd: float | None       # None → unlimited (unset / <= 0)
    remaining_usd: float | None     # None → unlimited
    over_budget: bool
    per_model: list[UsageBreakdownItem]
    per_purpose: list[UsageBreakdownItem]


class UsageResponse(BaseModel):
    rows: list[UsageRow]
    aggregate: UsageAggregate
    scope: MonthToDateScope


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    date_from: str | None = Query(default=None, description="ISO date (inclusive)."),
    date_to: str | None = Query(default=None, description="ISO date (inclusive)."),
    provider: str | None = Query(default=None),
    model: str | None = Query(default=None),
    purpose: str | None = Query(default=None),
    billed_to: str | None = Query(default=None, description="'user_byok' | 'deployment_default'"),
    limit: int = Query(default=500, ge=1, le=5000),
    format: str = Query(default="json", description="'json' (default) or 'csv'."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """Per-model_run usage + cost rows for the CALLING user only.

    MED-5: scoped to `ModelRun.user_id == user.id` — a user never sees
    another user's activity here. The `scope` block is the live
    month-to-date (UTC) view the monthly ceiling is enforced against
    (total + per-model/per-purpose breakdown + ceiling + remaining).

    CSV is returned with a Content-Disposition attachment header so
    browsers prompt a download.
    """
    from datetime import datetime as _dt
    from fastapi.responses import Response

    from ..models.model_run import ModelRun

    # Date parsing — accept YYYY-MM-DD or full ISO datetimes. For a
    # date-only `date_to`, treat the value as end-of-day inclusive
    # (add 1 day, use strict <) so a row created at 14:00 UTC on
    # 2026-05-15 is still in range when the filter is
    # `date_to=2026-05-15`. Without this we'd silently exclude every
    # row from "today" — the bug Nick caught on usage 2026-05-15.
    from datetime import timedelta as _td
    df: _dt | None = None
    dt_: _dt | None = None
    dt_is_date_only = False
    if date_from:
        try:
            df = _dt.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"date_from must be ISO, got {date_from!r}")
    if date_to:
        try:
            dt_ = _dt.fromisoformat(date_to)
            dt_is_date_only = len(date_to) == 10  # 'YYYY-MM-DD' is 10 chars
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"date_to must be ISO, got {date_to!r}")

    # MED-5: every query in this endpoint is scoped to the caller.
    stmt = (
        select(ModelRun)
        .where(ModelRun.user_id == user.id)
        .order_by(ModelRun.created_at.desc())
        .limit(limit)
    )
    if df is not None:
        stmt = stmt.where(ModelRun.created_at >= df)
    if dt_ is not None:
        if dt_is_date_only:
            # End-of-day inclusive: < midnight of the following day.
            stmt = stmt.where(ModelRun.created_at < dt_ + _td(days=1))
        else:
            stmt = stmt.where(ModelRun.created_at <= dt_)
    if provider:
        stmt = stmt.where(ModelRun.provider == provider)
    if model:
        stmt = stmt.where(ModelRun.model == model)
    if purpose:
        stmt = stmt.where(ModelRun.purpose == purpose)
    if billed_to:
        # usage->>billed_to filter via JSONB ->>
        from sqlalchemy import text as _text
        stmt = stmt.where(
            _text("usage->>'billed_to' = :bt").bindparams(bt=billed_to)
        )

    rows = list((await db.execute(stmt)).scalars().all())

    # Resolve credential labels for billed BYOK rows.
    cred_ids: set[uuid.UUID] = set()
    for r in rows:
        u = r.usage or {}
        cid = u.get("billed_credential_id") if isinstance(u, dict) else None
        if cid:
            try:
                cred_ids.add(uuid.UUID(cid))
            except (TypeError, ValueError):
                continue
    cred_label_by_id: dict[str, str | None] = {}
    if cred_ids:
        cred_rows = list((await db.execute(
            select(LlmProviderCredential.id, LlmProviderCredential.label)
            .where(LlmProviderCredential.id.in_(cred_ids))
        )).all())
        cred_label_by_id = {str(cid): label for cid, label in cred_rows}

    out_rows: list[UsageRow] = []
    agg = {
        "total_runs": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_estimated_usd_cost": 0.0,
        "runs_with_unknown_cost": 0,
    }
    for r in rows:
        u = r.usage or {}
        cost = _row_cost(r.model, r.estimated_cost_usd, u if isinstance(u, dict) else None)
        cid = (u.get("billed_credential_id") if isinstance(u, dict) else None)
        out_rows.append(UsageRow(
            id=str(r.id),
            created_at=r.created_at,
            provider=r.provider,
            model=r.model,
            purpose=r.purpose,
            prompt_version=r.prompt_version,
            billed_to=(u.get("billed_to") if isinstance(u, dict) else None),
            billed_credential_id=cid,
            billed_credential_label=cred_label_by_id.get(cid) if cid else None,
            input_tokens=u.get("input_tokens") if isinstance(u, dict) else None,
            output_tokens=u.get("output_tokens") if isinstance(u, dict) else None,
            cache_read_input_tokens=u.get("cache_read_input_tokens") if isinstance(u, dict) else None,
            cache_creation_input_tokens=u.get("cache_creation_input_tokens") if isinstance(u, dict) else None,
            latency_ms=u.get("latency_ms") if isinstance(u, dict) else None,
            estimated_usd_cost=cost,
            error=r.error,
        ))
        agg["total_runs"] += 1
        if isinstance(u, dict):
            agg["total_input_tokens"] += int(u.get("input_tokens") or 0)
            agg["total_output_tokens"] += int(u.get("output_tokens") or 0)
            agg["total_cache_read_tokens"] += int(u.get("cache_read_input_tokens") or 0)
            agg["total_cache_creation_tokens"] += int(u.get("cache_creation_input_tokens") or 0)
        if cost is None:
            agg["runs_with_unknown_cost"] += 1
        else:
            agg["total_estimated_usd_cost"] += cost
    agg["total_estimated_usd_cost"] = round(agg["total_estimated_usd_cost"], 4)

    if format == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "id", "created_at", "provider", "model", "purpose", "prompt_version",
            "billed_to", "billed_credential_id", "billed_credential_label",
            "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "latency_ms", "estimated_usd_cost", "error",
        ])
        for r in out_rows:
            w.writerow([
                r.id, r.created_at.isoformat(),
                r.provider, r.model, r.purpose, r.prompt_version or "",
                r.billed_to or "", r.billed_credential_id or "",
                r.billed_credential_label or "",
                r.input_tokens if r.input_tokens is not None else "",
                r.output_tokens if r.output_tokens is not None else "",
                r.cache_read_input_tokens if r.cache_read_input_tokens is not None else "",
                r.cache_creation_input_tokens if r.cache_creation_input_tokens is not None else "",
                r.latency_ms if r.latency_ms is not None else "",
                r.estimated_usd_cost if r.estimated_usd_cost is not None else "",
                (r.error or "").replace("\n", " ").replace("\r", " "),
            ])
        filename = f"ownchart-llm-usage-{datetime.now(timezone.utc).date().isoformat()}.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    scope = await _month_to_date_scope(db, user)
    return UsageResponse(rows=out_rows, aggregate=UsageAggregate(**agg), scope=scope)


async def _month_to_date_scope(db: AsyncSession, user: User) -> MonthToDateScope:
    """Live current-month (UTC) spend + ceiling view for one user.

    Computed independently of the row filters — it always reflects the
    same window the monthly ceiling is enforced against, so the UI's
    "$X of $Y this month, resets on <date>" never drifts from what the
    enforcement actually does.
    """
    from datetime import datetime as _dt

    from ..models.model_run import ModelRun

    now = _dt.now(timezone.utc)
    start = month_start_utc(now)
    resets = next_month_start_utc(now)

    mtd_rows = list((await db.execute(
        select(ModelRun)
        .where(ModelRun.user_id == user.id)
        .where(ModelRun.created_at >= start)
    )).scalars().all())

    total = 0.0
    per_model: dict[str, list[float]] = {}
    per_purpose: dict[str, list[float]] = {}
    for r in mtd_rows:
        cost = _row_cost(
            r.model, r.estimated_cost_usd,
            r.usage if isinstance(r.usage, dict) else None,
        ) or 0.0
        total += cost
        pm = per_model.setdefault(r.model, [0.0, 0.0])
        pm[0] += 1
        pm[1] += cost
        pp = per_purpose.setdefault(r.purpose, [0.0, 0.0])
        pp[0] += 1
        pp[1] += cost
    total = round(total, 6)

    ceiling = ceiling_from_setting(
        await setting_effective(db, user, "ai.monthly_spend_ceiling_usd")
    )
    remaining = None if ceiling is None else round(max(0.0, ceiling - total), 6)
    over_budget = ceiling is not None and total >= ceiling

    def _items(d: dict[str, list[float]]) -> list[UsageBreakdownItem]:
        return [
            UsageBreakdownItem(key=k, runs=int(v[0]), estimated_usd_cost=round(v[1], 6))
            for k, v in sorted(d.items(), key=lambda kv: -kv[1][1])
        ]

    return MonthToDateScope(
        month_start=start,
        resets_at=resets,
        total_estimated_usd_cost=total,
        ceiling_usd=ceiling,
        remaining_usd=remaining,
        over_budget=over_budget,
        per_model=_items(per_model),
        per_purpose=_items(per_purpose),
    )
