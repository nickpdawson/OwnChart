"""Discover feed — deterministic V1 sensemaking observations.

Per docs/06 §122: Discover is the "what you didn't know to ask" surface.
Per Nick's milestone-1 brief: deterministic only — no LLM, no
correlation engine. The point of V1 is to prove the *shape* of the
feed; the eventual statistical and AI-narrated items will plug into
this same item contract.

Item contract (per docs/06 §143 + signal-strength refinement):

    {
      id: stable-id,
      type: "dense_period" | "long_gap" | "unreviewed_high_count",
      title: str,
      why_surfaced: str,            # one sentence, no LLM
      evidence_window: { start, end } | null,
      signal_strength: "strong" | "moderate" | "needs_review",
      suggested_action: str,
      action_href: str | null,
    }

`signal_strength` deliberately uses the same vocabulary the
correlation/AI layer will use (per docs/06 §237 "language rules").
Even when it's deterministic ("12 events vs baseline 1.3"), keeping
the label consistent means we don't have to retrain users when
statistical findings start appearing in the same feed.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.user import User
from .auth import get_current_user
from .timeline import WEARABLE_METHODS

router = APIRouter()

SignalStrength = Literal["strong", "moderate", "needs_review"]


class EvidenceWindow(BaseModel):
    start: datetime
    end: datetime


class DiscoverItem(BaseModel):
    id: str
    type: str
    title: str
    why_surfaced: str
    evidence_window: EvidenceWindow | None
    signal_strength: SignalStrength
    suggested_action: str
    action_href: str | None


class DiscoverResponse(BaseModel):
    items: list[DiscoverItem]


# Sort order across mixed signal_strengths — strong > moderate >
# needs_review. needs_review is a "look at this" label, not a "this
# is a confident finding" label, so it ranks last when the feed is
# truncated.
_STRENGTH_RANK = {"strong": 0, "moderate": 1, "needs_review": 2}


async def _dense_periods(db: AsyncSession) -> list[DiscoverItem]:
    """Years with clinical event counts well above the user's baseline.

    Tightened heuristic (UX polish 2026-05-10) — the prior rule was
    "every year is dense" when historical data is sparse and recent
    FHIR ingests dump thousands of facts into one year. Now requires:

      1. ≥ 3 years of data overall (need a baseline).
      2. Year has ≥ 50 clinical events absolutely (no calling a
         year "dense" with 5 events just because the rest are 0).
      3. Year is ≥ 3× the **trimmed mean** (lops off the top + bottom
         year so a single FHIR-dump year doesn't poison its own
         comparison).
      4. Strong = ≥ 200 events AND ≥ 5× trimmed mean.
      5. Moderate = ≥ 50 events AND ≥ 3× trimmed mean.

    If the trimmed mean is < 5, baseline is too sparse to detect
    density meaningfully — skip entirely.
    """
    bucket = func.date_trunc("year", ExtractedFact.date_start)
    rows = (await db.execute(
        select(bucket.label("yr"), func.count().label("n"))
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.extraction_method.notin_(WEARABLE_METHODS))
        .group_by("yr")
    )).all()

    counts = [(r.yr, int(r.n)) for r in rows if r.yr is not None]
    if len(counts) < 3:
        return []

    # Trimmed mean: drop the single max and single min so a recent
    # FHIR-ingest dump year doesn't define its own baseline. With
    # < 4 years we use plain median; with ≥ 4 we trim.
    n_values = sorted(n for _, n in counts)
    if len(n_values) >= 4:
        trimmed = n_values[1:-1]
        baseline = sum(trimmed) / len(trimmed)
    else:
        baseline = statistics.median(n_values)
    if baseline < 5:
        return []

    out: list[DiscoverItem] = []
    for yr, n in counts:
        # Absolute floor: a year needs ≥ 50 clinical events to even
        # be eligible for a "dense" label.
        if n < 50:
            continue
        ratio = n / baseline
        if ratio < 3:
            continue
        strength: SignalStrength = "strong" if (n >= 200 and ratio >= 5) else "moderate"
        year = yr.year
        end_year = yr.replace(year=year, month=12, day=31, hour=23, minute=59, second=59)
        out.append(
            DiscoverItem(
                id=f"dense_period:{year}",
                type="dense_period",
                title=f"Dense care period: {year}",
                why_surfaced=(
                    f"{n} clinical events in {year} — about {ratio:.1f}× your "
                    f"typical year ({baseline:.0f} on average)."
                ),
                evidence_window=EvidenceWindow(start=yr, end=end_year),
                signal_strength=strength,
                suggested_action="Open the period to see what happened.",
                # `#year-YYYY` lets the timeline page scroll the user
                # to the right card without us shipping a focus-state
                # query parameter (V1 deterministic UX — native browser
                # scroll, no JS).
                action_href=f"/timeline#year-{year}",
            )
        )
    # Most recent dense periods are usually most actionable; tiebreak
    # within strength by year desc.
    out.sort(key=lambda i: (
        _STRENGTH_RANK[i.signal_strength],
        -(i.evidence_window.start.year if i.evidence_window else 0),
    ))
    return out


async def _long_gaps(db: AsyncSession) -> list[DiscoverItem]:
    """Stretches of >12mo with no clinical events between dated facts.

    A gap is only meaningful if there's activity on both sides — a
    user with no records in 1990 isn't "missing" anything, they just
    weren't in the system. So we walk consecutive distinct dated
    fact dates and emit gaps between adjacent ones.
    """
    rows = (await db.execute(
        select(ExtractedFact.date_start)
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.extraction_method.notin_(WEARABLE_METHODS))
        .order_by(ExtractedFact.date_start.asc())
    )).all()
    dates = [r[0] for r in rows]
    if len(dates) < 2:
        return []

    out: list[DiscoverItem] = []
    prev = dates[0]
    for d in dates[1:]:
        delta = d - prev
        # 366 days is "more than a year" allowing for leap years.
        months = delta.days / 30.44
        if months >= 12:
            strength: SignalStrength = "strong" if months >= 24 else "moderate"
            ys, ms = prev.year, prev.month
            ye, me = d.year, d.month
            out.append(
                DiscoverItem(
                    id=f"long_gap:{ys}-{ms:02d}_{ye}-{me:02d}",
                    type="long_gap",
                    title=(
                        f"No clinical events: {prev.strftime('%b %Y')} → "
                        f"{d.strftime('%b %Y')}"
                    ),
                    why_surfaced=(
                        f"{months:.0f}-month gap between adjacent dated facts."
                    ),
                    evidence_window=EvidenceWindow(start=prev, end=d),
                    signal_strength=strength,
                    suggested_action=(
                        "Add a note for any care that wasn't ingested, or "
                        "import older documents."
                    ),
                    action_href=None,
                )
            )
        prev = d
    # Longest gaps first, then most-recent within strength.
    out.sort(key=lambda i: (
        _STRENGTH_RANK[i.signal_strength],
        -(i.evidence_window.end - i.evidence_window.start).days
        if i.evidence_window else 0,
    ))
    return out


async def _connected_episodes(db: AsyncSession) -> list[DiscoverItem]:
    """Detect "this is parts of one event, not separate life events."

    Heuristic (V1, on-read): facts grouped by `(UTC date, first-anchor
    source_document_id)`. If a date+source pair has ≥3 clinical facts
    AND at least one is fact_type='procedure', it's almost certainly
    a perioperative bundle (procedure + anesthesia + medications +
    encounter notes all dated the same day, all from one source).

    Per the product/design team example (2026-05-10):
      > "Your a recent eye surgery is now a connected episode.
      >  this EHR added multiple procedure entries on the
      >  same day. OwnChart thinks these are parts of one surgery
      >  event, not separate life events."

    R4 (this commit) detects on read; R4-followup will persist
    HealthEvent rows at ingest time so this groupwork happens once.
    FHIR-resource-ID-shaped labels are filtered so legacy garbage
    doesn't appear as the episode anchor.
    """
    # Pull dated clinical facts with their first anchor's
    # source_document_id. Excludes wearable methods and
    # template-noise review states.
    anchor_id = func.unnest(ExtractedFact.evidence_anchor_ids).label("anchor_id")
    sub = (
        select(
            ExtractedFact.id.label("fact_id"),
            ExtractedFact.fact_type.label("fact_type"),
            ExtractedFact.label.label("label"),
            ExtractedFact.significance.label("significance"),
            ExtractedFact.date_start.label("date_start"),
            anchor_id,
        )
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.fact_type.in_(
            ("procedure", "condition", "encounter", "medication", "observation")
        ))
        .where(ExtractedFact.review_state.notin_(("deferred", "rejected", "source_only")))
        .where(ExtractedFact.extraction_method.notin_(WEARABLE_METHODS))
        # Keep one anchor per fact — only need to know "which source
        # does this fact's first anchor point at."
        .subquery()
    )
    rows = (await db.execute(
        select(
            sub.c.fact_id,
            sub.c.fact_type,
            sub.c.label,
            sub.c.significance,
            sub.c.date_start,
            EvidenceAnchor.source_document_id.label("source_id"),
        )
        .join(EvidenceAnchor, EvidenceAnchor.id == sub.c.anchor_id)
    )).all()

    # Group by (date::date, source_id) → list of (fact_id, fact_type, label)
    # Use only the first-encountered anchor per fact, since the same
    # fact can have many anchor_ids and we don't want to over-count.
    from collections import defaultdict
    seen_fact_ids: set = set()
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r.fact_id in seen_fact_ids:
            continue
        seen_fact_ids.add(r.fact_id)
        date_key = r.date_start.astimezone(timezone.utc).date()
        groups[(date_key, r.source_id)].append({
            "fact_id": r.fact_id,
            "fact_type": r.fact_type,
            "label": r.label,
            "significance": r.significance,
            "date_start": r.date_start,
        })

    # Source label lookup — we want the source label not the UUID.
    source_ids = list({sid for (_, sid) in groups.keys() if sid is not None})
    src_labels: dict = {}
    if source_ids:
        s_q = await db.execute(
            select(SourceDocument.id, SourceDocument.source_label, SourceDocument.original_filename)
            .where(SourceDocument.id.in_(source_ids))
        )
        for sid, slabel, sfile in s_q.all():
            src_labels[sid] = (slabel or sfile or "this source")

    out: list[DiscoverItem] = []
    for (date, source_id), facts in groups.items():
        if len(facts) < 3:
            continue
        # Significance-ranked anchor (2026-05-11): require at least one
        # major_procedure or major_event to call it an episode. A
        # pile of routine medication refills + observations on one day
        # is NOT an episode; the a recent eye surgery is.
        major_anchors = [
            f for f in facts
            if f.get("significance") in ("major_procedure", "major_event")
            and f["label"] and not _looks_like_fhir_id(f["label"])
        ]
        if not major_anchors:
            continue
        primary = major_anchors[0]

        # Compose: title leads with the primary procedure label;
        # why_surfaced names the source + count + same-day shape.
        fact_count = len(facts)
        src_name = src_labels.get(source_id, "this source")
        primary_label = primary["label"]
        date_str = date.isoformat()
        # Soft-pluralize date for readability ("May 2026" beats
        # "2026-05-01" in narrative copy; keep the iso form as a
        # secondary cue in why_surfaced).
        date_pretty = datetime.combine(date, datetime.min.time()).strftime("%B %-d, %Y")

        strength: SignalStrength = "strong" if fact_count >= 5 else "moderate"

        # The action: open the primary procedure in the fact-context
        # sidesheet — that view's `related_facts` + `episode`
        # heuristic already lists the same-day-same-source bundle.
        # When R4 promotes episodes to a first-class surface, the
        # action_href becomes /episode/{id}; the sidesheet bridge is
        # the V1 path.
        action_href = f"/discover?fact={primary['fact_id']}"

        # Internal `type` stays `connected_episode` for clients that
        # already key off it; user-facing copy now reads "Event".
        out.append(DiscoverItem(
            id=f"connected_episode:{date_str}:{source_id}",
            type="connected_episode",
            title=f"Connected Event: {primary_label} ({date_pretty})",
            why_surfaced=(
                f"{src_name} added {fact_count} clinical facts on {date_str}. "
                f"OwnChart thinks these are parts of one Event, "
                f"not separate moments."
            ),
            evidence_window=EvidenceWindow(
                start=datetime.combine(date, datetime.min.time(), tzinfo=timezone.utc),
                end=datetime.combine(date, datetime.max.time(), tzinfo=timezone.utc),
            ),
            signal_strength=strength,
            suggested_action="Open the Event to see what happened.",
            action_href=action_href,
        ))

    # Sort newest first within strength tier, cap at 10 connected episodes.
    out.sort(key=lambda i: (
        _STRENGTH_RANK[i.signal_strength],
        -(i.evidence_window.start.timestamp() if i.evidence_window else 0),
    ))
    return out[:10]


_FHIR_ID_PATTERN = re.compile(
    r"^(Encounter|MedicationRequest|MedicationDispense|MedicationStatement|"
    r"Procedure|Condition|Observation|DiagnosticReport|AllergyIntolerance|"
    r"Immunization|Resource) [A-Za-z0-9._\-]{12,}$"
)


def _looks_like_fhir_id(label: str) -> bool:
    """Heuristic — true for legacy 'Encounter enZx7fBPAul...' rows
    that snuck in before connectors._label_for was patched."""
    return bool(_FHIR_ID_PATTERN.match(label or ""))


async def _unreviewed_high_counts(db: AsyncSession) -> list[DiscoverItem]:
    """Fact types with conspicuous needs_review backlog.

    Always uses the `needs_review` strength label — this isn't a
    "finding", it's a "look at this" pointer. Threshold of 5 keeps
    noise low for healthy backlogs.
    """
    rows = (await db.execute(
        select(ExtractedFact.fact_type, func.count().label("n"))
        .where(ExtractedFact.review_state == "needs_review")
        .group_by(ExtractedFact.fact_type)
        .having(func.count() >= 5)
        .order_by(func.count().desc())
    )).all()

    out: list[DiscoverItem] = []
    for ft, n in rows:
        n = int(n)
        # Strength escalates only for very large backlogs — but the
        # *label* stays needs_review either way (per the V1 contract:
        # this is attention, not assertion). The visual separation
        # between "20+ pile-up" and "5-19" can come from sort order
        # and a future urgency flag.
        out.append(
            DiscoverItem(
                id=f"needs_review:{ft}",
                type="unreviewed_high_count",
                title=f"{n} {ft.replace('_', ' ')}s need review",
                why_surfaced=(
                    f"{n} {ft.replace('_', ' ')} facts have review_state "
                    f"= needs_review."
                ),
                evidence_window=None,
                signal_strength="needs_review",
                suggested_action="Open the review inbox.",
                action_href=f"/review?fact_type={ft}",
            )
        )
    return out


@router.get("")
async def get_discover(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
    include_dismissed: bool = Query(default=False),
) -> DiscoverResponse:
    """Return the deterministic V1 discovery feed.

    Items are computed on-read from the underlying facts/sources,
    so there's no `discover_items` table; their IDs are content-
    derived (e.g. `connected_episode:{date}:{source_id}`). Per-user
    dismissals live in `audit_events`
    (event_type='discover_dismissed', subject_type='discover_item').
    """
    items: list[DiscoverItem] = []
    items.extend(await _connected_episodes(db))
    items.extend(await _dense_periods(db))
    items.extend(await _long_gaps(db))
    items.extend(await _unreviewed_high_counts(db))

    items.sort(key=lambda i: _STRENGTH_RANK[i.signal_strength])

    if not include_dismissed:
        from ..models.audit_event import AuditEvent as _AE
        dismissed_ids = set((await db.execute(
            select(_AE.subject_id)
            .where(_AE.user_id == user.id)
            .where(_AE.event_type == "discover_dismissed")
            .where(_AE.subject_type == "discover_item")
        )).scalars().all())
        items = [i for i in items if i.id not in dismissed_ids]

    return DiscoverResponse(items=items[:limit])


@router.post("/{item_id:path}/dismiss", status_code=204)
async def dismiss_discover_item(
    item_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Persist a per-user dismiss for a discover item.

    `item_id` is the content-derived string (e.g.
    `connected_episode:2026-05-01:<source_uuid>`), so it can contain
    colons — hence the `:path` converter. Idempotent: re-dismissing
    is a no-op via the existing audit-row uniqueness check at read
    time (we just write another row; the read-time filter dedupes).
    """
    from ..models.audit_event import AuditEvent
    from datetime import timezone as _tz
    db.add(AuditEvent(
        user_id=user.id,
        event_type="discover_dismissed",
        subject_type="discover_item",
        subject_id=item_id[:128],
        detail={"item_id": item_id},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    ))
    await db.commit()


@router.post("/{item_id:path}/undismiss", status_code=204)
async def undismiss_discover_item(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Reverse a prior dismiss. Writes a 'discover_undismissed' audit
    row that suppresses the dismiss at read time."""
    from ..models.audit_event import AuditEvent
    from sqlalchemy import delete
    # Hard-delete the dismiss audit rows for this item — undismiss is
    # rare, so cleaning up is preferable to layered tombstones.
    await db.execute(
        delete(AuditEvent)
        .where(AuditEvent.user_id == user.id)
        .where(AuditEvent.event_type == "discover_dismissed")
        .where(AuditEvent.subject_type == "discover_item")
        .where(AuditEvent.subject_id == item_id[:128])
    )
    await db.commit()
