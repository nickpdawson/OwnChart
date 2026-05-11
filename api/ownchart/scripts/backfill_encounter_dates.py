"""Backfill ExtractedFact.date_start from linked FHIR Encounters.

Some EHR FHIR endpoints ((observed on at least one EHR)) return Procedures and other
clinically-meaningful resources without their own date fields, while
the parent Encounter is properly dated. The new ingest code falls back
to the encounter date at write time; this script reapplies that
fallback to facts that were already ingested before that fix landed.

Run inside the api container:

    docker compose exec api python -m ownchart.scripts.backfill_encounter_dates

Idempotent: only updates facts whose `date_start IS NULL`. Reads each
`fhir_bundle` SourceDocument's stored JSON to resolve the encounter
graph; never re-fetches from the EHR.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from ..core.db import SessionLocal
from ..core.logger import get_logger
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..routes.connectors import (
    _build_encounter_date_index,
    _date_for_with_fallback,
)

log = get_logger("ownchart.scripts.backfill_encounter_dates")


async def _backfill_for_source(db, src: SourceDocument) -> tuple[int, int]:
    """Apply encounter-date fallback to facts anchored to one fhir_bundle source.

    Returns (facts_examined, facts_updated).
    """
    bundle_path = Path(src.storage_uri)
    if not bundle_path.exists():
        log.warning("fhir_bundle_missing_on_disk", source_id=str(src.id), path=str(bundle_path))
        return (0, 0)
    try:
        snap = json.loads(bundle_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning(
            "fhir_bundle_parse_failed",
            source_id=str(src.id),
            error=f"{type(e).__name__}: {e}",
        )
        return (0, 0)

    encounter_dates = _build_encounter_date_index(snap)
    if not encounter_dates:
        return (0, 0)

    # Build {section_path → resource} so we can look up by anchor.
    resource_by_section: dict[str, dict] = {}
    for rt, resources in snap.items():
        if not isinstance(resources, list):
            continue
        for res in resources:
            rid = res.get("id") if isinstance(res, dict) else None
            if isinstance(rid, str):
                resource_by_section[f"{rt}/{rid}"] = res

    # Pull all anchors on this source, then their facts.
    anc_q = await db.execute(
        select(EvidenceAnchor).where(EvidenceAnchor.source_document_id == src.id)
    )
    anchors_by_id: dict[uuid.UUID, EvidenceAnchor] = {}
    for a in anc_q.scalars().all():
        anchors_by_id[a.id] = a
    if not anchors_by_id:
        return (0, 0)

    anchor_ids = list(anchors_by_id.keys())
    fact_q = await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.evidence_anchor_ids.op("&&")(anchor_ids))
        .where(ExtractedFact.date_start.is_(None))
    )
    facts = list(fact_q.scalars().all())

    examined = len(facts)
    updated = 0
    for f in facts:
        if not f.evidence_anchor_ids:
            continue
        anc = anchors_by_id.get(f.evidence_anchor_ids[0])
        if anc is None or not anc.section_path:
            continue
        res = resource_by_section.get(anc.section_path)
        if not isinstance(res, dict):
            continue
        # Apply the full current date-resolution chain (own date first,
        # then encounter fallback). We don't pre-skip on `own date
        # exists` — earlier ingests ran before the performedPeriod fix
        # landed, so facts with null date_start may now resolve to a
        # date that *is* on the resource itself.
        ds, dp = _date_for_with_fallback(res, encounter_dates)
        if ds is None:
            continue
        f.date_start = ds
        f.date_precision = dp or "day"
        updated += 1

    if updated:
        await db.commit()
    return (examined, updated)


async def main() -> int:
    total_examined = 0
    total_updated = 0
    sources_touched = 0
    async with SessionLocal() as db:
        srcs = (await db.execute(
            select(SourceDocument).where(SourceDocument.source_type == "fhir_bundle")
        )).scalars().all()
        for src in srcs:
            examined, updated = await _backfill_for_source(db, src)
            total_examined += examined
            total_updated += updated
            if updated:
                sources_touched += 1
                log.info(
                    "fhir_source_backfilled",
                    source_id=str(src.id),
                    examined=examined,
                    updated=updated,
                )
    print(
        f"Encounter-date backfill complete. "
        f"Sources scanned: {len(srcs)}; updated: {sources_touched}. "
        f"Facts examined (null date_start, FHIR-anchored): {total_examined}; updated: {total_updated}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
