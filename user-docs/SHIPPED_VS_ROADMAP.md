# Shipped vs Roadmap — OwnChart Beta 1

> What works today. What is explicitly held to a post-release follow-up.
> What is still on the longer roadmap. Last reviewed at the Beta 1
> release cut (2026-05-26).

This page is the honesty contract for Beta 1. If a feature is in
**Shipped**, you should be able to use it. If it is in
**Held for post-release**, the code may exist on the dev branch but
the user-visible behavior or live verification isn't release-grade
yet — do not plan around it in Beta 1. If it is in **Longer-term
roadmap**, it's a known good idea that's not yet started.

For deeper detail on what landed in each release see:

- [RELEASE_NOTES_BETA1.md](./RELEASE_NOTES_BETA1.md) (this release).
- [RELEASE_NOTES_ALPHA.md](./RELEASE_NOTES_ALPHA.md) (prior).

---

## Shipped in Beta 1 (new since alpha)

These are the additions Beta 1 makes on top of the alpha
foundation. The full alpha feature list is preserved under
"Shipped in alpha (carried forward)" below — Beta 1 includes
all of that, plus everything in this section.

### iOS EventKit calendar foundation

Calendar context lands as the **iOS EventKit calendar foundation**:
recent iOS calendar context from one or more iOS calendars the user
selects, stored under the active person record, with three privacy
modes (`busy_only`, `title_and_time`, `full_details`) applied
client-side and re-applied server-side as defense in depth. An LLM
exposure floor (`source_consent`) controls what the LLM sees
independent of what's stored. Per-calendar `external_id`,
`ical_uid`, and IANA time zones are captured on every event.
Deletion is iOS-authoritative — only explicit `tombstoned: true`
soft-deletes, with a 30-day hard-delete retention worker.

Public phrasing rule for Beta 1: calendar copy reads "iOS EventKit
calendar foundation" or "recent iOS calendar context." Other
calendar adapters (Google, ICS, CalDAV), Ask integration, timeline
/ dossier surfaces, web settings UI, and per-source history-window
controls remain held until their respective follow-ups land — see
"Held for post-release" below.

### Multi-tenant invites (household / caregiver records)

Beta 1 introduces person records on a single OwnChart instance —
a household / caregiver model. Each record is the body / life /
health record being analyzed; users hold memberships on records
with `owner` or `caregiver` roles. Every record-bearing endpoint
scopes by an `X-OwnChart-Person-Record` header (iOS) or
signed-session pin (web). Invites are owner-issued, single-use,
hashed at rest, expire (24h / 7d / 30d), and the resulting URL
is copied out of band — no outbound email in Beta 1.

The first signup on a fresh DB still creates the owner
automatically. After that, `/api/auth/register` requires a valid
invite token unless an operator explicitly opens public
self-registration via `auth.allow_self_registration: true`.

### Date provenance — Phase 1

Every fact carries a `date_origin` classification so the retrieval
and presentation layers can distinguish event dates from
source-import dates from ingest timestamps. Phase 1 covers new
ingest going forward — historical reingest of pre-Beta-1 rows is
held to a post-release follow-up so the change is observable
incrementally rather than as a single mass re-write.

### Export UI

Operators and users can request an export of the active person
record. The export job model + canonical OwnChart JSON + TXT
packet ship in Beta 1 with a 72-hour TTL, audit events at every
state transition, and per-record cross-leak prevention. The UI
surface for requesting + downloading exports ships in the web
app for owner / caregiver roles. **Async-with-progress and an
explicit upload-size cap are held** for the post-release
follow-up — Beta 1 keeps the synchronous-then-poll pattern.
**Pictal JSON ships in Beta 1.2** (see "Shipped in Beta 1.2"
below) as a third available mapper alongside the canonical
OwnChart JSON and TXT. CCDA XML remains roadmap.

### Cerner / Oracle Health connector

