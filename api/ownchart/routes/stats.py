"""Aggregate counts for the dashboard.

Single endpoint so the dashboard renders fast and doesn't have to
fan out across listSources/listTopics/listFactsByState. Pure SELECT
COUNT(*) calls, no Anthropic, no expensive IO.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth_context import AuthContext, get_auth_context
from ..core.db import get_session
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.topic import Topic

router = APIRouter()


class TopicSnapshot(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None
    fact_count: int


class RecentSource(BaseModel):
    id: str
    source_type: str
    source_label: str | None
    original_filename: str | None
    acquired_at: datetime


class FactCounts(BaseModel):
    total: int
    confirmed: int
    needs_review: int
    deferred: int
    rejected: int


class DashboardStats(BaseModel):
    source_count: int
    facts: FactCounts
    topics: list[TopicSnapshot]
    recent_sources: list[RecentSource]


@router.get("")
async def get_dashboard_stats(
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> DashboardStats:
    # M02 perimeter (Batch 9): every aggregation below filters by
    # ctx.active_record_id. Pre-M02 this endpoint queried global
    # counts (no user scoping), so this batch closes a pre-existing
    # leak as it adds the record gate.

    # Source count (excluding postgres-internal stuff if any).
    source_count = (
        await db.execute(
            select(func.count(SourceDocument.id))
            .where(SourceDocument.person_record_id == ctx.active_record_id)
        )
    ).scalar_one()

    # Fact counts by review_state.
    by_state = dict(
        (
            await db.execute(
                select(ExtractedFact.review_state, func.count(ExtractedFact.id))
                .where(ExtractedFact.person_record_id == ctx.active_record_id)
                .group_by(ExtractedFact.review_state)
            )
        ).all()
    )
    facts = FactCounts(
        total=sum(by_state.values()),
        confirmed=by_state.get("confirmed", 0),
        needs_review=by_state.get("needs_review", 0),
        deferred=by_state.get("deferred", 0),
        rejected=by_state.get("rejected", 0),
    )

    # Topic snapshots: ship just the topic shells (id/slug/name/desc)
    # WITHOUT per-topic fact counts. The previous loop ran 8 sequential
    # OR-of-ILIKE scans across the full extracted_facts table and
    # dominated dashboard latency — 49s on Nick's record (~8k facts,
    # 8 topics). The dossier detail page recomputes the precise count
    # for one topic at a time, where it belongs.
    # RC fix 2026-05-14. If the count is needed back later, the right
    # shape is a precomputed topic_counts cache refreshed after
    # ingest/backfill, not a synchronous scan on every Home render.
    topic_rows = (
        await db.execute(
            select(Topic)
            .where(Topic.person_record_id == ctx.active_record_id)
            .order_by(Topic.name)
        )
    ).scalars().all()
    topic_snaps: list[TopicSnapshot] = [
        TopicSnapshot(
            id=str(t.id),
            slug=t.slug,
            name=t.name,
            description=t.description,
            fact_count=0,
        )
        for t in topic_rows
    ]

    # Recent sources (last 5 by acquired_at).
    recent_rows = (
        await db.execute(
            select(SourceDocument)
            .where(SourceDocument.person_record_id == ctx.active_record_id)
            .order_by(SourceDocument.acquired_at.desc())
            .limit(5)
        )
    ).scalars().all()
    recent = [
        RecentSource(
            id=str(s.id),
            source_type=s.source_type,
            source_label=s.source_label,
            original_filename=s.original_filename,
            acquired_at=s.acquired_at,
        )
        for s in recent_rows
    ]

    return DashboardStats(
        source_count=source_count,
        facts=facts,
        topics=topic_snaps,
        recent_sources=recent,
    )
