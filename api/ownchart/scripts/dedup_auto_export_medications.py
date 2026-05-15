"""Backfill: collapse Auto Export medication duplicates.

The Health Auto Export iOS app re-pushes the full medication history
on every push. Before 2026-05-15, the auto-export worker had no
dedup — every push re-inserted every scheduled dose. Result: same
`(label, exact scheduled timestamp, status)` tuple landed N times.

This script walks all existing Auto Export medication facts
(`fact_type='medication'`, `extraction_method='patient_self_report'`,
`client_sample_key IS NULL`), computes the same deterministic
`client_sample_key` the ingest path now generates, and for each
duplicate group:

  - Keeps the OLDEST row (smallest `created_at`).
  - Stamps the kept row with the new `client_sample_key` so future
    re-pushes dedup against it.
  - Deletes the rest.
  - Cascades drop their `episode_members` rows via FK ON DELETE
    CASCADE; the keeper retains any references it already had.

Safe to run repeatedly — once a group is collapsed, the
`client_sample_key` is set on the survivor and no further dupes
exist for that group.

Run with:
  docker compose exec api python -m ownchart.scripts.dedup_auto_export_medications
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from ..core.db import SessionLocal
from ..models.audit_event import AuditEvent
from ..models.extracted_fact import ExtractedFact


def _csk_for(label: str, ds: datetime, description: str | None) -> str:
    """Reconstruct the dedup key for an existing fact.

    Mirrors `ingest/auto_export.py::_emit_medication`. Status comes
    from the description; the emitter formats description as
    `"<status> · <dosage> · (nickname)"` so the first token before
    " · " is the status (or the whole description if no separator).
    """
    status = ""
    if description:
        first = description.split(" · ", 1)[0].strip()
        # Status values from Auto Export: Taken / Skipped / Not Interacted.
        if first in ("Taken", "Skipped", "Not Interacted", "Snoozed"):
            status = first
    blob = (
        f"auto-export:medication:"
        f"{(label or '').lower().strip()}:"
        f"{ds.replace(microsecond=0).isoformat()}:"
        f"{status.lower()}"
    )
    return "ae-med-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


async def main() -> None:
    async with SessionLocal() as db:
        rows = list((await db.execute(
            select(ExtractedFact)
            .where(ExtractedFact.fact_type == "medication")
            .where(ExtractedFact.extraction_method == "patient_self_report")
            .where(ExtractedFact.client_sample_key.is_(None))
            .where(ExtractedFact.date_start.isnot(None))
            .order_by(ExtractedFact.created_at.asc())
        )).scalars().all())

        if not rows:
            print("No keyless Auto Export medication facts found. Nothing to dedup.")
            return

        # Group by computed key.
        groups: dict[str, list[ExtractedFact]] = defaultdict(list)
        for r in rows:
            assert r.date_start is not None
            csk = _csk_for(r.label, r.date_start, r.description)
            groups[csk].append(r)

        print(
            f"Walked {len(rows)} keyless medication facts → "
            f"{len(groups)} distinct dedup groups."
        )

        kept = 0
        deleted = 0
        ids_to_delete: list[Any] = []
        for csk, members in groups.items():
            # Members already arrive ordered by created_at asc.
            survivor = members[0]
            survivor.client_sample_key = csk
            kept += 1
            for m in members[1:]:
                ids_to_delete.append(m.id)
                deleted += 1

        if ids_to_delete:
            # Chunk the delete so an arbitrarily large IN list doesn't
            # blow up the SQL parser.
            CHUNK = 1000
            for i in range(0, len(ids_to_delete), CHUNK):
                chunk = ids_to_delete[i : i + CHUNK]
                await db.execute(
                    delete(ExtractedFact).where(ExtractedFact.id.in_(chunk))
                )

        now = datetime.now(timezone.utc)
        # Audit summary — attribute to the first survivor's user.
        if kept > 0:
            sample_user_id = rows[0].id  # placeholder; not all facts carry user_id directly
            # ExtractedFact links to user via evidence_anchor → source_document;
            # for a one-shot script audit row we omit subject_id and rely on
            # the detail summary.
            db.add(AuditEvent(
                user_id=None,
                event_type="auto_export_medication_dedup_backfill",
                subject_type="extracted_fact",
                subject_id=None,
                detail={
                    "rows_walked": len(rows),
                    "groups": len(groups),
                    "kept": kept,
                    "deleted": deleted,
                    "ran_at": now.isoformat(),
                },
            ))
            _ = sample_user_id
        await db.commit()
        print(
            f"Kept {kept} canonical rows (one per dedup group), "
            f"deleted {deleted} duplicates."
        )


if __name__ == "__main__":
    asyncio.run(main())
