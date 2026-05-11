"""Global timeline endpoint — buckets of clinical / wearable / source density.

Per docs/06 §99: the global timeline is the main chronological map.
Topic dossiers are *lenses* over this map, not the map itself. So this
endpoint queries the global evidence graph (extracted_facts +
source_documents) directly, not via topics.

Per Nick's milestone-1 brief: distinguish lanes (don't collapse into a
single undifferentiated event count). Three lanes:

- **Clinical** — extracted_facts where extraction_method != 'health_auto_export'.
  Grouped by fact_type so the bucket can show "2 procedures, 4 encounters".
- **Wearable** — extracted_facts where extraction_method == 'health_auto_export'.
  Grouped by the same normalized-label prefix the dossier clusters use,
  so the bucket can show "Heart rate: 27,832, Steps: 365".
- **Source/doc** — source_documents in the window. Dated by
  coalesce(captured_at, user_supplied_event_date, acquired_at) so
  a 2014 fax uploaded in 2026 lands in 2014, not 2026.

V1 is deterministic and infrastructure-light: SQL aggregates only, no
LLM, no zoom controls. Year and month grains.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.user import User
from .auth import get_current_user
# Reuse the dossier route's cluster_id derivation so a cluster has the
# same id whether surfaced via a topic dossier or a global timeline
# period drill. Pure function, safe to import across routes.
from .topics import _cluster_id_for, _clean_cluster_header

router = APIRouter()

Grain = Literal["year", "month", "week"]

# Wearable / quantified-self extraction methods. These land in the
# wearable lane on the global timeline (vs the clinical lane). Per
# docs/07 §680: native HealthKit must be included alongside Auto
# Export. Garmin / Oura / Whoop / Fitbit go here as they land.
WEARABLE_METHODS = ("health_auto_export", "native_healthkit")


class LaneClinical(BaseModel):
    event_count: int
    by_type: dict[str, int]


class LaneWearable(BaseModel):
    fact_count: int
    # Top wearable label-prefixes in this bucket, sorted by count desc.
    # Frontend picks how many to render; we don't pre-group into
    # heart_rate/sleep/steps because the prefix vocabulary is open
    # (any new Auto Export metric should appear without a code change).
    by_metric: dict[str, int]


class LaneSource(BaseModel):
    document_count: int
    by_type: dict[str, int]


class NarrativeEvent(BaseModel):
    """One care-meaningful event the year's narrative may name."""

    fact_type: str          # procedure | condition | encounter
    label: str
    display_label: str | None = None
    source_label: str | None


class NarrativeImport(BaseModel):
    """An import / source contribution worth mentioning in the year's
    narrative when no genuine care events are present."""

    source_label: str
    fact_count: int


class BucketNarrative(BaseModel):
    """Patient-meaningful summary of a time bucket.

    `kind` is the editorial classification — UI uses it to choose
    voice. Three values, in order of confidence:

      - "care_meaningful": at least one procedure / condition /
        meaningful encounter happened. `summary` names what
        happened ("Your example surgery, follow-up encounters").
        `top_events` carries the underlying events.
      - "data_dense": the bucket has substantial data (FHIR import,
        HealthKit backfill, etc.) but OwnChart can't yet identify
        what care happened. `summary` is plain-spoken about the
        import: "A large EHR import and HealthKit backfill
        landed here. OwnChart hasn't yet surfaced the care story."
      - "quiet": low data and no signal. `summary` admits it:
        "OwnChart hasn't yet narrated this year."

    Critical constraint (per product/design team, 2026-05-10):
    NEVER imply a year is clinically meaningful just because the
    database is dense. Care density ≠ data density.
    """

    # Added 2026-05-11: "mixed" for buckets that have BOTH a named
    # care event AND substantial import volume — UI shows both
    # signals (saturn/mixed visual state) without overpromising.
    kind: Literal["care_meaningful", "mixed", "data_dense", "quiet"]
    summary: str
    top_events: list[NarrativeEvent]
    top_imports: list[NarrativeImport]


class BucketEpisode(BaseModel):
    """A canonical Episode that falls in this bucket's date range.

    Added 2026-05-11 PM: when the user has promoted candidates into
    Episodes, the year/period card surfaces them as durable markers
    instead of just floating clinical facts.
    """

    id: str
    title: str
    kind: str
    date_start: datetime | None
    date_end: datetime | None


class TimelineBucket(BaseModel):
    start: datetime
    end: datetime
    grain: Grain
    clinical: LaneClinical
    wearable: LaneWearable
    source: LaneSource
    narrative: BucketNarrative | None = None
    episodes: list[BucketEpisode] = []

    @property
    def total(self) -> int:
        return self.clinical.event_count + self.wearable.fact_count + self.source.document_count


