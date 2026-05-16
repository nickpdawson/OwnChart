"""Demo data seed — ingests a bundled synthetic FHIR JSON file.

Runs once when OWNCHART_DEMO_MODE=true and the demo user has zero
sources. Looks for `/app/infra/demo_data/sample_patient.json` (or
the path set by OWNCHART_DEMO_BUNDLE_PATH). Skips silently if no
bundle is present so the demo container can still boot for UI work
without the full dataset.

Synthetic data sourcing options for the operator:
  - Synthea (https://github.com/synthetichealth/synthea) — generates
    Apache-2.0 FHIR bundles for arbitrary synthetic patients. The
    typical workflow: `./run_synthea -p 1 --exporter.fhir.export
    true`, then copy `output/fhir/*.json` to
    infra/demo_data/sample_patient.json.
  - Epic FHIR sandbox patient bundles — public sample data.
  - Hand-curated minimal bundle for QA.

The seed does NOT contact any remote FHIR server. Everything is
local file → DB.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ingest import storage
from ..models.conversation import Conversation
from ..models.episode import Episode
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.user import User
from .config import get_settings
from .logger import get_logger

log = get_logger("ownchart.core.demo_data_seed")


_DEFAULT_BUNDLE_PATH = "/app/infra/demo_data/sample_patient.json"


def _bundle_path() -> Path:
    return Path(os.environ.get("OWNCHART_DEMO_BUNDLE_PATH", _DEFAULT_BUNDLE_PATH))


async def purge_stale_demo_state_if_needed(
    db: AsyncSession,
    *,
    max_age_hours: int = 24,
) -> dict[str, int]:
    """Purge per-visitor conversations + user-saved episodes older than
    ``max_age_hours`` from the shared demo account.

    The demo account is shared across visitors; per-visitor scoping
    (see core/demo_session.py) hides one visitor's chat from the next,
    but the rows still accumulate in the DB. This sweep keeps the DB
    bounded and limits the leakage window if a filter ever regresses
    — old chats are simply gone.

    No-op outside demo mode. Returns counts for observability.
    """
    s = get_settings()
    if not s.demo_mode:
        return {"conversations": 0, "episodes": 0}

    demo_user = (await db.execute(
        select(User).where(User.email == s.demo_user_email)
    )).scalar_one_or_none()
    if demo_user is None:
        return {"conversations": 0, "episodes": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    # Conversations: every demo-mode conversation is a visitor chat
    # (there's no seeded conversation in the demo bundle). Delete
    # anything older than cutoff, regardless of session id.
    conv_del = await db.execute(
        delete(Conversation)
        .where(Conversation.user_id == demo_user.id)
        .where(Conversation.created_at < cutoff)
    )

    # Episodes: only user-created saves get the per-visitor stamp.
    # Seeded / LLM / heuristic episodes stay forever; user saves
    # purge with their parent conversation.
    ep_del = await db.execute(
        delete(Episode)
        .where(Episode.user_id == demo_user.id)
        .where(Episode.created_by == "user")
        .where(Episode.created_at < cutoff)
    )

    await db.commit()

    return {
        "conversations": conv_del.rowcount or 0,
        "episodes": ep_del.rowcount or 0,
    }


async def seed_demo_data_if_needed(db: AsyncSession) -> int:
    """If demo mode is on and the demo user has no sources, ingest
    the bundled FHIR JSON. Returns the number of source documents
    created (0 if nothing happened).
    """
    s = get_settings()
    if not s.demo_mode:
        return 0

    demo_user = (await db.execute(
        select(User).where(User.email == s.demo_user_email)
    )).scalar_one_or_none()
    if demo_user is None:
        log.info("demo_data_seed_skip_no_user")
        return 0

    existing = (await db.execute(
        select(func.count(SourceDocument.id))
        .where(SourceDocument.owner_user_id == demo_user.id)
    )).scalar_one() or 0
    if existing > 0:
        return 0

    path = _bundle_path()
    if not path.exists():
        log.info("demo_data_seed_skip_no_bundle", path=str(path))
        return 0

    try:
        bundle_bytes = path.read_bytes()
        bundle = json.loads(bundle_bytes.decode("utf-8"))
    except (OSError, ValueError) as e:
        log.warning("demo_data_seed_read_failed", error=str(e))
        return 0

    # Persist the raw bundle as one SourceDocument — same shape the
    # SMART-on-FHIR live ingest produces.
    async def _stream():
        yield bundle_bytes

    blob = await storage.write_blob(_stream(), suffix=".json")
    src = SourceDocument(
        owner_user_id=demo_user.id,
        source_type="fhir_bundle",
        original_filename=path.name,
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type="application/fhir+json",
        acquired_at=datetime.now(timezone.utc),
        source_system="demo:synthetic",
        source_label="Synthetic patient bundle (demo)",
        raw_metadata={
            "demo_seed": True,
            "deduplicated": blob.already_existed,
            "size_bytes": blob.size_bytes,
            "entries": _entry_count(bundle),
        },
    )
    db.add(src)
    await db.flush()

    # Extract individual ExtractedFact + EvidenceAnchor rows so the
    # dossiers, timeline, and Episode Intelligence pages have
    # something to render. Reuses the same helpers the live
    # connector sync uses (lazy import — routes/ depends on core/,
    # so core/ can't depend on routes/ statically).
    facts_created = await _extract_facts_from_bundle(db, src, bundle)
    await db.commit()
    log.info("demo_data_seed_done",
             source_id=str(src.id),
             entries=_entry_count(bundle),
             facts_extracted=facts_created)
    return 1


def _entry_count(bundle: dict) -> int:
    if not isinstance(bundle, dict):
        return 0
    entries = bundle.get("entry")
    return len(entries) if isinstance(entries, list) else 0


async def _extract_facts_from_bundle(
    db: AsyncSession,
    src: SourceDocument,
    bundle: dict,
) -> int:
    """Reshape Bundle.entry[] → {resourceType: [resources]} (the same
    `snap.fhir` shape the live SMART-on-FHIR fetcher produces), then
    walk the connector's existing extraction loop. Returns the
    number of ExtractedFact rows created.
    """
    snap_fhir: dict[str, list[dict]] = {}
    for entry in bundle.get("entry", []) or []:
        if not isinstance(entry, dict):
            continue
        res = entry.get("resource")
        if not isinstance(res, dict):
            continue
        rt = res.get("resourceType")
        if not isinstance(rt, str):
            continue
        snap_fhir.setdefault(rt, []).append(res)

    if not snap_fhir:
        return 0

    # Lazy imports — routes/connectors pulls in FastAPI deps. Doing
    # this here avoids an import cycle at module-load time.
    from ..ingest.fact_classifier import review_state_for_fhir
    from ..routes.connectors import (
        _FHIR_TO_CLAIM,
        _build_encounter_date_index,
        _date_for_with_fallback,
        _label_for,
    )

    encounter_dates = _build_encounter_date_index(snap_fhir)
    fact_count = 0
    for rt, resources in snap_fhir.items():
        fact_type = _FHIR_TO_CLAIM.get(rt)
        if not fact_type:
            continue
        for res in resources:
            anchor = EvidenceAnchor(
                source_document_id=src.id,
                anchor_type="fhir_resource",
                section_path=f"{rt}/{res.get('id', '?')}",
                text_excerpt=None,
            )
            db.add(anchor)
            await db.flush()
            ds, dp = _date_for_with_fallback(res, encounter_dates)
            label = _label_for(res)
            db.add(ExtractedFact(
                fact_type=fact_type,
                label=label,
                description=None,
                date_start=ds,
                date_end=None,
                date_precision=dp,
                confidence=85,
                review_state=review_state_for_fhir(label),
                evidence_anchor_ids=[anchor.id],
                extraction_method="fhir_resource",
            ))
            fact_count += 1
    return fact_count
