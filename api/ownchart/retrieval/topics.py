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

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.extracted_fact import ExtractedFact
from ..models.topic import Topic

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

    Membership combines two signals (OR-matched):

    1. **Alias substring** (`aliases` + topic name) — case-insensitive
       ILIKE match against label or description. Catches the easy case
       where the fact mentions the topic by name.
    2. **Label patterns** (`label_patterns`) — Postgres POSIX regex
       (`~*`) match against label or description. Catches vocabulary
       classes the alias path misses — e.g., a an operative
       report that says "Left lateral rectus recession 5 mm" without
       ever using the word "an eye condition".
    """
    terms = [topic.name, *topic.aliases]
    filters = []
    for t in terms:
        if not t:
            continue
        pattern = f"%{t}%"
        filters.append(ExtractedFact.label.ilike(pattern))
        filters.append(ExtractedFact.description.ilike(pattern))
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
) -> list[ExtractedFact]:
    """Return facts that belong to a topic. See ``topic_membership_clause``.

    `include_source_only=False` (default, 2026-05-11): also hide facts
    whose `significance='source_only'`. The source detail page is the
    only surface that opts in via its "show source-only" toggle.
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
        "visit", "visits", "appointment", "appointments",
    ),
    "observation": ("observation", "observations", "vital", "vitals"),
    "lab_result": ("lab", "labs", "labwork", "bloodwork"),
    "imaging_study": ("imaging", "scan", "scans", "xray", "x-ray", "mri", "ct"),
    "provider_relationship": ("provider", "providers", "doctor", "doctors", "physician", "physicians"),
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
) -> list[ExtractedFact]:
    """Free-text retrieval across fact labels + descriptions.

    Two-pass retrieval, results merged + de-duplicated:

    1. **Category-aware (fact_type-driven).** If the question mentions
       a category word ("medications", "procedures", "labs"), include
       *one representative fact per distinct label* of that type via
       PostgreSQL ``DISTINCT ON``. This is what makes "what medications
       do I take" work — the question contains zero specific drug
       names, so substring match alone misses every medication fact.
    2. **Substring match.** The original behavior. Per-token ILIKE
       across label + description, plus a phrase-match for the full
       raw query so exact medical strings ("optic atrophy") still
       hit as a unit.

    Order: category representatives first (so the prompt context
    leads with category breadth when relevant), then substring hits.
    Capped at ``limit`` total after de-dup by fact id.

    Over-retrieval is fine — the LLM caller filters facts further by
    relevance to the actual question; under-retrieval (the previous
    literal-substring-only behavior) is fatal because no fact's label
    matches a full natural-language question.
    """
    if not query.strip():
        return []
    tokens = _tokenize_query(query)
    raw = query.strip()

    # --- Category representatives -----------------------------------------
    matched_types = _detect_category_fact_types(tokens, query)
    cat_facts: list[ExtractedFact] = []
    if matched_types:
        # DISTINCT ON (fact_type, lower(label)) gives one row per
        # unique label. The label-prefix normalization the dossier
        # cluster route uses isn't needed here — medications already
        # share displayText across administrations, so lower(label) is
        # already the right grouping key. ORDER BY date_start DESC
        # picks the most recent administration as the representative.
        cat_stmt = (
            select(ExtractedFact)
            .where(ExtractedFact.fact_type.in_(matched_types))
            .order_by(
                ExtractedFact.fact_type,
                func.lower(ExtractedFact.label),
                ExtractedFact.date_start.desc().nullslast(),
            )
            .distinct(ExtractedFact.fact_type, func.lower(ExtractedFact.label))
            .limit(limit)
        )
        if not include_archived:
            cat_stmt = cat_stmt.where(
                ExtractedFact.review_state.notin_(_HIDDEN_STATES)
            )
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
        # Most-recent first (was ASC). 2026-05-13: Nick asked "tell me
        # about my broken fibula" and got back the 2005 ankle inversion
        # but none of the 2023 fibula fractures — they exist in the DB
        # and would have matched the token "fibula", but the oldest-first
        # ordering pushed them past the 24-row cap. Patients ask about
        # "my X" with recency bias 99% of the time; the LLM can still
        # walk backward through history once the relevant decade is
        # surfaced.
        sub_stmt = (
            select(ExtractedFact)
            .where(or_(*filters))
            .order_by(
                ExtractedFact.date_start.desc().nullslast(),
                ExtractedFact.created_at.desc(),
            )
            .limit(limit)
        )
        if not include_archived:
            sub_stmt = sub_stmt.where(
                ExtractedFact.review_state.notin_(_HIDDEN_STATES)
            )
        substr_facts = list((await db.execute(sub_stmt)).scalars().all())

    # Category first — when "medications" is in the query, the LLM
    # should see the categorical breadth before substring hits like a
    # "Medications:" section header from an unrelated note.
    seen: dict = {}
    for f in cat_facts + substr_facts:
        if f.id not in seen:
            seen[f.id] = f
        if len(seen) >= limit:
            break
    return list(seen.values())[:limit]
