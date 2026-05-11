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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import encrypt
from ..core.db import get_session
from ..core.logger import get_logger
from ..llm.providers import available_providers
from ..models.audit_event import AuditEvent
from ..models.llm_provider_credential import LlmProviderCredential
from ..models.user import User
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