class TimelineResponse(BaseModel):
    grain: Grain
    range_start: datetime
    range_end: datetime
    buckets: list[TimelineBucket]


def _bucket_label(start: datetime, grain: Grain) -> str:
    """Human-readable bucket name for narrative copy. Year-grain
    returns the year; finer grains include the month."""
    if grain == "year":
        return str(start.year)
    if grain == "month":
        return start.strftime("%B %Y")
    return start.strftime("%b %d, %Y")


def _derive_narrative(slot: dict, grain: Grain) -> "BucketNarrative":
    """Classify a time bucket and emit patient-meaningful narrative copy.

    Three-way classification — care_meaningful > data_dense > quiet.

    The product/design team constraint (2026-05-10): care density is
    NOT data density. A year is not meaningful just because 1,000
    FHIR rows landed in it. Only assert care meaning when we can
    name a procedure / condition / encounter event. Otherwise be
    plain-spoken about the import shape, or admit the absence.
    """
    bucket_name = _bucket_label(slot["start"], grain)
    top_events: list[dict] = slot["top_events"]
    top_imports: list[dict] = slot["top_imports"]
    clinical_total = slot["clinical_count"]
    wearable_total = slot["wearable_count"]
    source_total = slot["source_count"]

    def _fmt_event_phrase(ev: dict) -> str:
        # "Your example surgery" if a source label is attached;
        # otherwise just the label. Prefer the candidate display_label
        # (R5) when present — patient-readable beats SNOMED-jargon
        # in narrative copy.
        nm = ev.get("display_label") or ev["label"]
        if ev["source_label"]:
            return f"{nm} at {ev['source_label']}"
        return nm

    # CARE-MEANINGFUL or MIXED: there's at least one real care event we
    # can name. If the bucket also has heavy import/wearable volume,
    # flag as "mixed" so the UI can show both signals without
    # promising the year was clinically dramatic.
    if top_events:
        # Cap the year subtitle per Nick (Q4): long prose makes the
        # timeline worse. Lead with the single top event; supporting
        # events live in `top_events[]` for the card body, not the
        # subtitle prose.
        head = _fmt_event_phrase(top_events[0])
        if len(top_events) > 1:
            head += f" (+{len(top_events) - 1} more)"

        also_dense = (
            wearable_total >= 1000
            or source_total >= 5
            or (top_imports and top_imports[0]["fact_count"] >= 50)
        )
        if also_dense:
            kind: Literal["care_meaningful", "mixed", "data_dense", "quiet"] = "mixed"
            tail = " · plus assembled data"
        else:
            kind = "care_meaningful"
            tail = ""
        summary = f"{bucket_name}: {head}{tail}"
        return BucketNarrative(
            kind=kind,
            summary=summary,
            top_events=[
                NarrativeEvent(
                    fact_type=ev["fact_type"],
                    label=ev["label"],
                    display_label=ev.get("display_label"),
                    source_label=ev["source_label"],
                )
                for ev in top_events[:3]
            ],
            top_imports=[
                NarrativeImport(source_label=im["source_label"], fact_count=im["fact_count"])
                for im in top_imports[:2]
            ],
        )

    # DATA-DENSE: the bucket has substantial data but we can't name a
    # specific care event. Be honest about what landed — Stanford
    # import, HealthKit backfill, etc. — without implying care happened.
    data_dense = (
        wearable_total >= 1000
        or source_total >= 5
        or (top_imports and top_imports[0]["fact_count"] >= 50)
    )
    if data_dense:
        bits: list[str] = []
        if top_imports:
            names = [
                f"{im['source_label']} import ({im['fact_count']:,} facts)"
                for im in top_imports[:2]
            ]
            bits.append(" and ".join(names))
        if wearable_total >= 1000:
            bits.append(f"a HealthKit / Auto Export backfill ({wearable_total:,} readings)")
        if not bits and source_total > 0:
            bits.append(f"{source_total} source document{'s' if source_total != 1 else ''}")
        landed = " and ".join(bits) if bits else "data"
        summary = (
            f"{bucket_name}: {landed} landed here. "
            f"OwnChart hasn't yet surfaced specific care events."
        )
        return BucketNarrative(
            kind="data_dense",
            summary=summary,
            top_events=[],
            top_imports=[
                NarrativeImport(source_label=im["source_label"], fact_count=im["fact_count"])
                for im in top_imports[:2]
            ],
        )

    # QUIET: low data, no signal. Admit it plainly.
    return BucketNarrative(
        kind="quiet",
        summary=f"{bucket_name}: OwnChart hasn't yet narrated this period.",
        top_events=[],
        top_imports=[],
    )


