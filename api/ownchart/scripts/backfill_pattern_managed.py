"""Backfill: deferred → pattern_managed for past pattern accepts.

Up through 2026-05-15, accepting a medication_pattern / provider_pattern
candidate flipped member facts to `deferred` — which hid them from
Ask / EI / Timeline / Dossiers in addition to the Review Inbox.

After Nick's correction (2026-05-15), pattern accepts flip to a new
`pattern_managed` state that's invisible to the Inbox but visible
everywhere else. This script finds facts that were flipped via a
historical pattern accept and re-flips them.

The audit log records the lineage: every pattern accept that flipped
facts wrote an AuditEvent with event_type='pattern_managed_suppression'
and detail.fact_count + detail.pattern_key. The candidates themselves
hold the fact_ids list. We walk the audit-log → candidate → fact_ids
chain and flip any of those facts that are still `deferred`.

Safety:
  - Only touches facts whose id appears in an accepted pattern
    candidate's fact_ids.
  - Only re-flips facts currently in `deferred`. Doesn't touch
    rejected / source_only / confirmed / etc.
  - Idempotent — running twice changes nothing on the second pass.

Run with:
  docker compose exec api python -m ownchart.scripts.backfill_pattern_managed
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update

from ..core.db import SessionLocal
from ..models.audit_event import AuditEvent
from ..models.extracted_fact import ExtractedFact
from ..models.sensemaking_candidate import SensemakingCandidate


async def main() -> None:
    async with SessionLocal() as db:
        # Find every candidate that was accepted with pattern semantics.
        # `disposition='accepted'` + candidate_type in patterns is
        # sufficient — we don't need to walk audit events.
        accepted = list((await db.execute(
            select(SensemakingCandidate)
            .where(SensemakingCandidate.candidate_type.in_(
                ("medication_pattern", "provider_pattern")
            ))
            .where(SensemakingCandidate.disposition == "accepted")
        )).scalars().all())

        if not accepted:
            print("No accepted pattern candidates found. Nothing to backfill.")
            return

        all_fact_ids: set = set()
        for c in accepted:
            for fid in (c.fact_ids or []):
                all_fact_ids.add(fid)

        print(
            f"Found {len(accepted)} accepted pattern candidates referencing "
            f"{len(all_fact_ids)} distinct fact ids."
        )

        # Re-flip ONLY rows currently in 'deferred'. Anything that's
        # been touched since (rejected, source_only, confirmed by a
        # later user action) is left alone — preserves the user's
        # explicit overrides.
        result = await db.execute(
            update(ExtractedFact)
            .where(ExtractedFact.id.in_(all_fact_ids))
            .where(ExtractedFact.review_state == "deferred")
            .values(review_state="pattern_managed")
        )
        flipped = result.rowcount or 0
        now = datetime.now(timezone.utc)
        db.add(AuditEvent(
            user_id=accepted[0].user_id,
            event_type="pattern_managed_backfill",
            subject_type="extracted_fact",
            subject_id=None,
            detail={
                "candidates_walked": len(accepted),
                "fact_ids_considered": len(all_fact_ids),
                "facts_flipped": flipped,
                "ran_at": now.isoformat(),
            },
        ))
        await db.commit()
        print(f"Flipped {flipped} facts from 'deferred' → 'pattern_managed'.")


if __name__ == "__main__":
    asyncio.run(main())
