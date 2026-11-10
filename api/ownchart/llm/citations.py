"""Citation validation shared by every answer path.

The model may only cite evidence it was shown. Chat used to check `fact`
and `source` ids and pass every other citation type through; Home and
topic briefs persisted whatever ids came back. The audit (2026-11-10,
item 7) called that out: an injected id from another record, or a made-up
one, could land in the saved research artifact with a source-backed badge.

`filter_citations` returns the accepted citations and an audit record that
callers persist next to the answer, so the saved artifact says how many
citations were removed and why rather than showing a quietly shorter list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable


# Citation types the application can verify today. Anything else is
# dropped: there is no allowed-id set for it, so it cannot be checked.
VERIFIABLE_TYPES: frozenset[str] = frozenset({"fact", "source", "anchor"})


@dataclass
class CitationAudit:
    accepted: int = 0
    dropped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "dropped": self.dropped,
            "reasons": dict(self.reasons),
        }


def _as_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def filter_citations(
    citations: Iterable[Any],
    *,
    fact_ids: set[uuid.UUID],
    source_ids: set[uuid.UUID],
    anchor_ids: set[uuid.UUID] | None = None,
) -> tuple[list[dict[str, Any]], CitationAudit]:
    """Keep citations whose (type, subject_id) is in the supplied evidence.

    Dedupes on (type, subject_id) and preserves order. `anchor_ids`
    defaults to empty, so anchor citations are dropped unless the caller
    supplies the anchors it showed the model.
    """
    allowed: dict[str, set[uuid.UUID]] = {
        "fact": fact_ids,
        "source": source_ids,
        "anchor": anchor_ids or set(),
    }
    audit = CitationAudit()
    seen: set[tuple[str, uuid.UUID]] = set()
    out: list[dict[str, Any]] = []
    for c in citations:
        if not isinstance(c, dict):
            audit.drop("malformed")
            continue
        ctype = c.get("citation_type")
        sid = _as_uuid(c.get("subject_id"))
        if not ctype or sid is None:
            audit.drop("malformed")
            continue
        if ctype not in VERIFIABLE_TYPES:
            audit.drop(f"unverifiable_type:{ctype}")
            continue
        if (ctype, sid) in seen:
            continue
        if sid not in allowed[ctype]:
            audit.drop(f"not_in_evidence:{ctype}")
            continue
        seen.add((ctype, sid))
        audit.accepted += 1
        out.append({**c, "subject_id": str(sid)})
    return out, audit


def filter_fact_ids(
    ids: Iterable[Any], allowed: set[uuid.UUID],
) -> tuple[list[str], int]:
    """Keep ids that name supplied facts; return (kept, dropped_count)."""
    kept: list[str] = []
    dropped = 0
    seen: set[uuid.UUID] = set()
    for raw in ids:
        fid = _as_uuid(raw)
        if fid is None or fid not in allowed:
            dropped += 1
            continue
        if fid in seen:
            continue
        seen.add(fid)
        kept.append(str(fid))
    return kept, dropped


def filter_statements(
    statements: Iterable[Any], allowed: set[uuid.UUID],
) -> tuple[list[dict[str, Any]], int]:
    """For brief `well_supported` / `uncertain` entries ({statement,
    fact_ids}): keep each statement but strip fact_ids not in evidence.
    A statement left with no supporting ids is kept and flagged
    `unsupported: true` so the UI can qualify it instead of showing it
    with an empty, confident citation list."""
    out: list[dict[str, Any]] = []
    dropped_ids = 0
    for s in statements:
        if not isinstance(s, dict):
            continue
        kept, dropped = filter_fact_ids(s.get("fact_ids") or [], allowed)
        dropped_ids += dropped
        entry = {**s, "fact_ids": kept}
        if not kept:
            entry["unsupported"] = True
        out.append(entry)
    return out, dropped_ids
