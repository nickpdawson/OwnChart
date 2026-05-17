# Shipped vs Roadmap — OwnChart 0.1 Alpha

> What works today. What is explicitly not in the alpha. Last
> reviewed at the 0.1 alpha cut.

This page is the honesty contract for the alpha. If a feature is in
**Shipped**, you should be able to use it. If a feature is in
**Roadmap**, do not plan around it being there — it isn't.

For deeper detail on what's been hardened for the alpha vs deferred
to beta, see [RELEASE_NOTES_ALPHA.md](./RELEASE_NOTES_ALPHA.md).

## Shipped in alpha

### Ask / AI research partner with citations

Natural-language questions across your whole record, an individual
Event, a Dossier, or a specific time window. Every substantive answer
cites the evidence it used. Conversations are saved, searchable,
resumable, and pinnable to Events / Dossiers.

### Events and Dossiers

- **Events** are meaningful things that happened (surgery, race,
  injury, diagnosis, recovery window). Rename, alias, search by alias
  in chat. Save-as-Event and Attach-to-Event from any chat candidate.
- **Dossiers** are long-running topics (hearing loss, knees,
  strabismus, training). Living case files that collect facts,
  sources, conversations, Events.

### Source Authority Doctrine

A 6-tier classifier (`primary_event` > `specialist_proximate` >
`contemporaneous_support` > `ehr_summary` > `self_reported_history` >
`model_inference`) drives retrieval ordering. The doctrine ensures
that asking "when did I have ACL surgery" cites the operative imaging,
not a pre-op anesthesia summary. See
[SOURCE_AUTHORITY_DOCTRINE.md](./SOURCE_AUTHORITY_DOCTRINE.md).

### Photo and screenshot ingestion

- Camera-roll photos via the iOS app with EXIF capture date, GPS, and
  on-device caption.
- Structured screenshot extraction via Claude vision: vaccine cards,
  lab results, prescription labels, etc. Brand-name labels carry the
  underlying medical concept in the description so retrieval matches
  the concept even when the chart label is a brand.

### HealthKit alpha sync path

Native iOS app reads HealthKit and syncs activity, heart, body, sleep,
workouts, nutrition, mindfulness, symptoms, medications, reproductive,
and clinical-records data. Per-identifier strategy
(`daily_aggregate` for high-volume metrics like steps, `raw` for
low-volume like workouts). Anchored-query cursor is persisted
server-side so re-syncs are safe and incremental. See
[HEALTHKIT_SYNC.md](./HEALTHKIT_SYNC.md).

### FHIR / CCDA / PDF ingestion

- **FHIR R4** via SMART on FHIR. Epic and athenahealth are the most
  mature paths; ModMed is newly wired; NextGen and Oracle Health /
  Cerner are documented or in progress. See [CONNECTORS.md](./CONNECTORS.md).
- **CCDA / XML** continuity-of-care documents.
- **FHIR clinical notes and CCDA attachments auto-extract at sync
  time** — every `clinical_note` or `ccda_xml` attachment with
  ≥40 chars of plaintext is sent to the extractor immediately after
  the connector sync, and structured facts appear within ~30 seconds
  of the sync response. PDF document ingestion is supported via
  upload; PDF text extraction is the same vision path that handles
  scanned documents. Pre-existing rows ingested before the
  auto-extract hook landed are not backfilled automatically — run
  `scripts/backfill_clinical_notes.py` if you want history extracted.
- **Single-host contract:** `/api/*` lives on the same hostname as
  the UI. No `api.*` subdomain.

### Review compression and pattern-managed facts

Routine refill noise (the same medication re-asserted at every
encounter; the same active-meds block fired by every preventive visit)
is collapsed under a `pattern_managed` state. Those facts stay
retrievable in Ask, Events, Dossiers, Timeline, and analysis — they
just stop crowding the Review Inbox. See the Review section in the
app for the "this fact is part of a known pattern" explainer.

### Usage / cost attribution

