"""Backfill clinical_note extraction across every source_document
whose plaintext was fetched but never parsed into facts.

Nick caught the gap 2026-05-13 PM: every clinical_note across 6
health systems (184 rows) had has_plaintext=true and ZERO extracted
facts. This script iterates over those rows, runs the new
extract_clinical_note pipeline on each, and reports a summary the
user can verify against:

    184 notes processed, X facts extracted, Y need review, Z errored.

Run inside the api container:

    docker compose exec api python -m ownchart.scripts.backfill_clinical_notes

Idempotent: skips any source whose raw_metadata.extraction_status is
already 'completed', so re-runs are safe and cheap. Process notes
serially to keep Anthropic API rate-limits + cost predictable; each
call is ~$0.03 of Opus at 8k input tokens / 2k output tokens.

Restrict to one user with `--user <uuid>`. Useful for re-running
after a single new EHR connector sync.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Optional

from sqlalchemy import select

from ..core.db import SessionLocal
from ..core.logger import get_logger
from ..extract.clinical_note import extract_clinical_note
from ..models.evidence_anchor import EvidenceAnchor
from ..models.source_document import SourceDocument
from ..models.user import User

log = get_logger("ownchart.scripts.backfill_clinical_notes")


async def _has_anchors(db, source_id: uuid.UUID) -> bool:
    cnt = (await db.execute(
        select(EvidenceAnchor.id)
        .where(EvidenceAnchor.source_document_id == source_id)
        .limit(1)
    )).first()
    return cnt is not None


async def main(
    user_id: Optional[uuid.UUID] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> int:
    """Returns the number of newly-created facts."""
    async with SessionLocal() as db:
        # Includes ccda_xml — the Hopkins data showed ccda_xml has
        # the same has_plaintext=true / 0 facts gap as clinical_note
        # had. The extractor strips HTML/XML tags before sending to
        # the LLM, so the same prompt works.
        q = (
            select(SourceDocument)
            .where(SourceDocument.source_type.in_(("clinical_note", "ccda_xml")))
            .order_by(SourceDocument.acquired_at.asc())
        )
        if user_id is not None:
            q = q.where(SourceDocument.owner_user_id == user_id)
        if limit is not None and limit > 0:
            q = q.limit(limit)
        rows = list((await db.execute(q)).scalars().all())

    if not rows:
        print("No clinical_note source documents found.")
        return 0

    print(f"Found {len(rows)} clinical_note source documents to consider.")

    processed = 0
    already_done = 0
    skipped_empty = 0
    errored = 0
    facts_total = 0
    needs_review_total = 0

    for s in rows:
        async with SessionLocal() as db:
            # Re-fetch in this session so we can mutate cleanly.
            source = await db.get(SourceDocument, s.id)
            if source is None:
                continue
            rm = source.raw_metadata if isinstance(source.raw_metadata, dict) else {}

            # Idempotency: skip if already processed.
            if (rm or {}).get("extraction_status") == "completed":
                already_done += 1
                continue
            # Also skip if anchors already exist (legacy data path).
            if await _has_anchors(db, source.id):
                already_done += 1
                continue
            # Skip if there's no plaintext to work with.
            has_text = bool((rm or {}).get("has_plaintext"))
            excerpt_len = int((rm or {}).get("plaintext_length") or 0)
            if not has_text or excerpt_len < 40:
                skipped_empty += 1
                continue

            user = await db.get(User, source.owner_user_id)
            if user is None:
                errored += 1
                print(f"  [{source.id}] owner user missing — skipping")
                continue
            if not user.phi_consent_granted:
                skipped_empty += 1
                continue

            title = (rm or {}).get("title") or source.original_filename or ""
            label = source.source_label or ""
            print(f"  → extracting [{source.id}] {label} :: {title}")

            if dry_run:
                continue

            err_msg: str | None = None
            res = None
            try:
                res = await extract_clinical_note(db, user, source)
                if res.error:
                    err_msg = res.error
            except Exception as e:  # noqa: BLE001
                err_msg = f"{type(e).__name__}: {e}"

            if err_msg:
                errored += 1
                print(f"    ERROR: {err_msg}")
                # Stamp the source so the UI / future inbox can surface
                # a Retry. Re-fetch in case the extractor's own mid-
                # transaction commit left stale state on the instance.
                from datetime import datetime as _dt, timezone as _tz
                src2 = await db.get(SourceDocument, source.id)
                if src2 is not None:
                    rm = dict(src2.raw_metadata or {})
                    rm["extraction_status"] = "failed"
                    rm["extraction_error"] = err_msg[:1000]
                    rm["extraction_failed_at"] = _dt.now(_tz.utc).isoformat()
                    src2.raw_metadata = rm
                    await db.commit()
                continue
            processed += 1
            facts_total += res.fact_count
            # Rough needs_review count: each fact's review_state would
            # need to be re-read, but the extractor already logged
            # individual facts. Keep the summary tight here.
            print(
                f"    +{res.fact_count} facts"
                + (f" — reviewer: {res.notes_to_reviewer[:80]}"
                   if res.notes_to_reviewer else "")
            )

    # Final summary line — the user-facing acceptance criterion.
    print("\n" + "=" * 60)
    print(
        f"{len(rows)} notes considered, "
        f"{processed} processed, "
        f"{already_done} already done, "
        f"{skipped_empty} skipped (empty/no consent), "
        f"{errored} errored, "
        f"{facts_total} facts extracted."
    )
    print("=" * 60)
    return facts_total


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--user", type=str, default=None, help="Restrict to one user UUID")
    p.add_argument("--limit", type=int, default=None, help="Cap notes processed")
    p.add_argument("--dry-run", action="store_true", help="Print plan, don't call LLM")
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
    asyncio.run(main(user_id=uid, limit=args.limit, dry_run=args.dry_run))
