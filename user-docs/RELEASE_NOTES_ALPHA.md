# OwnChart Alpha — Release Notes

**Tag:** alpha-0.1.0
**Date drafted:** 2026-05-16
**Branch:** `dev` → pending PM approval for merge to `main`
**Demo:** demo.ownchart.me (released only after dev → main)
**Personal:** ownchart.dzsec.net (already running `dev`)

This is the alpha-readiness cut. Scope was narrowed at PM ask: ship what's needed for first-pass clinical-friend review, defer everything else. No new product surfaces in this release.

## What's in

### Native iOS

- HealthKit sync (`/api/healthkit/sync`) covering activity, heart, body, sleep, workouts, nutrition, mindfulness, symptoms, medications, reproductive, and clinical records.
  - Per-identifier strategy: `daily_aggregate` for high-volume metrics (steps, HR, energy), `raw` for low-volume (workouts, body mass, sleep, symptoms).
  - Demo-mode guard refuses raw HR / steps / energy posts to keep the demo DB sized for the public deploy.
  - Idempotency: every sample carries a `client_sample_key`; daily aggregates collapse on a `daily-metric|<identifier>|<UTC-date>` partial unique index. Re-syncs are safe.
  - Anchored-query cursor persisted server-side per identifier so backfills resume.
- Camera-roll photo upload with EXIF capture date, GPS, batch-import deferral, on-device caption.
- Structured-screenshot extraction (vaccine cards, lab results, prescription labels) via Claude vision. Brand-name labels (Comirnaty, Fluarix, Trulicity) carry the medical concept in the description so retrieval matches "covid vaccinations" without a hardcoded brand list.
- Voice notes (with on-device transcript).

### Web app

- Ask, Home insight, Timeline, Discover, Dossier, Episode, Review, Settings (incl. provider connectors + LLM provider config).
- "Processing" animation on every blocking LLM surface (EI, Ask, Save-as-Episode, Home insight) since EI runs 30–45s and the previous UI looked dead.

### Backend

- FHIR sync from connected providers, with automatic `clinical_note` and `ccda_xml` extraction at sync time (no separate batch).
- Source Authority Doctrine: 6-tier classifier (`primary_event` > `specialist_proximate` > `contemporaneous_support` > `ehr_summary` > `self_reported_history` > `model_inference`) driving retrieval ordering. Anti-patterns Nick caught in round-3 review are now CI-protected.
- Retrieval tier-diversity (`_ensure_tier_diversity`): round-robin across tiers so a single tier's date-DESC ordering can't crowd out older but higher-authority records.
- Category-aware retrieval: question tokens like "vaccinations" / "allergies" / "appointments" pull entire `fact_type` buckets, defeating brand-label vs concept token mismatches.
- Single-host contract: `/api/*` served on the user's chosen hostname (no `api.` subdomain).

## What's been hardened for alpha

### Reliability

- Structured JSON error contract: every error response is JSON `{detail, ...}` with `Content-Type: application/json`. No uvicorn plain-text 500s. Catches both `HTTPException` and unhandled exceptions.
- EXIF NUL-byte fix: iPhone screenshot `UserComment` follows EXIF spec `"ASCII\x00\x00\x00<text>"`; `_safe()` strips NULs from str / bytes / dict-key paths before JSONB write (was crashing photo uploads with `asyncpg.UntranslatableCharacterError`).
- Upload-batch audit (new for alpha): `X-Client-Batch-Id` / `X-Client-Item-Id` headers are stamped on `raw_metadata.upload_audit` on success and echoed in error JSON on failure, so iOS can map a 4xx/5xx back to its local `(item_id → file)` map. See `UPLOAD_CONTRACT.md`.
- SourceDetail surfaces `extraction_status`, `extraction_fact_count`, `extraction_error`, `vision_status`, `vision_structured_fact_count`, `vision_relevance_score` so the UI shows extraction state without digging into logs.

### Tests

80 pure-function pytest tests, run in ~2s, no DB / LLM. CI-protect the alpha-critical paths:

| File | Tests | Covers |
|---|---|---|
| `test_exif_safe.py` | 7 | NUL-stripping on str/bytes/dict-key paths + IFDRational handling. |
| `test_healthkit_sync.py` | 14 | Strategy enforcement, alpha-scope registry coverage, daily_metric_key idempotency, label formatting. |
| `test_medication_chronology.py` | 7 | Tracker > pre-op PSH ordering, encounter-summary med-list is contemporaneous (reconciliation, not Rx). |
| `test_medication_dedup.py` | 9 | Auto Export medication sample dedup. |
| `test_retrieval_diversity.py` | 9 | Tier round-robin + category aliases (vaccination / allergy / appointment / instruction). |
| `test_source_authority.py` | 24 | 6-tier classifier across primary_event / specialist_proximate / contemporaneous_support / ehr_summary / self_reported_history; anti-pattern guards (PSH ≠ operative record); date_origin taxonomy. |
| `test_upload_audit.py` | 10 | Stateless header → dict → raw_metadata correlation. |

## Known issues / deferred to beta

- **Open (P1)**: vision extractor doesn't pull `date_start` from PDF facts (Alpine PDFs have 83 facts but NULL `date_start`). Tracked task #124.
- **Open (P1)**: clinical-note extractor should mark undated facts as `historical_undated` rather than stamping the ingest date as `date_start`. Tracked task #125.
- **Deferred**: Server-side upload-audit table for cross-session reconciliation. Stateless header echo covers alpha (single user, foreground sync). Multi-tenant beta will want indexed lookup.
- **Deferred**: Provider OAuth, multi-tenant scaling, calendar surface, broad UI redesigns. Out of alpha scope per PM.

## How to verify the build

```bash
# In api container
/opt/venv/bin/python -m pytest ownchart/tests/

# Expect:
# 80 passed in ~2s
```

Manual smoke per `docs/QA-TEST-PLAN.md`:
1. iOS upload of vaccine card screenshot → SourceDetail shows `vision_structured_fact_count > 0`.
2. iOS HK sync with `mode=demo` → raw HR rejected with 415; `daily_aggregate` accepted.
3. Ask: "list my covid vaccinations" → returns Comirnaty rows even though the label is brand-only.
4. Ask: "when did I have ACL surgery" → cites OrthoVirginia imaging (tier-1), not Stanford anesthesia pre-op (tier-5).
5. Force a 415 (small PNG); confirm the iOS app shows the specific item that failed (batch correlation).

## Hold

- **dev → main**: held until PM approval.
- **Demo redeploy**: held until after main.
- **NPM / Cloudflare / DNS / cert changes**: require explicit go-ahead from Nick before each.
