"""Topic resolver — find ExtractedFacts that belong to a topic.

V1 strategy (no embeddings yet):
  - Match `label` against topic.name + topic.aliases via pg_trgm similarity.
  - Match `coded_concepts` against `topic.related_concepts` for direct hits.
  - Combine + dedupe.

Default queries exclude `deferred` (operational/template noise) and
`rejected` (user dismissed) facts. Both surfaces have an `include_archived`
escape hatch for users who want to see everything.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.extracted_fact import ExtractedFact
from ..models.topic import Topic

log = get_logger("ownchart.retrieval.topics")


# Significance rank for ORDER BY — lower number = higher priority.
# Matches the canonical taxonomy in canonical/significance.py. Used by
# search_facts to surface major events ahead of background HK chatter
# when the query's tokens match across thousands of rows.
_SIGNIFICANCE_RANK = case(
    (ExtractedFact.significance == "major_event", 1),
    (ExtractedFact.significance == "major_procedure", 2),
    (ExtractedFact.significance == "major_diagnosis", 3),
    (ExtractedFact.significance == "major_medication", 4),
    (ExtractedFact.significance == "major_activity_lifestyle", 5),
    (ExtractedFact.significance == "background", 6),
    else_=7,
)


def _pattern_managed_fact_ids_subq(user_id: uuid.UUID):
    """Subquery returning IDs of facts that were suppressed via
    accepted pattern compression. Used to LIFT the deferred-state
    filter for those facts: the user accepted the pattern as a
    review-burden reduction, not as "pretend this medication doesn't
    exist", so retrieval should still find them when chat asks.

    Returns a subquery suitable for ANY(...) / IN comparisons.
    """
    from ..models.sensemaking_candidate import SensemakingCandidate
    return (
        select(func.unnest(SensemakingCandidate.fact_ids))
        .where(SensemakingCandidate.user_id == user_id)
        .where(SensemakingCandidate.disposition == "accepted")
        .where(SensemakingCandidate.candidate_type.in_(
            ("medication_pattern", "provider_pattern"),
        ))
        .scalar_subquery()
    )

# Review states that disappear from default dossier + retrieval views.
# `source_only` (docs/07 §644-649) means the fact is preserved as
# source context but not promoted to the life graph — same hiding
# treatment as `deferred` / `rejected`, semantically a third bucket.
_HIDDEN_STATES = ("deferred", "rejected", "source_only")

# Common English stopwords — strip from /ask queries before tokenized
# retrieval so "how many surgeries have I had?" doesn't try
# to substring-match the whole sentence.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "about", "after", "all", "am", "an", "and", "any", "are", "as",
        "at", "be", "because", "been", "before", "being", "between", "both",
        "but", "by", "can", "could", "did", "do", "does", "doing", "during",
        "each", "few", "for", "from", "further", "had", "has", "have", "having",
        "he", "her", "here", "hers", "herself", "him", "himself", "his", "how",
        "i", "if", "in", "into", "is", "it", "its", "itself", "just", "many",
        "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "of",
        "off", "on", "once", "only", "or", "other", "our", "ours", "ourselves",
        "out", "over", "own", "same", "she", "should", "so", "some", "such",
        "than", "that", "the", "their", "theirs", "them", "themselves", "then",
        "there", "these", "they", "this", "those", "through", "to", "too",
        "under", "until", "up", "very", "was", "we", "were", "what", "when",
        "where", "which", "while", "who", "whom", "why", "will", "with", "would",
        "you", "your", "yours", "yourself", "yourselves",
        # Question-shaped fillers
        "tell", "show", "explain", "story", "summary", "summarize",
        # Common verbs that leak into description ILIKE matches and pull
        # high-significance unrelated facts to the top. "Do I take
        # creatine?" was matching every Omeprazole/Celebrex/Finasteride
        # description containing "Take 1 tablet by mouth..." — and the
        # significance-rank ORDER BY pushed major_medication ahead of
        # the background-significance creatine. Caught 2026-05-13 PM.
        "take", "takes", "taking", "took",
        "use", "uses", "using", "used",
        "get", "gets", "getting", "got", "gotten",
        "make", "makes", "making", "made",
        "currently", "ever", "lately", "recently", "still",
    }
)


def _tokenize_query(query: str, min_len: int = 3) -> list[str]:
    """Split a free-text question into salient search tokens.

    Drops stopwords and short tokens; lowercases. Keeps medical-style
    multi-word phrases as separate tokens (the OR match below catches
    each piece — over-retrieval is fine, the LLM filters down).
    """
    raw = re.split(r"[^\w]+", query.lower())
    return [t for t in raw if len(t) >= min_len and t not in _STOPWORDS]


def topic_membership_clause(topic: Topic):
    """Return the SQL OR-clause that decides whether a fact is on-topic.

    Exposed so callers can reuse the predicate in aggregation queries
    (cluster counts) without re-fetching the ORM rows. Returns ``None``
    if the topic has no aliases / patterns to match on — caller should
    treat that as "no facts".

    Membership combines three signals (OR-matched):

    1. **Long alias substring** (≥6 chars) — case-insensitive ILIKE.
       Cheap and broad; safe at this length.
    2. **Short alias word-bounded** (3–5 chars) — POSIX regex with
       `\\m...\\M` word anchors. "ACL" matches "ACL repair" but not
       "Octreotide acetate"; "OA" alone is BELOW the 3-char floor
       and silently dropped. Without this guard, a 2-char alias
       like "OA" did a 1.9s parallel seq scan over 1.7M rows
       (caught 2026-05-15 PM on /dossier/left-knee).
    3. **Label patterns** (`label_patterns`) — Postgres POSIX regex
       (`~*`). Catches vocabulary classes the alias path misses,
       e.g. "Left lateral rectus recession 5 mm" without the word
       eye.

    Aliases under 3 characters are dropped entirely with a logged
    warning — they were almost certainly user typos or stub entries.
    """
    terms = [topic.name, *topic.aliases]
    filters = []
    dropped: list[str] = []
    for t in terms:
        if not t:
            continue
        s = t.strip()
        if len(s) < 3:
            dropped.append(s)
            continue
        if len(s) <= 5:
            # Word-bounded regex so short tokens stay precise.
            # Postgres `~*` is case-insensitive POSIX; `\m`/`\M` mark
            # word start / end (Postgres extension). Escape any
            # regex meta-chars the user might have typed.
            esc = re.sub(r"([.+*?^$()\[\]{}|\\])", r"\\\1", s)
            rx = r"\m" + esc + r"\M"
            filters.append(ExtractedFact.label.op("~*")(rx))
            filters.append(ExtractedFact.description.op("~*")(rx))
        else:
            pattern = f"%{s}%"
            filters.append(ExtractedFact.label.ilike(pattern))
            filters.append(ExtractedFact.description.ilike(pattern))
    if dropped:
        log.warning(
            "topic_alias_too_short",
            topic=topic.slug,
            dropped=dropped,
            note="aliases <3 chars cause ILIKE seq-scan explosions; rename them",
        )
    for rx in topic.label_patterns or []:
        if not rx:
            continue
        filters.append(ExtractedFact.label.op("~*")(rx))
        filters.append(ExtractedFact.description.op("~*")(rx))
    if not filters:
        return None
    return or_(*filters)


def hidden_review_states() -> tuple[str, ...]:
    return _HIDDEN_STATES


async def facts_for_topic(
    db: AsyncSession,
    topic: Topic,
    limit: int = 200,
    include_archived: bool = False,
    include_source_only: bool = False,
    person_record_id: uuid.UUID | None = None,
) -> list[ExtractedFact]:
    """Return facts that belong to a topic. See ``topic_membership_clause``.

    `include_source_only=False` (default, 2026-05-11): also hide facts
    whose `significance='source_only'`. The source detail page is the
    only surface that opts in via its "show source-only" toggle.

    M02 perimeter (Batch 5): when `person_record_id` is supplied,
    constrain matches to that record's facts. Post-migration 0032
    Topics already carry person_record_id, so the topic_id itself
    is record-scoped; this clause is defense-in-depth against future
    cross-record alias collisions.
    """
    clause = topic_membership_clause(topic)
    if clause is None:
        return []
    stmt = (
        select(ExtractedFact)
        .where(clause)
        .order_by(ExtractedFact.date_start.asc().nullslast(), ExtractedFact.created_at.desc())
        .limit(limit)
    )
    if person_record_id is not None:
        stmt = stmt.where(ExtractedFact.person_record_id == person_record_id)
    if not include_archived:
        stmt = stmt.where(ExtractedFact.review_state.notin_(_HIDDEN_STATES))
    if not include_source_only:
        stmt = stmt.where(
            or_(
                ExtractedFact.significance.is_(None),
                ExtractedFact.significance != "source_only",
            )
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


# Category words that map to ExtractedFact.fact_type values. When the
# user's question mentions any of these — "what medications do I take",
# "list my procedures" — substring match alone fails because the fact
# labels are concrete ("Finasteride 1mg Oral tablet") not categorical.
# This map lets us broaden retrieval to *all facts of that type*, then
# de-dup to one representative per distinct label.
_FACT_TYPE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "medication": (
        "medication", "medications", "med", "meds",
        "drug", "drugs", "prescription", "prescriptions",
        "rx", "pill", "pills",
    ),
    "symptom": ("symptom", "symptoms"),
    "procedure": (
        "procedure", "procedures", "surgery", "surgeries",
        "operation", "operations",
    ),
    "condition": (
        "condition", "conditions",
        "diagnosis", "diagnoses",
        "disease", "diseases",
    ),
    "encounter": (
        "encounter", "encounters",
        "visit", "visits",
    ),
    "observation": (
        "observation", "observations", "vital", "vitals",
        # FU-ASK-RECENT-WEARABLE (Beta 1, 2026-05-22) — wearable
        # vocabulary expansion. The user's natural language for
        # HealthKit / Auto Export data is "sleep", "HRV", "heart rate",
        # "training", "workout", "steps", "activity" — none of these
        # mapped to the observation bucket before, so a wearable
        # question never triggered the category-aware pass and
        # search_facts returned 40 random samples ordered by trigram
        # similarity. Phrases-with-spaces ("resting heart rate",
        # "heart rate variability") are matched whole by the
        # multi-word branch of _detect_category_fact_types.
        "sleep", "sleeping", "slept",
        "hrv", "heart rate variability",
        "heart rate", "resting heart rate", "resting hr",
        "rhr", "pulse",
        "workout", "workouts", "training", "trained", "exercise",
        "exercises", "exercising",
        "steps", "step count",
        "activity", "activities",
        "calories", "energy",
    ),
    "lab_result": ("lab", "labs", "labwork", "bloodwork"),
    "imaging_study": ("imaging", "scan", "scans", "xray", "x-ray", "mri", "ct"),
    "provider_relationship": ("provider", "providers", "doctor", "doctors", "physician", "physicians"),
    # 2026-05-16: vision-extracted vaccination cards produce facts
    # with brand-name labels (`COMIRNATY 2025-2026 PFIZER ...`,
    # `FLUARIX 2025-2026 GSK ...`) that don't contain the tokens
    # `covid` / `flu` / `vaccine`. Without category-aware retrieval,
    # the question "list my covid vaccinations" tokens-match zero
    # rows. Adding the full alias set so any vaccine question pulls
    # the whole vaccination fact_type bucket.
    "vaccination": (
        "vaccine", "vaccines", "vaccination", "vaccinations",
        "vax", "immunization", "immunizations", "shot", "shots",
        "booster", "boosters",
    ),
    "allergy": ("allergy", "allergies", "allergic"),
    "appointment": (
        "appointment", "appointments", "scheduled",
        "upcoming", "next visit",
    ),
    "instruction": (
        "instruction", "instructions",
        "discharge", "take-home",
        "aftercare",
    ),
}
_WORD_TO_FACT_TYPE: dict[str, str] = {
    word: ft for ft, words in _FACT_TYPE_SYNONYMS.items() for word in words
}


def _detect_category_fact_types(tokens: list[str], raw_query: str) -> set[str]:
    """Return the set of fact_types implied by category words in the query.

    Matches lowercased single-token category words. Doesn't try to be
    clever about phrases — if the user's question contains "lab" or
    "lab work", both forms hit "lab" / "labwork" entries in the map.
    """
    found: set[str] = set()
    raw_lower = raw_query.lower()
    for tok in tokens:
        if tok in _WORD_TO_FACT_TYPE:
            found.add(_WORD_TO_FACT_TYPE[tok])
    # Multi-word category phrases (e.g. "lab results", "x ray").
    for word, ft in _WORD_TO_FACT_TYPE.items():
        if " " in word and word in raw_lower:
            found.add(ft)
    return found


async def search_facts(
    db: AsyncSession,
    query: str,
    limit: int = 40,
    include_archived: bool = False,
    user_id: uuid.UUID | None = None,
    person_record_id: uuid.UUID | None = None,
) -> list[ExtractedFact]:
    """Free-text retrieval across fact labels + descriptions.

    Two-pass retrieval, results merged + de-duplicated:

    1. **Category-aware (fact_type-driven).** If the question mentions
       a category word ("medications", "procedures", "labs"), include
       *one representative fact per distinct label* of that type via
       PostgreSQL ``DISTINCT ON``. This is what makes "what medications
       do I take" work — the question contains zero specific drug
       names, so substring match alone misses every medication fact.
    2. **Substring match.** Per-token ILIKE across label + description,
       plus a phrase-match for the full raw query so exact medical
       strings ("optic atrophy") still hit as a unit.

    Order (2026-05-13 PM): both passes now sort by **significance rank
    first, date DESC second**. Caught during golden-path walk:
    "Tell me the story of my May 1 strabismus surgery" was returning
    1979 conditions because tokens like "story" / "surgery" matched
    thousands of background HK observations and the date-only
    ordering left the actual major_procedure facts past the 40-row
    cap. Significance-rank ordering surfaces the May 1 procedures
    first regardless of how dense the recent background chatter is.

    Pattern-managed visibility (2026-05-13 PM): when ``user_id`` is
    supplied, facts whose review_state='deferred' BECAUSE OF accepted
    pattern compression (medication_pattern / provider_pattern) are
    re-included in retrieval. The user accepted the pattern to reduce
    the inbox burden — not to make their medications invisible. The
    home insight referenced a recent Celebrex prescription and the
    follow-up chat couldn't find it; this lifts that gap.

    **M02 perimeter (Batch 4, 2026-05-18 PM):** when
    ``person_record_id`` is supplied, EVERY retrieval pass — category,
    substring, AND source-name expansion — is scoped to that record.
    This is load-bearing: ask.py, conversations.py, and topic
    rendering all feed the LLM context block from this function, so a
    missing filter here would put another patient's facts into a
    caregiver's prompt. Argument is keyword-only and defaults to None
    so legacy callers (admin scripts, no-record retrieval) keep
    working; the route layer is responsible for passing it on every
    user-facing path.

    Over-retrieval is fine — the LLM caller filters facts further by
    relevance; under-retrieval is fatal because no fact's label
    matches a full natural-language question.
    """
    if not query.strip():
        return []
    tokens = _tokenize_query(query)
    raw = query.strip()

    # State-filter helper — `deferred` is normally hidden, but for
    # callers that pass user_id we ALSO re-include facts that were
    # deferred via accepted pattern compression.
    def _state_filter():
        if include_archived:
            return None
        base = ExtractedFact.review_state.notin_(_HIDDEN_STATES)
        if user_id is None:
            return base
        return or_(base, ExtractedFact.id.in_(_pattern_managed_fact_ids_subq(user_id)))

    def _apply_record_scope(stmt):
        """M02 perimeter: scope SELECT to active person record.
        No-op when person_record_id is None (legacy callers)."""
        if person_record_id is None:
            return stmt
        return stmt.where(ExtractedFact.person_record_id == person_record_id)

    # --- Category representatives -----------------------------------------
    matched_types = _detect_category_fact_types(tokens, query)
    cat_facts: list[ExtractedFact] = []
    if matched_types:
        # DISTINCT ON (fact_type, lower(label)) gives one row per
        # unique label. ORDER BY significance rank + date DESC picks
        # the most clinically-significant administration as the
        # representative (a major_medication beats a background daily
        # vitamin log).
        cat_stmt = (
            select(ExtractedFact)
            .where(ExtractedFact.fact_type.in_(matched_types))
            .order_by(
                ExtractedFact.fact_type,
                func.lower(ExtractedFact.label),
                _SIGNIFICANCE_RANK,
                ExtractedFact.date_start.desc().nullslast(),
            )
            .distinct(ExtractedFact.fact_type, func.lower(ExtractedFact.label))
            .limit(limit)
        )
        cat_stmt = _apply_record_scope(cat_stmt)
        sf = _state_filter()
        if sf is not None:
            cat_stmt = cat_stmt.where(sf)
        cat_facts = list((await db.execute(cat_stmt)).scalars().all())

    # --- Substring match --------------------------------------------------
    filters = []
    if raw:
        pat = f"%{raw}%"
        filters.append(ExtractedFact.label.ilike(pat))
        filters.append(ExtractedFact.description.ilike(pat))
    for tok in tokens:
        pat = f"%{tok}%"
        filters.append(ExtractedFact.label.ilike(pat))
        filters.append(ExtractedFact.description.ilike(pat))

    substr_facts: list[ExtractedFact] = []
    if filters:
        sub_stmt = (
            select(ExtractedFact)
            .where(or_(*filters))
            .order_by(
                _SIGNIFICANCE_RANK,
                ExtractedFact.date_start.desc().nullslast(),
                ExtractedFact.created_at.desc(),
            )
            .limit(limit)
        )
        sub_stmt = _apply_record_scope(sub_stmt)
        sf = _state_filter()
        if sf is not None:
            sub_stmt = sub_stmt.where(sf)
        substr_facts = list((await db.execute(sub_stmt)).scalars().all())

    # --- Source-name expansion (2026-05-15 PM) ---------------------------
    # When the raw query mentions a known source by name ("Look at my
    # OrthoVirginia records", "What do Stanford notes say…"), retrieval
    # must reach facts that live UNDER that source — not just facts
    # whose label/description happens to contain the source name.
    # Without this, asking the chat to consult OrthoVirginia returned
    # 0 substantive facts because the source identity lives on
    # source_documents.source_label, not in extracted_facts.* text.
    source_facts: list[ExtractedFact] = []
    raw_lower = raw.lower()
    if raw_lower:
        from ..models.evidence_anchor import EvidenceAnchor
        from ..models.source_document import SourceDocument
        # Pull all distinct source_labels — small set (one row per
        # connected practice / upload origin) so this is cheap. M02
        # perimeter: when scoped, list only this record's sources so
        # we never match "Mom's Stanford notes" while serving a child.
        source_label_stmt = (
            select(SourceDocument.id, SourceDocument.source_label)
            .where(SourceDocument.source_label.isnot(None))
            .distinct()
        )
        if person_record_id is not None:
            source_label_stmt = source_label_stmt.where(
                SourceDocument.person_record_id == person_record_id,
            )
        label_rows = list((await db.execute(source_label_stmt)).all())
        # Match labels against the raw question. Strip whitespace +
        # case-fold; also try a no-whitespace form so "Ortho Virginia"
        # in the question hits "OrthoVirginia" in the data and vice
        # versa.
        matched_source_ids: list[uuid.UUID] = []
        seen_labels: set[str] = set()
        for sid, label in label_rows:
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            lab = label.lower().strip()
            lab_nospace = "".join(lab.split())
            raw_nospace = "".join(raw_lower.split())
            if (lab in raw_lower or lab_nospace in raw_nospace) and len(lab) >= 4:
                matched_source_ids.append(sid)
        if matched_source_ids:
            # Two-step: collect anchor_ids under matched sources, then
            # find facts whose evidence_anchor_ids array overlaps. The
            # `&&` array-overlap operator on a GIN-indexed text[] is
            # the canonical Postgres pattern for "facts that cite any
            # of these anchors."
            anchor_ids = list((await db.execute(
                select(EvidenceAnchor.id)
                .where(EvidenceAnchor.source_document_id.in_(matched_source_ids))
            )).scalars().all())
            if anchor_ids:
                fact_stmt = (
                    select(ExtractedFact)
                    .where(ExtractedFact.evidence_anchor_ids.op("&&")(anchor_ids))
                    .order_by(
                        _SIGNIFICANCE_RANK,
                        ExtractedFact.date_start.desc().nullslast(),
                    )
                    .limit(limit)
                )
                fact_stmt = _apply_record_scope(fact_stmt)
                sf = _state_filter()
                if sf is not None:
                    fact_stmt = fact_stmt.where(sf)
                source_facts = list((await db.execute(fact_stmt)).scalars().all())

    # Merge order:
    #   - **Source-name match wins first** when the user named a
    #     specific source — that's a direct instruction to look there.
    #   - Substring match comes next.
    #   - Category-representative facts fill remaining slots.
    #
    # When substring is empty (genuinely unspecific query like
    # "what medications am I on?"), fall back to category-rep facts.
    if source_facts:
        merge_order = source_facts + substr_facts + cat_facts
    elif substr_facts:
        merge_order = substr_facts + cat_facts
    else:
        merge_order = cat_facts + substr_facts

    # Source Authority Doctrine diversity guarantee (docs/SOURCE_
    # AUTHORITY_DOCTRINE.md): when a question pulls a broad result
    # set, reserve top-K slots so each authority tier present in the
    # match gets representation. Without this, date-DESC + significance
    # tiebreaks can completely crowd out an older specialty record by
    # newer copy-forward pre-op history. Caught 2026-05-15 PM:
    # "When was my last ACL surgery" returned 40 facts with 0 from
    # OrthoVirginia even though OV has the imaging + dx.
    if len(merge_order) > limit:
        merge_order = _ensure_tier_diversity(
            await _annotate_with_tiers(db, merge_order),
            limit=limit,
        )

    seen: dict = {}
    for f in merge_order:
        if f.id not in seen:
            seen[f.id] = f
        if len(seen) >= limit:
            break
    return list(seen.values())[:limit]


# Authority-tier ranking (mirrors llm/conversations.py::_TIER_RANK).
# Duplicated here to avoid a circular import; both modules update
# together — covered by the doctrine doc.
_AUTHORITY_RANK: dict[str, int] = {
    "primary_event":           1,
    "specialist_proximate":    2,
    "contemporaneous_support": 3,
    "ehr_summary":             4,
    "self_reported_history":   5,
    "model_inference":         6,
    "unknown":                 4,
}


async def _annotate_with_tiers(
    db: AsyncSession,
    facts: list[ExtractedFact],
) -> list[tuple[ExtractedFact, str]]:
    """Pair each fact with the authority tier of its primary anchor's
    source_document. Reads the persisted `authority_tier` from
    `raw_metadata` (stamped at ingest by `scripts/backfill_authority_
    tier.py` + the extractor). Missing tier → 'unknown'.
    """
    from ..models.evidence_anchor import EvidenceAnchor
    from ..models.source_document import SourceDocument
    anchor_ids: list[uuid.UUID] = []
    fact_anchor: dict[uuid.UUID, uuid.UUID] = {}
    for f in facts:
        if f.evidence_anchor_ids:
            aid = f.evidence_anchor_ids[0]
            anchor_ids.append(aid)
            fact_anchor[f.id] = aid
    if not anchor_ids:
        return [(f, "unknown") for f in facts]
    anchor_to_source = {
        aid: sid for (aid, sid) in (await db.execute(
            select(EvidenceAnchor.id, EvidenceAnchor.source_document_id)
            .where(EvidenceAnchor.id.in_(anchor_ids))
        )).all() if sid is not None
    }
    source_ids = list(set(anchor_to_source.values()))
    tier_by_source: dict[uuid.UUID, str] = {}
    if source_ids:
        rows = list((await db.execute(
            select(SourceDocument.id, SourceDocument.raw_metadata)
            .where(SourceDocument.id.in_(source_ids))
        )).all())
        for sid, rm in rows:
            tier = "unknown"
            if isinstance(rm, dict):
                t = rm.get("authority_tier")
                if isinstance(t, str):
                    tier = t
            tier_by_source[sid] = tier

    out: list[tuple[ExtractedFact, str]] = []
    for f in facts:
        aid = fact_anchor.get(f.id)
        sid = anchor_to_source.get(aid) if aid else None
        tier = tier_by_source.get(sid, "unknown") if sid else "unknown"
        out.append((f, tier))
    return out


def _ensure_tier_diversity(
    annotated: list[tuple[ExtractedFact, str]],
    *,
    limit: int,
) -> list[ExtractedFact]:
    """Round-robin pull by authority tier so every tier present in
    the result set gets representation in the top-K.

    Within a tier, preserve the original ordering (which is already
    significance + date + source-name-match weighted). Higher-authority
    tiers go FIRST in each round so the head of the list reads
    primary_event → specialist_proximate → ... rather than tier-mixed.
    """
    by_tier: dict[str, list[ExtractedFact]] = {}
    for f, tier in annotated:
        by_tier.setdefault(tier, []).append(f)
    # Sort tiers by authority rank (lower = higher authority).
    tiers_present = sorted(
        by_tier.keys(), key=lambda t: _AUTHORITY_RANK.get(t, 4),
    )
    out: list[ExtractedFact] = []
    # Pass 1: one fact per tier, highest-authority first, in rounds
    # until we hit limit or run out of facts.
    idx_per_tier: dict[str, int] = {t: 0 for t in tiers_present}
    while len(out) < limit:
        progressed = False
        for tier in tiers_present:
            if len(out) >= limit:
                break
            i = idx_per_tier[tier]
            bucket = by_tier[tier]
            if i < len(bucket):
                out.append(bucket[i])
                idx_per_tier[tier] = i + 1
                progressed = True
        if not progressed:
            break
    return out
