"""Re-extract personal photos under the latest vision prompt.

The 2026-05-16 vision prompt change (P0-3) added structured_facts
extraction so screenshots of vaccine cards, lab results, prescription
labels, etc. produce real ExtractedFact rows. Photos uploaded BEFORE
the deploy ran under the old prompt and didn't get structured facts.

This script finds every photo whose vision pass completed before the
deploy and re-enqueues it through the latest extractor. Filters:

  - source_type = 'photo'
  - raw_metadata.vision is populated (we have a previous run)
  - no image_structured_field anchor exists yet (otherwise the
    new prompt already ran on it)

Safe to re-run; idempotent. Each photo costs one Claude Opus vision
call — typically a few cents each.

Usage:
    docker compose exec api python -m ownchart.scripts.reextract_screenshots
    docker compose exec api python -m ownchart.scripts.reextract_screenshots --dry-run
    docker compose exec api python -m ownchart.scripts.reextract_screenshots --limit 5
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select, text

from ..core.arq_pool import enqueue_personal_photo_vision
from ..core.db import SessionLocal
from ..models.evidence_anchor import EvidenceAnchor
from ..models.source_document import SourceDocument


async def main(*, dry_run: bool, limit: int | None) -> None:
    async with SessionLocal() as db:
        # Photos with a completed vision pass.
        sources = list((await db.execute(
            select(SourceDocument)
            .where(SourceDocument.source_type == "photo")
            .where(text("raw_metadata ? 'vision'"))
            .order_by(SourceDocument.acquired_at.desc())
        )).scalars().all())
        # Already-structured photos to skip.
        existing_structured = set((await db.execute(
            select(EvidenceAnchor.source_document_id)
            .where(EvidenceAnchor.anchor_type == "image_structured_field")
        )).scalars().all())

        todo = [s for s in sources if s.id not in existing_structured]
        if limit is not None:
            todo = todo[:limit]

        print(f"Found {len(sources)} photos with vision; "
              f"{len(existing_structured)} already structured; "
              f"{len(todo)} eligible for re-extract.")
        if dry_run:
            for s in todo:
                print(f"  would re-extract: {s.id}  {s.original_filename}  "
                      f"acquired={s.acquired_at.isoformat()}")
            print("(dry run — nothing enqueued)")
            return

        enqueued = 0
        for s in todo:
            await enqueue_personal_photo_vision(str(s.id))
            # Flag UI pending so users see the spinner come back.
            rm = dict(s.raw_metadata or {})
            rm["vision_pending"] = True
            s.raw_metadata = rm
            enqueued += 1
        await db.commit()
        print(f"Enqueued {enqueued} re-extract jobs.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="List candidates without enqueueing.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap re-extract to N photos (cost guard).")
    args = p.parse_args()
    asyncio.run(main(dry_run=args.dry_run, limit=args.limit))
