"""Effective evidence: an extracted fact as the patient has corrected it.

Corrections live in `UserAssertion` rows and the `ExtractedFact` keeps
its original wording and dates (PRD: "Original records are never
overwritten"). Until 2026-11-10 only the topic readout merged the two,
and it did so non-deterministically (dict overwrite in arbitrary order).
Chat, the legacy Ask route and topic briefs sent the model the original
label and date even after the patient corrected them.

This module is the one projection every reader uses:

  - `load_assertions`  — fetch the record's assertions for a set of facts
  - `apply_assertions` — deterministic latest-effective merge per field
  - `effective_facts`  — both, returning `EffectiveFact` in input order

Rules of the merge (chronological by created_at, then id):

  - `correct`  sets each supplied canonical field; fields the correction
               left empty keep the previous effective value, so a later
               date-only correction does not erase an earlier label fix.
  - `confirm`  marks the fact confirmed; changes no field.
  - `reject`   marks the fact rejected; changes no field.
  - `annotate` appends its reason as a note; changes no field. A later
               annotation never erases an earlier correction.

The original values stay on `EffectiveFact.original_*` so prompts, UI and
exports can show both: "you corrected this" and "the source said".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.extracted_fact import ExtractedFact
from ..models.user_assertion import UserAssertion


@dataclass
class EffectiveFact:
    fact: ExtractedFact
    label: str
    description: str | None
    date_start: datetime | None
    date_end: datetime | None
    original_label: str
    original_description: str | None
    original_date_start: datetime | None
    original_date_end: datetime | None
    corrected: bool = False
    confirmed: bool = False
    rejected: bool = False
    corrected_at: datetime | None = None
    correction_reasons: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)

    @property
    def id(self) -> uuid.UUID:
        return self.fact.id

    @property
    def display_label(self) -> str:
        """What a human sees: the correction, else the curated display
        label, else the raw extracted label."""
        if self.corrected and self.label != self.original_label:
            return self.label
        return self.fact.display_label or self.label

    @property
    def label_changed(self) -> bool:
        return self.corrected and self.label != self.original_label

    @property
    def date_changed(self) -> bool:
        return self.corrected and (
            self.date_start != self.original_date_start
            or self.date_end != self.original_date_end
        )

    @property
    def claim_class(self) -> str:
        """docs/04 evidence class for this fact's current values."""
        if self.corrected or self.confirmed:
            return "user_canonical"
        return "source_backed"

    def correction_note(self) -> str | None:
        """One prompt-ready line describing the patient's correction, or
        None when there is nothing to say."""
        if self.rejected:
            return "rejected by the patient — do not present as true"
        if not self.corrected:
            if self.confirmed:
                return "confirmed by the patient"
            return None
        parts: list[str] = []
        if self.label_changed:
            parts.append(f"label '{self.original_label}' → '{self.label}'")
        if self.date_changed:
            o = self.original_date_start.date().isoformat() if self.original_date_start else "?"
            n = self.date_start.date().isoformat() if self.date_start else "?"
            parts.append(f"date {o} → {n}")
        if self.description != self.original_description and self.description:
            parts.append("description revised")
        when = f" on {self.corrected_at.date().isoformat()}" if self.corrected_at else ""
        reason = f" ({'; '.join(self.correction_reasons)})" if self.correction_reasons else ""
        what = "; ".join(parts) if parts else "values confirmed as corrected"
        return (
            f"corrected by the patient{when}: {what}{reason}. Treat the corrected "
            "values as this person's working record; the source's original is "
            "kept for reference."
        )


def _sort_key(a: UserAssertion) -> tuple:
    return (a.created_at or datetime.min, str(a.id))


def apply_assertions(
    fact: ExtractedFact, assertions: Iterable[UserAssertion],
) -> EffectiveFact:
    ef = EffectiveFact(
        fact=fact,
        label=fact.label,
        description=fact.description,
        date_start=fact.date_start,
        date_end=fact.date_end,
        original_label=fact.label,
        original_description=fact.description,
        original_date_start=fact.date_start,
        original_date_end=fact.date_end,
    )
    for a in sorted(assertions, key=_sort_key):
        kind = a.assertion_type
        if kind == "correct":
            ef.corrected = True
            ef.corrected_at = a.created_at or ef.corrected_at
            if a.canonical_label:
                ef.label = a.canonical_label
            if a.canonical_description:
                ef.description = a.canonical_description
            if a.canonical_date_start is not None:
                ef.date_start = a.canonical_date_start
            if a.canonical_date_end is not None:
                ef.date_end = a.canonical_date_end
            if a.reason:
                ef.correction_reasons.append(a.reason)
        elif kind == "confirm":
            ef.confirmed = True
        elif kind == "reject":
            ef.rejected = True
        elif kind == "annotate":
            if a.reason:
                ef.annotations.append(a.reason)
    return ef


async def load_assertions(
    db: AsyncSession,
    fact_ids: Sequence[uuid.UUID],
    *,
    person_record_id: uuid.UUID | None,
) -> dict[uuid.UUID, list[UserAssertion]]:
    """Assertions grouped by fact, filtered to the record when given.

    The record filter matters: a caregiver's correction on Mom's record
    must never color the same clinical phrase on Dad's record.
    """
    if not fact_ids:
        return {}
    stmt = select(UserAssertion).where(UserAssertion.related_fact_id.in_(list(fact_ids)))
    if person_record_id is not None:
        stmt = stmt.where(UserAssertion.person_record_id == person_record_id)
    rows = (await db.execute(stmt)).scalars().all()
    out: dict[uuid.UUID, list[UserAssertion]] = {}
    for a in rows:
        if a.related_fact_id is not None:
            out.setdefault(a.related_fact_id, []).append(a)
    return out


async def effective_facts(
    db: AsyncSession,
    facts: Sequence[ExtractedFact],
    *,
    person_record_id: uuid.UUID | None,
) -> list[EffectiveFact]:
    by_fact = await load_assertions(
        db, [f.id for f in facts], person_record_id=person_record_id,
    )
    return [apply_assertions(f, by_fact.get(f.id, ())) for f in facts]


def effective_by_id(effs: Iterable[EffectiveFact]) -> dict[uuid.UUID, EffectiveFact]:
    return {e.id: e for e in effs}
