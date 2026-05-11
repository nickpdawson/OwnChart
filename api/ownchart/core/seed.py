"""Idempotent seeders run on api startup.

Currently:
  - provider_connectors  ← /app/infra/connectors.seed.yaml (or env-overridden path)

Tokens, connections, cached counts are NEVER touched by seeding — only the
registry row's static fields (name, fhir_base, scopes, ehr_vendor). client_id
is sourced from an env var per row so we don't ship secrets in the seed file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.provider_connector import ProviderConnector
from .logger import get_logger

log = get_logger("ownchart.seed")

DEFAULT_SEED_PATH = "/app/infra/connectors.seed.yaml"


async def seed_provider_connectors(db: AsyncSession, path: str | None = None) -> int:
    p = Path(path or os.environ.get("OWNCHART_CONNECTORS_SEED_PATH") or DEFAULT_SEED_PATH)
    if not p.exists():
        log.info("seed_skipped_missing_file", path=str(p))
        return 0
    try:
        data = yaml.safe_load(p.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("seed_yaml_parse_failed", path=str(p), error=str(e))
        return 0
    rows = (data or {}).get("connectors") or []
    upserted = 0
    for row in rows:
        slug = row.get("slug")
        if not slug:
            continue
        existing = (await db.execute(
            select(ProviderConnector).where(ProviderConnector.slug == slug)
        )).scalar_one_or_none()

        # Resolve client_id from env override (preferred) or inline literal.
        env_var = row.get("client_id_env")
        client_id = os.environ.get(env_var) if env_var else None
        if client_id is None and "client_id" in row:
            client_id = row["client_id"]

        if existing is None:
            existing = ProviderConnector(
                slug=slug,
                name=row.get("name", slug),
                ehr_vendor=row.get("ehr_vendor"),
                fhir_base=row["fhir_base"],
                scopes=row.get("scopes") or "openid fhirUser launch/patient patient/*.rs offline_access",
                enabled=row.get("enabled", True),
                client_id=client_id,
            )
            db.add(existing)
        else:
            # Refresh static fields. Don't clobber an existing client_id with None.
            existing.name = row.get("name", existing.name)
            existing.ehr_vendor = row.get("ehr_vendor") or existing.ehr_vendor
            existing.fhir_base = row.get("fhir_base") or existing.fhir_base
            existing.scopes = row.get("scopes") or existing.scopes
            existing.enabled = row.get("enabled", existing.enabled)
            if client_id:
                existing.client_id = client_id
        upserted += 1

    await db.commit()
    log.info("seed_provider_connectors_complete", count=upserted)
    return upserted