def _bucket_end(start: datetime, grain: Grain) -> datetime:
    """Right edge of the bucket. Used for display and for the period
    drill on the frontend (clicking a year shows that year's window)."""
    if grain == "year":
        return start.replace(year=start.year + 1) - timedelta(microseconds=1)
    if grain == "month":
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1) - timedelta(microseconds=1)
        return start.replace(month=start.month + 1) - timedelta(microseconds=1)
    # week
    return start + timedelta(days=7) - timedelta(microseconds=1)


def _norm_label_expr():
    """Same normalization the dossier cluster route uses.

    Heart rate: 72 bpm → "heart rate". CCDA / vision labels (no colon)
    pass through whitespace-collapsed and lower-cased.
    """
    return func.lower(
        func.trim(
            func.regexp_replace(
                func.split_part(ExtractedFact.label, ":", 1), r"\s+", " ", "g"
            )
        )
    )


def _representative_label_expr():
    """Pick a presentable form of the prefix (Title Case-ish).

    The normalization expression lower-cases for grouping. To render
    "Heart rate" in the response (not "heart rate") we keep the
    original split_part output and just trim/collapse whitespace.
    """
    return func.trim(
        func.regexp_replace(
            func.split_part(ExtractedFact.label, ":", 1), r"\s+", " ", "g"
        )
    )


def _source_event_date_expr():
    """A source document's "when" for timeline purposes.

    captured_at (EXIF) > user_supplied_event_date > acquired_at.
    Without the coalesce, a 2014 fax uploaded in 2026 would land in
    2026's bucket, which is wrong for life-view sensemaking.
    """
    return func.coalesce(
        SourceDocument.captured_at,
        SourceDocument.user_supplied_event_date,
        SourceDocument.acquired_at,
    )