Beta 1 adds Oracle Health (Cerner) as a first-class SMART-on-FHIR
connector. Patient-app registration via
[code-console.cerner.com](https://code-console.cerner.com),
FHIR R4 (Ignite), public client with PKCE (no secret), env var
`OWNCHART_CERNER_CLIENT_ID`. FHIR base URLs come from the Oracle
Millennium patient R4 endpoint directory; the doc carries a
concrete Centra Health example. See
[CERNER_SETUP.md](./CERNER_SETUP.md).

---

## Shipped in Beta 1.1 (post-Beta-1 increment)

A small targeted release on top of Beta 1. Same shipped-vs-held
honesty contract.

### HealthKit MCP bridge (local-only)

A published macOS bridge that lets a local MCP client on the same
Mac (Claude Desktop, Claude Code, Codex) read aggregated HealthKit
data from your iPhone over Wi-Fi while the OwnChart iOS app is
open. macOS + Node 20+; npm: [`ownchart-hk-mcp-bridge`](https://www.npmjs.com/package/ownchart-hk-mcp-bridge);
source: [github.com/nickpdawson/ownchart-hk-mcp-bridge](https://github.com/nickpdawson/ownchart-hk-mcp-bridge).
Operator-facing setup is in [HEALTHKIT_MCP.md](./HEALTHKIT_MCP.md);
the bridge repo carries the full README, threat model, and
acceptance grid.

**What the bridge exposes:** aggregated daily summaries
(`healthkit_query_daily_summary`) and a capability registry
(`healthkit_capabilities`). Read-only. Paired with a 6-digit code
once; revoke from OwnChart Settings.

**What it does NOT expose:** raw sample streams, GPS coordinates,
workout routes, or medication dose events. Beta 1.1 is a local
integration — there is no cloud relay, no OwnChart backend in the
path, and **no ChatGPT remote-connector support implied or
provided**. The bridge requires the OwnChart iOS app to be running
in the foreground (with a brief grace period); it is not always-on
background infrastructure.

This is a distinct surface from the still-roadmap **MCP server
endpoint to the OwnChart backend** (Postgres-backed records on the
self-hosted instance) — see "MCP server (Model Context Protocol)"
under "Longer-term roadmap" below.

---

## Shipped in Beta 1.2 (post-Beta-1 increment)

A small targeted release on top of Beta 1 / 1.1. Same
shipped-vs-held honesty contract.

### Pictal JSON export

Beta 1.2 adds **Pictal JSON** as a third available export mapper
alongside the canonical OwnChart JSON and TXT mappers that
shipped in Beta 1. Requesting an export produces a downloadable
Pictal JSON file at the existing `/api/exports` flow; the user
downloads it from the OwnChart web UI and **imports it into
Pictal manually** outside of OwnChart. There is no
OwnChart-to-Pictal network handoff and no cloud relay — the
file goes user → user.

Pictal JSON inherits the Beta 1 export contract:
synchronous-then-poll job model, per-record cross-leak
prevention, owner / caregiver roles, 72-hour TTL, and the five
existing audit events (`export_requested`, `export_completed`,
`export_failed`, `export_downloaded`, `export_deleted`). The
held async + size-cap items remain held; they apply uniformly
across all three mappers when they ship.

CCDA XML stays roadmap — OwnChart does not generate CCDA in this
release. If a user already has a CCDA file from elsewhere (an
EHR portal download, a CCD export from a health system, etc.),
Pictal itself can ingest that CCDA directly — that's a Pictal
feature, not an OwnChart export path.

---

## Held for post-release (Beta 1 → Beta 2 follow-ups)

These have code or design in place but are not user-grade in
Beta 1. Each gets a tracker entry and a verification gate before
its public-facing copy flips.

### ModMed live OAuth

The ModMed connector is implemented and the setup guide is
complete ([MODMED_SETUP.md](./MODMED_SETUP.md)), including the
FHIR Vendor Dashboard registration walk-through, the
portal-URL-vs-FHIR-base trap, and a Forefront Dermatology
endpoint example. **End-to-end OAuth against a real production
ModMed practice has not been verified in this release.** If the
SMART login page loads but patient credentials fail, the cause
is almost always vendor-side patient / firm / app entitlement,
not OwnChart code. Tracked for post-release verification.

### Reingest date provenance for historical rows

`date_origin` ships for new ingest in Beta 1 (Phase 1 above).
A backfill pass over pre-Beta-1 facts to retroactively assign
provenance is held — the goal is to land it cleanly with audit
output rather than as a silent mass-write.

### Canonical Spine — Phase 2

Phase 1 (date provenance, perimeter scoping) ships. Phase 2 —
broader spine alignment across the retrieval, projection, and
presentation paths — is held to a follow-up so it can be reviewed
as a coherent step rather than spread across Beta 1 slices.

### Export — async-with-progress + size cap

Beta 1 export is synchronous-then-poll under a 72-hour TTL with
no explicit per-job size cap. Long-running large exports and
true async progress reporting are held; the explicit upload-size
cap will land alongside the async mode so the user-visible
contract changes once, not twice.

### HealthKit steps ingest

HealthKit workout fidelity is shipped (Slice 2: per-workout
activity type, distance, energy, device). High-volume metrics
that need daily-aggregation refinement — most prominently
**steps** — are held to a post-release pass so the right
storage shape lands once. Workouts, heart, sleep, body, and
the other categories continue to sync per the alpha contract.

### HealthKit medication dose events

`HKCategoryTypeIdentifierMedicationDoseEvent` (the Apple Health
"Medications" dose-log surface that records when a user taps
"taken" / "skipped" in iOS) is **not** in the Beta 1 / 1.1 ingest
or in the HealthKit MCP bridge tool surface. Other medication
data paths (FHIR `MedicationStatement` / `MedicationRequest` from
EHR connectors, screenshot extraction of Rx labels, manual
entry) continue to work; only the HealthKit dose-event stream
specifically is held.

---

## Shipped in alpha (carried forward into Beta 1)

Beta 1 includes everything from alpha. Each item below is still
shipped and verified.

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

### HealthKit sync path

Native iOS app reads HealthKit and syncs activity, heart, body, sleep,
workouts, nutrition, mindfulness, symptoms, medications, reproductive,
and clinical-records data. Per-identifier strategy
(`daily_aggregate` for high-volume metrics, `raw` for low-volume).
Anchored-query cursor is persisted server-side so re-syncs are safe
and incremental. Beta 1 adds workout fidelity (type / distance /
energy / source / device); a steps-specific ingest refinement is held
for post-release (see above). See [HEALTHKIT_SYNC.md](./HEALTHKIT_SYNC.md).

### FHIR / CCDA / PDF ingestion

- **FHIR R4** via SMART on FHIR. Epic and athenahealth are the most
  mature paths; **Oracle Health (Cerner) is added in Beta 1** (see
  above); ModMed is implemented with the live-OAuth caveat noted
  above; NextGen is documented or in progress. See
  [CONNECTORS.md](./CONNECTORS.md).
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
Per-record / per-caregiver filters land in Beta 1 alongside the
multi-tenant invites work above.

### Voice notes (with on-device iOS transcript)

The iOS app records a voice memo, transcribes on-device, and uploads
the transcript with timestamp metadata. The audio file is preserved;
the transcript is the searchable surface.

### Demo mode

A read-only synthetic-data instance at <https://demo.ownchart.me>.
Safe for App Store review, sharing the product with someone, or
testing changes against a stable record. See [DEMO.md](./DEMO.md).

---

## Longer-term roadmap (not Beta 1, not held — not started)

These are good ideas that are explicitly not Beta 1. They are not
"held" because they aren't partially built yet — they're roadmap.

### Calendar — Google / ICS / CalDAV adapters

Beta 1 ships the iOS EventKit calendar foundation only. Google
Calendar, ICS feeds, and CalDAV adapters share the same backend
data model (one shared adapter contract) but are post-Beta-1.
Public Beta 1 copy should not name "Google" or "ICS" as shipped
adapters until they land. Per-source history-window controls
(beyond the 90d default), Ask retrieval integration, and timeline /
dossier product surfaces for calendar events are also post-Beta-1.

### Passkeys / 2FA

Local password (Argon2id) + invite tokens are the auth in Beta 1.
Passkeys / WebAuthn / TOTP 2FA / SSO are roadmap. Deploy behind a
reverse proxy that does its own auth layer (e.g. Authentik) if you
need stronger authentication before this lands.

### Garmin

Garmin sync (Connect IQ, FIT files, Garmin Health API) is not
shipped. For now, Apple Health / HealthKit is the only first-class
wearable path. Garmin → Apple Health bridges (via third-party apps)
work as data sources for HealthKit sync, but native Garmin is
roadmap.

### Life events surface

A first-class "life events" object (job change, move, relationship,
loss, travel, exercise routine change) that doesn't fit cleanly into
Medical Event or Dossier is roadmap. Today, those things live as
notes or free-text annotations.

### MCP server (Model Context Protocol)

An MCP server endpoint so other tools (Claude Desktop, Cursor, any
MCP-aware client) can query your OwnChart instance with the same
consent and audit guarantees as the web app — roadmap. The
architectural commitment is there; the implementation is not in
Beta 1.

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

### CCDA XML export mapper

Beta 1 exports canonical OwnChart JSON + TXT; Beta 1.2 adds Pictal
JSON (see "Shipped in Beta 1.2" above). **CCDA XML is on the longer
roadmap as a best-effort artifact** — OwnChart does not generate
CCDA today. If a user already has a CCDA file from elsewhere (an EHR
portal download, a CCD export from a health system), Pictal itself
can ingest that CCDA directly; that's a Pictal feature, not an
OwnChart export path. The "OwnChart generates CCDA" roadmap entry
is separate from "Pictal accepts CCDA from any source."

### Other roadmap items mentioned in surrounding docs

- **Application-layer encryption of `data/`** — relies on host disk
  encryption today.
- **DICOM / imaging support.**
- **Plugin architecture.**
- **Tamper-evident audit log** (hash-chained / external-anchored).
- **Automated PHI scanning on outbound LLM calls.**
- **First-class backup tool** — backups are operator-implemented in
  Beta 1.
- **Prompt-edit UI** — prompts are file-editable in Beta 1
  ([PROMPTS.md](./PROMPTS.md)).

---

## How to read this list

A feature being on the Shipped list is not the same as the feature
being polished. The Beta 1 standard is "the feature works on the
golden path and doesn't lie." Edge cases, UI rough edges, and "I
expected this to do X" gaps are likely; that's what beta means. File
issues, watch for the held-and-roadmap items above, and treat
OwnChart as a serious tool you are helping shape — not as a finished
product you're consuming.

— Last reviewed: Beta 1 release cut, 2026-05-26.
