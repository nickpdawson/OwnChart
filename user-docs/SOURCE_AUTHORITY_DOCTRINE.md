# Source Authority Doctrine

> OwnChart must not confuse precision with truth.
>
> When multiple sources describe the same health or life event, the system ranks evidence by **authority, proximity, and purpose** — not by whether a source has a clean date or a tidy label.
>
> Canonical truth beats convenient precision.

Status: doctrine, adopted 2026-05-15 PM (alpha gate work).

## Evidence hierarchy

The tiers below are the unit of source-authority ranking. They live as the `authority_tier` string in `source_documents.raw_metadata.authority_tier` and propagate to every retrieval result + every LLM prompt rendering.

| Tier | Label | What it is |
|---:|---|---|
| 1 | `primary_event` | Operative reports, procedure notes, pathology reports, imaging reports, lab results, signed specialist assessments, device-recorded health data. |
| 2 | `specialist_proximate` | Notes from the specialty closest to the event — orthopedics for knees, ophthalmology for eyes, cardiology for rhythm, audiology for hearing, HealthKit for workouts. |
| 3 | `contemporaneous_support` | PT notes, discharge instructions, prescriptions, visit summaries, calendar / travel / life events near the event. |
| 4 | `ehr_summary` | EHR summaries, problem lists, continuity-of-care snapshots. Useful but often copied, stale, incomplete, or compressed. |
| 5 | `self_reported_history` | Intake forms and patient-reported surgical/medical history *inside* clinical records. Useful clues, not canonical unless the user confirms them. |
| 6 | `model_inference` | LLM-derived statements. Always labeled `inferred`; never overwrite source-backed or user-canonical truth. |

User-declared corrections (`user_assertions` table) are **user-canonical** — they take precedence over inference and over conflicting tier-3+ sources, but the original evidence is preserved alongside.

## Answer behavior

- **Lead with the strongest source**, even if it is less tidy.
- If a weaker source provides a more precise date or detail, present it as **secondary**, not the headline.
- If the primary source proves the event but not the exact date, **say so**.
- If sources conflict, **surface the conflict** — never silently pick the neatest answer.
- If the user declares a correction, store it as user-canonical while preserving original evidence.
- Copied clinical history must never outrank records from the specialty / source closest to the event.

## Answer scaffold

Event-shaped answers must surface four sections (collapsible in UI, explicit in prose for the LLM):

1. **What we know** — the synthesized one-paragraph reading.
2. **Best evidence** — the highest-authority citation(s) backing the claim. Format `tier_label · source · date · fact_id`.
3. **What is uncertain** — what the primary source doesn't establish (e.g. "the operative report shows the surgery but not the surgeon's name").
4. **What would make this canonical** — concrete next step: upload the op note, request the pathology report, confirm in user-canonical corrections.

## Implementation status (2026-05-15)

| Item | Status |
|---|---|
| 6-tier classifier in retrieval | ✅ Live |
| Authority tier surfaced in Ask evidence block | ✅ Live |
| Ingestion-time tier stamping on `source_documents.raw_metadata.authority_tier` | ✅ Live |
| Retrieval source-type / tier diversity guarantee | ✅ Live (top-K reserves slots per tier present in match) |
| Ask + EI prompt updated with the 4-section scaffold | ✅ Live |
| Claim-level evidence strength (per-fact, not source-level) | 🟡 Queued |
| User-canonical correction UI on Event page (`user_assertions` exists, UI doesn't) | 🟡 Queued |
| Source-conflict surfacing UI ("Sources disagree about date") | 🟡 Queued |
| Backfill script for existing source_documents | ✅ Live: `scripts/backfill_authority_tier.py` |

## Anti-patterns the doctrine forbids

- Citing a Stanford **anesthesia pre-procedure evaluation** as primary evidence for an ACL surgery date when an OrthoVirginia specialist note exists. The pre-op H&P is a copy-forward patient-reported history, not the operative record.
- Hiding a problem-list entry behind a more recent encounter note that copied the same diagnosis forward. The original entry is the higher-authority record.
- Asserting an event date in prose with no citation, then footnoting "inferred" — inference must be labeled inline, in the same sentence.
- Picking the answer with the cleanest date when a higher-tier source has a fuzzier date. Truth ranks above tidy.