@router.get("")
async def get_timeline(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    from_: datetime | None = Query(default=None, alias="from"),
    to_: datetime | None = Query(default=None, alias="to"),
    grain: Grain = Query(default="year"),
) -> TimelineResponse:
    """Bucketed event/density counts for the global timeline.

    The defaults (when from/to are omitted) span from the earliest
    dated fact to now, capped at a sane lower bound so a brand-new
    install with one 1980s fact doesn't render 45 empty buckets.
    """
    if grain not in ("year", "month", "week"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid grain")

    # Resolve window. We pull the earliest fact date if `from` was
    # omitted; if there are no dated facts at all, return an empty
    # response rather than guess a year.
    now = datetime.now(timezone.utc)
    if from_ is None:
        earliest = (await db.execute(
            select(func.min(ExtractedFact.date_start))
        )).scalar_one_or_none()
        from_ = earliest or now
    if to_ is None:
        to_ = now
    if from_ > to_:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from > to")

    bucket_expr_facts = func.date_trunc(grain, ExtractedFact.date_start)
    src_date = _source_event_date_expr()
    bucket_expr_sources = func.date_trunc(grain, src_date)

    # 1. Clinical lane: extracted_facts grouped by (bucket, fact_type),
    #    excluding wearable methods. NULL date_start drops naturally —
    #    undated clinical facts don't belong on the timeline.
    clin_q = await db.execute(
        select(
            bucket_expr_facts.label("bucket"),
            ExtractedFact.fact_type,
            func.count().label("n"),
        )
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= from_)
        .where(ExtractedFact.date_start <= to_)
        .where(ExtractedFact.extraction_method.notin_(WEARABLE_METHODS))
        .group_by("bucket", ExtractedFact.fact_type)
    )

    # 2. Wearable lane: extracted_facts grouped by (bucket, normalized
    #    label prefix), only wearable methods. Representative label is
    #    a max() of the cleaned-but-cased prefix — within a single
    #    metric the casing is consistent so this is stable.
    norm = _norm_label_expr()
    rep = _representative_label_expr()
    wear_q = await db.execute(
        select(
            bucket_expr_facts.label("bucket"),
            norm.label("metric_key"),
            func.max(rep).label("metric_label"),
            func.count().label("n"),
        )
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= from_)
        .where(ExtractedFact.date_start <= to_)
        .where(ExtractedFact.extraction_method.in_(WEARABLE_METHODS))
        .group_by("bucket", norm)
    )

    # 3. Source lane: source_documents grouped by (bucket, source_type)
    #    using the coalesced event date.
    src_q = await db.execute(
        select(
            bucket_expr_sources.label("bucket"),
            SourceDocument.source_type,
            func.count().label("n"),
        )
        .where(SourceDocument.owner_user_id == user.id)
        .where(src_date.isnot(None))
        .where(src_date >= from_)
        .where(src_date <= to_)
        .group_by("bucket", SourceDocument.source_type)
    )

    # 4. Top care-meaningful events per bucket, RANKED BY SIGNIFICANCE
    #    (2026-05-11). The a recent eye surgery should dominate over
    #    cold-sore refills and routine problem-list entries.
    #    DISTINCT ON keeps one row per (bucket, label) so repeated daily
    #    medications don't crowd out actual care events; defense-in-
    #    depth FHIR-resource-ID filter stays in case ingest leaks.
    from ..canonical.significance import MAJOR as _SIG_MAJOR
    bad_label_pattern = r"^(Encounter|MedicationRequest|MedicationDispense|MedicationStatement|Procedure|Condition|Observation|DiagnosticReport|AllergyIntolerance|Immunization|Resource) [A-Za-z0-9._\-]{12,}$"
    sig_rank_expr = case(
        (ExtractedFact.significance == "major_event", 1),
        (ExtractedFact.significance == "major_procedure", 2),
        (ExtractedFact.significance == "major_diagnosis", 3),
        (ExtractedFact.significance == "major_medication", 4),
        (ExtractedFact.significance == "major_activity_lifestyle", 5),
        else_=99,
    )
    event_q = await db.execute(
        select(
            bucket_expr_facts.label("bucket"),
            ExtractedFact.fact_type,
            ExtractedFact.label,
            ExtractedFact.display_label,
            ExtractedFact.significance,
            ExtractedFact.evidence_anchor_ids,
        )
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= from_)
        .where(ExtractedFact.date_start <= to_)
        .where(ExtractedFact.significance.in_(_SIG_MAJOR))
        .where(ExtractedFact.review_state.notin_(("deferred", "rejected", "source_only")))
        .where(~ExtractedFact.label.op("~")(bad_label_pattern))
        .order_by(bucket_expr_facts.desc(), sig_rank_expr.asc(), ExtractedFact.confidence.desc().nullslast())
    )
    event_rows = event_q.all()

    # 5. Top source-label contributions per bucket. Used when a year
    #    is data-dense but has no specific care events — the narrative
    #    leans on what landed instead of pretending care happened.
    #    Subquery unnests each fact's evidence_anchor_ids so we can
    #    JOIN against evidence_anchors → source_documents, same
    #    pattern routes/topics.py uses for cluster source counts.
    contrib_anchor_id = func.unnest(ExtractedFact.evidence_anchor_ids).label("anchor_id")
    contrib_sub = (
        select(
            bucket_expr_facts.label("bucket"),
            ExtractedFact.id.label("fact_id"),
            contrib_anchor_id,
        )
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= from_)
        .where(ExtractedFact.date_start <= to_)
        .subquery()
    )
    src_contrib_q = await db.execute(
        select(
            contrib_sub.c.bucket,
            SourceDocument.source_label,
            SourceDocument.original_filename,
            func.count(func.distinct(contrib_sub.c.fact_id)).label("n"),
        )
        .join(EvidenceAnchor, EvidenceAnchor.id == contrib_sub.c.anchor_id)
        .join(SourceDocument, SourceDocument.id == EvidenceAnchor.source_document_id)
        .where(SourceDocument.owner_user_id == user.id)
        .group_by(contrib_sub.c.bucket, SourceDocument.source_label, SourceDocument.original_filename)
        .order_by(contrib_sub.c.bucket.desc(), func.count(func.distinct(contrib_sub.c.fact_id)).desc())
    )
    contrib_rows = src_contrib_q.all()

    # Anchor-to-source resolution for top events (so the narrative can
    # name "Your example surgery" instead of just "Eye surgery").
    # Collect first-anchor IDs across all event rows, batch-resolve.
    first_anchor_ids: list = []
    for r in event_rows:
        if r.evidence_anchor_ids:
            first_anchor_ids.append(r.evidence_anchor_ids[0])
    anchor_to_source_label: dict = {}
    if first_anchor_ids:
        a_q = await db.execute(
            select(EvidenceAnchor.id, SourceDocument.source_label, SourceDocument.original_filename)
            .join(SourceDocument, SourceDocument.id == EvidenceAnchor.source_document_id)
            .where(EvidenceAnchor.id.in_(first_anchor_ids))
        )
        for aid, slabel, sfile in a_q.all():
            anchor_to_source_label[aid] = (slabel or sfile)

    # Merge into a single buckets dict keyed on the truncated start.
    buckets: dict[datetime, dict] = {}

    def _slot(bucket_start: datetime) -> dict:
        # Postgres returns timezone-aware datetimes from date_trunc on
        # tz-aware columns; normalize to UTC for stable map keys.
        if bucket_start.tzinfo is None:
            bucket_start = bucket_start.replace(tzinfo=timezone.utc)
        if bucket_start not in buckets:
            buckets[bucket_start] = {
                "start": bucket_start,
                "clinical_count": 0,
                "clinical_by_type": {},
                "wearable_count": 0,
                "wearable_by_metric": {},
                "source_count": 0,
                "source_by_type": {},
                "top_events": [],
                "top_imports": [],
            }
        return buckets[bucket_start]

    for row in clin_q.all():
        s = _slot(row.bucket)
        s["clinical_count"] += int(row.n)
        s["clinical_by_type"][row.fact_type] = (
            s["clinical_by_type"].get(row.fact_type, 0) + int(row.n)
        )

    for row in wear_q.all():
        s = _slot(row.bucket)
        s["wearable_count"] += int(row.n)
        # Use the representative-label form (e.g. "Heart rate") for the
        # display key; if that's empty, fall back to the lower-cased
        # normalized key.
        key = (row.metric_label or "").strip() or (row.metric_key or "(unlabeled)")
        s["wearable_by_metric"][key] = (
            s["wearable_by_metric"].get(key, 0) + int(row.n)
        )

    for row in src_q.all():
        s = _slot(row.bucket)
        s["source_count"] += int(row.n)
        s["source_by_type"][row.source_type] = (
            s["source_by_type"].get(row.source_type, 0) + int(row.n)
        )

    # Top care-meaningful events per bucket — cap at 3, dedupe by label.
    for row in event_rows:
        s = _slot(row.bucket)
        if len(s["top_events"]) >= 3:
            continue
        if any(ev["label"] == row.label for ev in s["top_events"]):
            continue
        first_anchor = (row.evidence_anchor_ids or [None])[0]
        source_label = anchor_to_source_label.get(first_anchor) if first_anchor else None
        s["top_events"].append({
            "fact_type": row.fact_type,
            "label": row.label,
            "display_label": row.display_label,
            "source_label": source_label,
        })

    # Top imports per bucket — cap at 2. Skip rows with no
    # source_label AND no original_filename (nothing to name).
    for row in contrib_rows:
        s = _slot(row.bucket)
        if len(s["top_imports"]) >= 2:
            continue
        name = (row.source_label or row.original_filename or "").strip()
        if not name:
            continue
        s["top_imports"].append({
            "source_label": name,
            "fact_count": int(row.n),
        })

    # Canonical episodes that fall inside the window — bucket them
    # by start date so each year/period card carries its episode set.
    from ..models.episode import Episode
    ep_rows = list((await db.execute(
        select(Episode)
        .where(Episode.user_id == user.id)
        .where(Episode.date_start.isnot(None))
        .where(Episode.date_start >= from_)
        .where(Episode.date_start <= to_)
        .order_by(Episode.date_start.asc())
    )).scalars().all())
    eps_by_bucket: dict[datetime, list[BucketEpisode]] = {}
    for ep in ep_rows:
        if ep.date_start is None:
            continue
        key_dt = ep.date_start
        if key_dt.tzinfo is None:
            key_dt = key_dt.replace(tzinfo=timezone.utc)
        if grain == "year":
            bk = key_dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif grain == "month":
            bk = key_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            bk = key_dt - timedelta(days=key_dt.weekday())
            bk = bk.replace(hour=0, minute=0, second=0, microsecond=0)
        eps_by_bucket.setdefault(bk, []).append(BucketEpisode(
            id=str(ep.id),
            title=ep.title,
            kind=ep.kind,
            date_start=ep.date_start,
            date_end=ep.date_end,
        ))

    out: list[TimelineBucket] = []
    for start in sorted(buckets.keys()):
        s = buckets[start]
        narrative = _derive_narrative(s, grain)
        out.append(
            TimelineBucket(
                start=s["start"],
                end=_bucket_end(s["start"], grain),
                grain=grain,
                clinical=LaneClinical(
                    event_count=s["clinical_count"],
                    by_type=s["clinical_by_type"],
                ),
                wearable=LaneWearable(
                    fact_count=s["wearable_count"],
                    by_metric=s["wearable_by_metric"],
                ),
                source=LaneSource(
                    document_count=s["source_count"],
                    by_type=s["source_by_type"],
                ),
                narrative=narrative,
                episodes=eps_by_bucket.get(s["start"], []),
            )
        )

    return TimelineResponse(
        grain=grain,
        range_start=from_,
        range_end=to_,
        buckets=out,
    )


