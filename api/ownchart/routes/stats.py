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

from ..core.db import get_session
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.topic import Topic
from ..models.user import User
from .auth import get_current_user

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
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> DashboardStats:
    # Source count (excluding postgres-internal stuff if any).
    source_count = (
        await db.execute(select(func.count(SourceDocument.id)))
    ).scalar_one()

    # Fact counts by review_state.
    by_state = dict(
        (
            await db.execute(
                select(ExtractedFact.review_state, func.count(ExtractedFact.id))
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

    # Topics with per-topic fact counts using the same retrieval rule
    # the dossier uses (alias substring + label_patterns). For the
    # dashboard summary we use a cheap heuristic: ILIKE any alias.
    # Exact match parity with the dossier resolver isn't required;
    # this is just a rough "how big is each topic".
    topic_rows = (await db.execute(select(Topic).order_by(Topic.name))).scalars().all()
    topic_snaps: list[TopicSnapshot] = []
    for t in topic_rows:
        terms = [t.name, *(t.aliases or [])]
        if not terms:
            count = 0
        else:
            from sqlalchemy import or_

            filters = []
            for term in terms:
                if not term:
                    continue
                pat = f"%{term}%"
                filters.append(ExtractedFact.label.ilike(pat))
                filters.append(ExtractedFact.description.ilike(pat))
            for rx in t.label_patterns or []:
                if not rx:
                    continue
                filters.append(ExtractedFact.label.op("~*")(rx))
                filters.append(ExtractedFact.description.op("~*")(rx))
            if filters:
                count = (await db.execute(
                    select(func.count(ExtractedFact.id))
                    .where(or_(*filters))
                    .where(ExtractedFact.review_state.notin_(("deferred", "rejected")))
                )).scalar_one()
            else:
                count = 0
        topic_snaps.append(
            TopicSnapshot(
                id=str(t.id),
                slug=t.slug,
                name=t.name,
                description=t.description,
                fact_count=count,
            )
        )

    # Recent sources (last 5 by acquired_at).
    recent_rows = (
        await db.execute(
            select(SourceDocument)
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