`/settings/providers/usage` shows your LLM spend by date range,
provider, model, purpose, and cache-hit rate. CSV export available.
V1 single-user (the deployment owner sees all rows); per-user filter
ships with multi-user.

### Voice notes (with on-device iOS transcript)

The iOS app records a voice memo, transcribes on-device, and uploads
the transcript with timestamp metadata. The audio file is preserved;
the transcript is the searchable surface.

### Demo mode

A read-only synthetic-data instance at <https://demo.ownchart.me>.
Safe for App Store review, sharing the product with someone, or
testing changes against a stable record. See [DEMO.md](./DEMO.md).

## Roadmap — not shipped in 0.1 alpha

These are explicit non-goals for the alpha. They are good ideas; they
are not in the version you're holding.

### Calendar integration

Connecting a calendar (Google Calendar, Apple Calendar, iCal) so OwnChart
can correlate health signals with travel, meetings, late nights, and
density of life. Not shipped. When asked, the AI does not have your
calendar.

### Multi-user / caregiver / household

The database schema separates *user account* (authentication) from
*person whose record this is*, which is the foundation for caregiver
delegation. The management UI (invite a caregiver, scope their
access, manage their consent) is not shipped in 0.1. Effective today,
OwnChart is single-tenant per instance.

### Passkeys / 2FA

Local password (Argon2id) is the auth in 0.1. Passkeys / WebAuthn /
TOTP 2FA / SSO are roadmap. Recommend deploying behind a reverse proxy
that does its own auth layer (e.g. Authentik) if you need stronger
authentication before this lands.

### Garmin

Garmin sync (Connect IQ, FIT files, Garmin Health API) is not shipped.
For now, Apple Health / HealthKit is the only first-class wearable
path. Garmin → Apple Health bridges (via third-party apps) work as
data sources for HealthKit sync, but native Garmin is roadmap.

### Life events surface

A first-class "life events" object (job change, move, relationship,
loss, travel, exercise routine change) that doesn't fit cleanly into
Medical Event or Dossier is roadmap. Today, those things live as
notes or free-text annotations.

### MCP server (Model Context Protocol)

An MCP server endpoint so other tools (Claude Desktop, Cursor, any
MCP-aware client) can query your OwnChart instance with the same
consent and audit guarantees as the web app — roadmap. The
architectural commitment is there; the implementation is not in 0.1.

### Broader provider / connector coverage

Vendors not yet covered in `user-docs/`:

- Allscripts / Veradigm
- eClinicalWorks (eCW)
- MEDITECH
- Kaiser Permanente (CCD-only path; FHIR isn't publicly exposed)
- MyHealthONE / HCA

If you've registered an app with one of these and want to upstream a
setup guide, the existing vendor guides
([EPIC_SETUP.md](./EPIC_SETUP.md), etc.) show the shape.

### Richer charts and timeline

Timeline heatmap redesign, multi-event comparison ("compare Event A
to Event B in HRV, sleep, and weight"), and longitudinal pattern
charts are all roadmap. Today the Timeline is functional but plain;
Discover surfaces patterns but doesn't chart them yet.

### Other roadmap items mentioned in surrounding docs

- **Application-layer encryption of `data/`** — relies on host disk
  encryption today.
- **DICOM / imaging support.**
- **Plugin architecture.**
- **Tamper-evident audit log** (hash-chained / external-anchored).
- **Automated PHI scanning on outbound LLM calls.**
- **First-class backup tool** — backups are operator-implemented in
  0.1.
- **Prompt-edit UI** — prompts are file-editable in 0.1
  ([PROMPTS.md](./PROMPTS.md)).

## How to read this list

A feature being on the Shipped list is not the same as the feature
being polished. The alpha standard is "the feature works on the
golden path and doesn't lie." Edge cases, UI rough edges, and "I
expected this to do X" gaps are likely; that's what alpha means. File
issues, watch for the roadmap items listed above, and treat OwnChart
as a serious tool you are helping shape — not as a finished product
you're consuming.

— Last reviewed: 0.1 alpha release cut.