# ---------------------------------------------------------------------------
# Period drill — clicking a bucket on the timeline opens this view
# ---------------------------------------------------------------------------


class PeriodEvent(BaseModel):
    id: str
    fact_type: str
    label: str
    display_label: str | None = None
    date_start: datetime | None
    extraction_method: str
    review_state: str
    source_id: str | None
    source_anchor_id: str | None


class PeriodCluster(BaseModel):
    """Cluster of clinical facts inside a period.

    Mirrors the dossier's `FactCluster` shape so the visual contract
    on the frontend is the same: card with date range + counts +
    needs-review badge, click to expand to underlying events. The
    `cluster_id` matches the dossier's cluster_id derivation so a
    "Finasteride 1mg" cluster has the same id whether surfaced via a
    topic dossier or via this period drill.
    """

    cluster_id: str
    fact_type: str
    label: str  # representative
    fact_count: int
    source_count: int
    needs_review_count: int
    date_start_min: datetime | None
    date_start_max: datetime | None


class PeriodSource(BaseModel):
    id: str
    source_type: str
    original_filename: str | None
    source_label: str | None
    event_date: datetime  # the coalesced date used to place it on the timeline


class PeriodResponse(BaseModel):
    range_start: datetime
    range_end: datetime
    # Clinical lane: rolled-up clusters, not a flat fact list.
    # 4,000+ medication administrations in a year would otherwise
    # silently truncate at any flat-list cap *and* drown out the
    # actual non-medication clinical events the user wants to see.
    clinical_clusters: list[PeriodCluster]
    wearable_summary: LaneWearable
    sources: list[PeriodSource]


