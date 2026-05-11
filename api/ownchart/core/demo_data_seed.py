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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ingest import storage
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
        source_system="demo:epic-sandbox",
        source_label="Epic FHIR sandbox (demo)",
        raw_metadata={
            "demo_seed": True,
            "deduplicated": blob.already_existed,
            "size_bytes": blob.size_bytes,
            "entries": _entry_count(bundle),
        },
    )
    db.add(src)
    await db.flush()
    await db.commit()
    log.info("demo_data_seed_done",
             source_id=str(src.id),
             entries=_entry_count(bundle))
    return 1


def _entry_count(bundle: dict) -> int:
    if not isinstance(bundle, dict):
        return 0
    entries = bundle.get("entry")
    return len(entries) if isinstance(entries, list) else 0
