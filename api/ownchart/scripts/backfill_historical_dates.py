"""Retroactive fix for the date-leak bug caught 2026-05-14 PM.

The clinical-note extractor used to default `date_start` to the
note's creation_date when the LLM didn't extract an explicit date.
That's correct for SAME-DAY findings (the note recorded what
happened today) but a lie for HISTORICAL entries — a 2026 note's
"History of ACL surgery" should not be dated 2026-05-09.

Home / Timeline / Discover were therefore showing historical
events as if they happened on the import date. This script
walks every `claude_clinical_note_v1` extracted_fact whose label
looks historical (or whose description='history_of') and either:

  - sets date_start to NULL (date is genuinely unknown), AND
  - stamps coded_concepts.date_origin='historical_undated' AND
    coded_concepts.historical=true,

so downstream surfaces can render an "undated · historical" badge
instead of "May 9 2026."

Run inside the api container:

    docker compose exec api python -m ownchart.scripts.backfill_historical_dates

Idempotent: re-running does nothing on facts already stamped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Optional

from sqlalchemy import or_, select

from ..core.db import SessionLocal
from ..core.logger import get_logger
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument

log = get_logger("ownchart.scripts.backfill_historical_dates")


# Mirrors the extractor's _looks_historical().
_HIST_PREFIXES = (
    "history of ", "hx of ", "h/o ", "status post ", "s/p ",
    "prior ", "previous ", "former ", "past ",
)


def _looks_historical(label: str | None, description: str | None) -> bool:
    s = (label or "").lower().strip()
    if any(s.startswith(p) for p in _HIST_PREFIXES):
        return True
    if "(history" in s or "(hx" in s or "(prior" in s or "(former" in s:
        return True
    d = (description or "").lower().strip()
    if d in ("history_of", "resolved"):
        return True
    return False


async def main(
    user_id: Optional[uuid.UUID] = None,
    dry_run: bool = False,
) -> int:
    async with SessionLocal() as db:
        # Get all candidate facts first; user-scope by filtering against
        # the anchor->source chain in Python. The `&&` array-overlap
        # operator can't compare uuid[] with the subquery result here.
        user_anchor_ids: set[uuid.UUID] | None = None
        if user_id is not None:
            anchor_rows = list((await db.execute(
                select(EvidenceAnchor.id)
                .join(
                    SourceDocument,
                    SourceDocument.id == EvidenceAnchor.source_document_id,
                )
                .where(SourceDocument.owner_user_id == user_id)
            )).scalars().all())
            user_anchor_ids = set(anchor_rows)
        q = (
            select(ExtractedFact)
            .where(ExtractedFact.extraction_method == "claude_clinical_note_v1")
            .where(ExtractedFact.fact_type.in_(("condition", "procedure")))
        )
        all_rows = list((await db.execute(q)).scalars().all())
        if user_anchor_ids is None:
            rows = all_rows
        else:
            rows = [
                f for f in all_rows
                if any(aid in user_anchor_ids for aid in (f.evidence_anchor_ids or []))
            ]

    print(f"Considering {len(rows)} candidate facts…")

    touched = 0
    already = 0
    skipped_not_historical = 0
    async with SessionLocal() as db:
        for f in rows:
            if not _looks_historical(f.label, f.description):
                skipped_not_historical += 1
                continue
            cc = dict(f.coded_concepts or {})
            if cc.get("historical") is True and cc.get("date_origin") == "historical_undated":
                already += 1
                continue
            cc["historical"] = True
            cc["date_origin"] = "historical_undated"
            # If the fact had an explicit date from the LLM (rare but
            # possible — "history of ACL repair, 2007" gives the LLM
            # a year), keep it; only NULL out the leak case where the
            # date matches the source's creation_date / acquired_at.
            if f.date_start is not None:
                # Find the source via first anchor and check its
                # creation date. If it matches f.date_start, the date
                # is the note-date leak we want to clear.
                source_creation = None
                source_acquired = None
                if f.evidence_anchor_ids:
                    a = await db.get(EvidenceAnchor, f.evidence_anchor_ids[0])
                    if a is not None:
                        s = await db.get(SourceDocument, a.source_document_id)
                        if s is not None:
                            rm = s.raw_metadata or {}
                            from datetime import datetime as _dt
                            if isinstance(rm.get("creation"), str):
                                try:
                                    source_creation = _dt.fromisoformat(
                                        rm["creation"].replace("Z", "+00:00")
                                    )
                                except ValueError:
                                    pass
                            source_acquired = s.acquired_at
                # If the fact's date matches the source-leak dates
                # (note creation or acquired_at) to the day, NULL it.
                if (
                    (source_creation
                     and f.date_start.date() == source_creation.date())
                    or (source_acquired
                        and f.date_start.date() == source_acquired.date())
                ):
                    f.date_start = None
                else:
                    # Real historical date — keep but mark provenance.
                    cc["date_origin"] = "explicit"
            f.coded_concepts = cc
            db.add(f)
            touched += 1
        if not dry_run:
            await db.commit()
        else:
            await db.rollback()
            print("(dry run — no changes committed)")

    print("=" * 60)
    print(
        f"{len(rows)} considered · "
        f"{touched} {'would-touch' if dry_run else 'touched'} · "
        f"{already} already-stamped · "
        f"{skipped_not_historical} not historical"
    )
    print("=" * 60)
    return touched


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--user", type=str, default=None, help="Restrict to one user UUID")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    uid: Optional[uuid.UUID] = None
    if args.user:
        try:
            uid = uuid.UUID(args.user)
        except ValueError:
            print(f"Invalid --user UUID: {args.user}", file=sys.stderr)
            sys.exit(2)
    asyncio.run(main(user_id=uid, dry_run=args.dry_run))