# Per-period caps. Year-grain windows can have hundreds of distinct
# clusters in pathological cases; the cap keeps the payload bounded
# while ordering ensures the most-actionable ones (needs-review then
# high fact_count) survive any clipping.
_PERIOD_CLUSTERS_CAP = 200
_PERIOD_SOURCES_CAP = 100
_PERIOD_WEARABLE_TOP = 12
# Default cap for /period/cluster/facts. Same rationale as the
# dossier's cluster-expand: 500 most-recent facts is plenty for
# casual drilling; pagination/Discover handles the long tail.
_PERIOD_CLUSTER_FACTS_DEFAULT = 500
_PERIOD_CLUSTER_FACTS_MAX = 2000


@router.get("/period")
async def get_timeline_period(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    start: datetime = Query(...),
    end: datetime = Query(...),
) -> PeriodResponse:
    """Cross-lane detail for a window. The "click a dense period" drill.

    Returns clinical clusters (rolled-up via the same grouping rule
    the dossier uses), a wearable aggregate summary, and source
    documents. Per docs/06: this is the cross-lane test — clusters,
    metrics, and evidence side-by-side for the same window.
    """
    if start > end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start > end")

    norm = _norm_label_expr()
    base_clinical = (
        ExtractedFact.date_start.isnot(None),
        ExtractedFact.date_start >= start,
        ExtractedFact.date_start <= end,
        ExtractedFact.extraction_method.notin_(WEARABLE_METHODS),
        ExtractedFact.review_state.notin_(("deferred", "rejected")),
    )

    # Cluster aggregates: count, date range, needs-review-count, and a
    # representative label via array_agg(label ORDER BY length DESC).
    cluster_q = await db.execute(
        select(
            ExtractedFact.fact_type,
            norm.label("norm_label"),
            func.count().label("fact_count"),
            func.count()
                .filter(ExtractedFact.review_state == "needs_review")
                .label("needs_review_count"),
            func.min(ExtractedFact.date_start).label("dmin"),
            func.max(ExtractedFact.date_start).label("dmax"),
            (
                func.array_agg(
                    ExtractedFact.label,
                    order_by=func.length(ExtractedFact.label).desc().nullslast(),
                )
            ).label("labels_by_length"),
        )
        .where(*base_clinical)
        .group_by(ExtractedFact.fact_type, norm)
    )
    cluster_rows = cluster_q.all()

    # Distinct source counts per cluster: unnest(evidence_anchor_ids)
    # JOIN evidence_anchors, group on the same key. Subquery so the
    # WHERE clauses match the cluster aggregate scope exactly.
    anchor_id = func.unnest(ExtractedFact.evidence_anchor_ids).label("anchor_id")
    sub = (
        select(
            ExtractedFact.fact_type.label("ft"),
            norm.label("nl"),
            anchor_id,
        )
        .where(*base_clinical)
        .subquery()
    )
    src_q = await db.execute(
        select(
            sub.c.ft,
            sub.c.nl,
            func.count(func.distinct(EvidenceAnchor.source_document_id)).label("src_count"),
        )
        .join(EvidenceAnchor, EvidenceAnchor.id == sub.c.anchor_id)
        .group_by(sub.c.ft, sub.c.nl)
    )
    src_by_key: dict[tuple[str, str], int] = {
        (ft, nl): int(c) for (ft, nl, c) in src_q.all()
    }

    clusters_out: list[PeriodCluster] = []
    for r in cluster_rows:
        rep = ""
        if r.labels_by_length:
            for candidate in r.labels_by_length:
                if candidate:
                    rep = candidate
                    break
        clusters_out.append(
            PeriodCluster(
                cluster_id=_cluster_id_for(r.fact_type, r.norm_label or ""),
                fact_type=r.fact_type,
                label=_clean_cluster_header(rep) or rep or (r.norm_label or ""),
                fact_count=int(r.fact_count),
                source_count=src_by_key.get((r.fact_type, r.norm_label or ""), 0),
                needs_review_count=int(r.needs_review_count),
                date_start_min=r.dmin,
                date_start_max=r.dmax,
            )
        )

    # Surface attention-worthy clusters first, then largest, with a
    # latest-date tiebreak. Same ordering rule the dossier uses.
    clusters_out.sort(
        key=lambda c: (
            -c.needs_review_count,
            -c.fact_count,
            -(c.date_start_max.timestamp() if c.date_start_max else 0),
            c.label.lower(),
        )
    )
    clusters_out = clusters_out[:_PERIOD_CLUSTERS_CAP]

    # Wearable aggregate — same as before. Top N metrics by count.
    rep_expr = _representative_label_expr()
    w_rows = (await db.execute(
        select(
            norm.label("metric_key"),
            func.max(rep_expr).label("metric_label"),
            func.count().label("n"),
        )
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= start)
        .where(ExtractedFact.date_start <= end)
        .where(ExtractedFact.extraction_method.in_(WEARABLE_METHODS))
        .group_by(norm)
        .order_by(func.count().desc())
        .limit(_PERIOD_WEARABLE_TOP)
    )).all()
    wear_total = sum(int(r.n) for r in w_rows)
    wear_by_metric: dict[str, int] = {}
    for r in w_rows:
        key = (r.metric_label or "").strip() or (r.metric_key or "(unlabeled)")
        wear_by_metric[key] = int(r.n)
    wearable_summary = LaneWearable(fact_count=wear_total, by_metric=wear_by_metric)

    # Sources in window — unchanged.
    src_date = _source_event_date_expr()
    s_rows = (await db.execute(
        select(SourceDocument, src_date.label("event_date"))
        .where(SourceDocument.owner_user_id == user.id)
        .where(src_date.isnot(None))
        .where(src_date >= start)
        .where(src_date <= end)
        .order_by(src_date.asc())
        .limit(_PERIOD_SOURCES_CAP)
    )).all()
    sources = [
        PeriodSource(
            id=str(s.id),
            source_type=s.source_type,
            original_filename=s.original_filename,
            source_label=s.source_label,
            event_date=ed,
        )
        for s, ed in s_rows
    ]

    return PeriodResponse(
        range_start=start,
        range_end=end,
        clinical_clusters=clusters_out,
        wearable_summary=wearable_summary,
        sources=sources,
    )


