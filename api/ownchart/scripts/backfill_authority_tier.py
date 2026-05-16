"""Backfill `authority_tier` on `source_documents.raw_metadata`.

Source Authority Doctrine (user-docs/SOURCE_AUTHORITY_DOCTRINE.md): every
source carries a 6-tier authority label that retrieval + prompts read
to rank evidence. New sources stamp this at ingest; existing rows
need a one-shot backfill.

Safe to run repeatedly — only writes when the tier is missing or
has changed (e.g. classifier rules updated).

Run with:
  docker compose exec api python -m ownchart.scripts.backfill_authority_tier
"""

from __future__ import annotations

import asyncio
from collections import Counter

from sqlalchemy import select

from ..core.db import SessionLocal
from ..llm.conversations import _source_quality_tier
from ..models.source_document import SourceDocument


async def main() -> None:
    tier_counts: Counter[str] = Counter()
    changed = 0
    async with SessionLocal() as db:
        rows = list((await db.execute(select(SourceDocument))).scalars().all())
        for s in rows:
            tier = _source_quality_tier(s.source_label, s.original_filename, s.source_type)
            tier_counts[tier] += 1
            rm = dict(s.raw_metadata or {})
            if rm.get("authority_tier") != tier:
                rm["authority_tier"] = tier
                s.raw_metadata = rm
                changed += 1
        await db.commit()

    print(f"Scanned {sum(tier_counts.values())} source_documents.")
    print(f"Wrote authority_tier on {changed} rows (others already matched).")
    print()
    print("Tier distribution:")
    for tier, n in tier_counts.most_common():
        print(f"  {tier:28s} {n:6d}")


if __name__ == "__main__":
    asyncio.run(main())