class NotableEvent(BaseModel):
    """A single dated clinical event surfaced on Home / strips.

    The user's example: "I had eye surgery on May 1, 2026." That fact
    should be visible at a glance from Home, deep-linkable to the
    year-card on /timeline, and (eventually) to a focused episode page.

    Ranked by `significance` (2026-05-11) — major_* facts dominate over
    routine problem-list entries and cold-sore refills. Source_only
    facts are excluded entirely from this feed.
    """

    id: str
    label: str
    display_label: str | None = None
    fact_type: str
    significance: str | None = None
    date_start: datetime
    source_id: str | None
    source_label: str | None


class NotableEventsResponse(BaseModel):
    events: list[NotableEvent]


# Fact types that carry timeline weight on their own — procedures and
# surgeries are the canonical "I want to see this moment" events.
# Conditions and encounters land here too but with lighter visual
# treatment on the frontend.
_NOTABLE_FACT_TYPES: tuple[str, ...] = (
    "procedure",
    "condition",
    "encounter",
)


@router.get("/notable-events")
async def get_notable_events(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=8, ge=1, le=50),
) -> NotableEventsResponse:
    """Top N dated clinical events ranked by significance + date.

    Significance ranking (2026-05-11): major_event > major_procedure >
    major_diagnosis > major_medication > major_activity_lifestyle.
    source_only and background facts are excluded — the May 1 eye
    surgery should dominate over cold-sore refills.
    """
    from ..canonical.significance import MAJOR

    # Defense-in-depth: even with significance set, exclude FHIR-ID
    # fallback labels so a botched ingest doesn't reach Home.
    bad_label = ExtractedFact.label.op("~")(r"^(Encounter|MedicationRequest|MedicationDispense|MedicationStatement|Procedure|Condition|Observation|DiagnosticReport|AllergyIntolerance|Immunization|Resource) [A-Za-z0-9._\-]{12,}$")

    # Sort by significance rank (lower = more prominent), then by
    # date desc. case() gives major_event=1 ... major_activity=5.
    sig_rank = case(
        (ExtractedFact.significance == "major_event", 1),
        (ExtractedFact.significance == "major_procedure", 2),
        (ExtractedFact.significance == "major_diagnosis", 3),
        (ExtractedFact.significance == "major_medication", 4),
        (ExtractedFact.significance == "major_activity_lifestyle", 5),
        else_=99,
    )

    rows = (await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.significance.in_(MAJOR))
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.review_state.notin_(("deferred", "rejected", "source_only")))
        .where(~bad_label)
        .order_by(sig_rank.asc(), ExtractedFact.date_start.desc())
        .limit(limit)
    )).scalars().all()

    # Resolve first-anchor → source for each. Batched, same pattern
    # the period drill uses, so we can show "from KP HealthSummary."
    anchor_ids: list = []
    for f in rows:
        if f.evidence_anchor_ids:
            anchor_ids.append(f.evidence_anchor_ids[0])
    anchor_to_source_id: dict = {}
    if anchor_ids:
        anc_q = await db.execute(
            select(EvidenceAnchor.id, EvidenceAnchor.source_document_id)
            .where(EvidenceAnchor.id.in_(anchor_ids))
        )
        anchor_to_source_id = {aid: sid for (aid, sid) in anc_q.all()}
    src_ids = list(set(anchor_to_source_id.values()))
    src_by_id: dict = {}
    if src_ids:
        s_q = await db.execute(
            select(SourceDocument).where(SourceDocument.id.in_(src_ids))
        )
        src_by_id = {s.id: s for s in s_q.scalars().all()}

    events: list[NotableEvent] = []
    for f in rows:
        first_anchor = (f.evidence_anchor_ids or [None])[0]
        sid = anchor_to_source_id.get(first_anchor) if first_anchor else None
        source = src_by_id.get(sid) if sid else None
        events.append(
            NotableEvent(
                id=str(f.id),
                label=f.label,
                display_label=f.display_label,
                fact_type=f.fact_type,
                significance=f.significance,
                date_start=f.date_start,
                source_id=str(sid) if sid else None,
                source_label=(
                    (source.source_label or source.original_filename)
                    if source
                    else None
                ),
            )
        )
    return NotableEventsResponse(events=events)


@router.get("/period/cluster/facts")
async def get_period_cluster_facts(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    start: datetime = Query(...),
    end: datetime = Query(...),
    cluster_id: str = Query(...),
    limit: int = Query(default=_PERIOD_CLUSTER_FACTS_DEFAULT, ge=1, le=_PERIOD_CLUSTER_FACTS_MAX),
) -> list[PeriodEvent]:
    """Expand one cluster within a period — return its facts, newest first.

    Cluster_id is resolved by re-running the same GROUP BY the period
    endpoint uses, then matching the hash. Same trick the dossier
    cluster-expand uses; cheap aggregate (~bounded number of rows)
    plus one bounded fact fetch.
    """
    if start > end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start > end")

    norm = _norm_label_expr()
    base_clinical = (
        ExtractedFact.date_start.isnot(None),
        ExtractedFact.date_start >= start,
        ExtractedFact.date_start <= end,
        ExtractedFact.extraction_method.notin_(WEARABLE_METHODS),
        ExtractedFact.review_state.notin_(("deferred", "rejected")),
    )

    key_q = await db.execute(
        select(ExtractedFact.fact_type, norm.label("norm"))
        .where(*base_clinical)
        .group_by(ExtractedFact.fact_type, norm)
    )
    match: tuple[str, str] | None = None
    for ft, nl in key_q.all():
        if _cluster_id_for(ft, nl or "") == cluster_id:
            match = (ft, nl or "")
            break
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found in period")

    fact_rows = (await db.execute(
        select(ExtractedFact)
        .where(*base_clinical)
        .where(ExtractedFact.fact_type == match[0])
        .where(norm == match[1])
        .order_by(
            ExtractedFact.date_start.desc().nullslast(),
            ExtractedFact.created_at.desc(),
        )
        .limit(limit)
    )).scalars().all()

    # Anchor-to-source resolution for view-source links.
    anchor_ids: list = []
    for f in fact_rows:
        if f.evidence_anchor_ids:
            anchor_ids.append(f.evidence_anchor_ids[0])
    anchor_to_source: dict = {}
    if anchor_ids:
        a_q = await db.execute(
            select(EvidenceAnchor.id, EvidenceAnchor.source_document_id)
            .where(EvidenceAnchor.id.in_(anchor_ids))
        )
        anchor_to_source = {aid: sid for (aid, sid) in a_q.all()}

    events = []
    for f in fact_rows:
        first_anchor = (f.evidence_anchor_ids or [None])[0]
        events.append(
            PeriodEvent(
                id=str(f.id),
                fact_type=f.fact_type,
                label=f.label,
                display_label=f.display_label,
                date_start=f.date_start,
                extraction_method=f.extraction_method,
                review_state=f.review_state,
                source_id=str(anchor_to_source.get(first_anchor))
                if first_anchor and anchor_to_source.get(first_anchor)
                else None,
                source_anchor_id=str(first_anchor) if first_anchor else None,
            )
        )
    return events
