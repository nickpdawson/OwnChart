# OwnChart Beta 1 — Release Notes

**Tag:** beta1-0.2.0 (release-readiness pass)
**Date:** 2026-05-26
**Latest addendum:** Beta 1.1 (HealthKit MCP bridge), 2026-05-30 — see
end of this doc.
**Audience:** operators and technical self-hosters. Parallel to
[RELEASE_NOTES_ALPHA.md](./RELEASE_NOTES_ALPHA.md); the full
shipped-vs-held picture lives in
[SHIPPED_VS_ROADMAP.md](./SHIPPED_VS_ROADMAP.md).

This is the Beta 1 release. Beta 1 adds five user-visible
capabilities on top of the alpha (iOS EventKit calendar foundation,
multi-tenant invites, date provenance Phase 1, Export UI, Cerner
connector) and explicitly **holds** five items for a post-release
follow-up (ModMed live OAuth, reingest date provenance, Canonical
Spine Phase 2, Export async + size cap, HealthKit steps ingest).
The alpha feature set is carried forward unchanged.

## What's in Beta 1

### iOS EventKit calendar foundation

Calendar context arrives in Beta 1 as the **iOS EventKit calendar
foundation**. Public-facing copy reads "iOS EventKit calendar
foundation" or "recent iOS calendar context" — that constraint is
intentional and reflects what's actually shipped: the EventKit
adapter end-to-end, three privacy modes (`busy_only`,
`title_and_time`, `full_details`) applied iOS-side and re-applied
server-side, an LLM exposure floor (`source_consent`) decoupling
storage from what the LLM sees, per-calendar `external_id` /
`ical_uid` / IANA time-zone capture, and iOS-authoritative deletion
(explicit `tombstoned: true` is the only delete trigger; absence
from a sync window is never inferred as a delete). Calendar events
live in their own table — they are never materialized as
`extracted_fact` rows, so the clinical retrieval contract stays
clean.

Live verification on `ownchart.dzsec.net` (2026-05-19): 192 real
events across two iOS calendars under one person record; all 192
record-scoped, full privacy + audit metadata preserved, delete
contract exercised end-to-end. See
`Working Docs/BETA1_M02_SLICE3_RELEASE_NOTE_2026_05_19.md` for the
internal acceptance ledger.

**Not in this release, by design:** Google Calendar OAuth, ICS
feeds, CalDAV, per-source history-window controls beyond the 90-day
default, Ask retrieval integration, timeline / dossier event
surfaces, and the web calendar settings UI. Those are tracked
individually; see `SHIPPED_VS_ROADMAP.md` "Longer-term roadmap" for
the public-facing list and the M02 implementation tracker for the
internal status ledger.

### Multi-tenant invites (household / caregiver records)

Beta 1 introduces person records on a single OwnChart instance — a
household / caregiver model, not enterprise SaaS tenancy. Each
person record is the body / life / health record being analyzed;
users hold memberships on records with `owner` or `caregiver`
roles. Every record-bearing endpoint scopes by an
`X-OwnChart-Person-Record` header (iOS, explicit) or signed-session
pin (web, ergonomic fallback). Cross-record probes return 404;
revoked-access returns 403 `record_access_revoked` without
invalidating the session.

Invites are owner-issued, single-use, hashed at rest, expire
(24h / 7d / 30d), and the resulting URL is copied out of band — no
outbound email in Beta 1.

First-signup-creates-owner is preserved on a fresh DB. After the
owner exists, `/api/auth/register` requires a valid invite token
unless an operator explicitly opens public self-registration via
`auth.allow_self_registration: true` (default `false`).

### Date provenance — Phase 1

Every fact carries a `date_origin` classification so retrieval and
presentation can distinguish event dates from source-import dates
from ingest timestamps. Phase 1 covers new ingest going forward.

**Held:** retroactive reingest of pre-Beta-1 facts to assign
provenance to historical rows. The hold is intentional — we'd
rather land that as a deliberate backfill with audit output than
as a silent mass write during release.

### Export UI

Operators and users can request an export of the active person
record from the web Settings → Export surface. The export job
model + canonical OwnChart JSON + TXT packet ship in Beta 1 with
a 72-hour TTL, audit events at every state transition
(`export_requested`, `export_completed`, `export_failed`,
`export_downloaded`, `export_deleted`), and per-record cross-leak
prevention. Owner and caregiver roles can request and download;
viewer role cannot.

**Held:** async-with-progress execution and an explicit per-job
upload size cap. Beta 1 keeps the synchronous-then-poll pattern.
Pictal JSON and CCDA mappers are on the longer roadmap.

### Cerner / Oracle Health connector

Beta 1 adds Oracle Health (Cerner) as a first-class SMART-on-FHIR
connector. The setup guide ([CERNER_SETUP.md](./CERNER_SETUP.md))
walks the patient-app registration via
[code-console.cerner.com](https://code-console.cerner.com): Patient
app type, FHIR R4 (Ignite), public client with PKCE (no client
secret), env var `OWNCHART_CERNER_CLIENT_ID`. Production FHIR base
URLs come from the Oracle Millennium patient R4 endpoint directory;
a concrete Centra Health example
(`https://fhir-myrecord.cerner.com/r4/ab208292-75a1-4788-9fc7-1e9a40a7eee3/`)
anchors the per-tenant-UUID shape. `/.well-known/smart-configuration`
discovery is the verification step before wiring `connectors.seed.yaml`.

## Held for post-release

These items are tracked but not user-grade in Beta 1.

### ModMed live OAuth

The ModMed connector code is implemented and
[MODMED_SETUP.md](./MODMED_SETUP.md) walks the FHIR Vendor
Dashboard registration including the portal-URL-vs-FHIR-base trap,
PKCE-public-client posture (no client secret), and a Forefront
Dermatology endpoint example. **End-to-end OAuth against a real
production ModMed practice has not been verified in this release.**
If the SMART login page loads but patient credentials fail, the
cause is almost always vendor-side patient / firm / app entitlement
(see the doc's troubleshooting matrix), not OwnChart code. Live
verification is the post-release follow-up — no code-fix work is
expected, just a real practice round-trip.

### Reingest date provenance for historical rows

Phase 1 (new ingest) ships. The backfill that retroactively assigns
`date_origin` to pre-Beta-1 facts is held — see Phase 1 notes
above.

### Canonical Spine — Phase 2

Phase 1 (date provenance + perimeter scoping) ships. Phase 2
(broader spine alignment across retrieval / projection /
presentation) is held to a follow-up so it can be reviewed as a
coherent step rather than spread across Beta 1 slices.

### Export — async-with-progress + explicit size cap

Beta 1 export is synchronous-then-poll under a 72-hour TTL with no
explicit per-job size cap. Long-running large exports and true
async progress reporting are held; the size cap will land alongside
async mode so the user-visible contract changes once, not twice.

### HealthKit steps ingest

HealthKit workout fidelity is shipped (per-workout activity type,
distance, energy, source, device — verified live on a paired Apple
Watch). High-volume metrics that need daily-aggregation refinement
— most prominently **steps** — are held to a post-release pass so
the right storage shape lands once. Workouts, heart, sleep, body,
nutrition, mindfulness, and the other categories continue to sync
per the alpha contract.

## What's verified, where

| Slice | Live-verified on | Acceptance ledger |
|---|---|---|
| Multi-person perimeter (Slice 1) | `ownchart.dzsec.net` 2026-05-18 | `Working Docs/BETA1_M02_SLICE1_RELEASE_NOTE_2026_05_18.md` |
| HealthKit workout fidelity (Slice 2) | `ownchart.dzsec.net` 2026-05-18, paired Apple Watch | `Working Docs/BETA1_M02_SLICE2_RELEASE_NOTE_2026_05_18.md` |
| iOS EventKit calendar foundation (Slice 3) | `ownchart.dzsec.net` 2026-05-19, paired iPhone | `Working Docs/BETA1_M02_SLICE3_RELEASE_NOTE_2026_05_19.md` |
| Export skeleton + JSON/TXT (Slice 4) | `ownchart.dzsec.net` 2026-05-19 | `Working Docs/BETA1_M02_SLICE4_RELEASE_NOTE_2026_05_19.md` |
| Cerner connector | as part of the Cerner doc + connector framework | [CERNER_SETUP.md](./CERNER_SETUP.md) |
| ModMed connector | **not yet live-verified against a real practice** | [MODMED_SETUP.md](./MODMED_SETUP.md) |

## Operator upgrade notes

- New env var: `OWNCHART_CERNER_CLIENT_ID` — set after registering
  at code-console.cerner.com. See
  [INSTALL.md](./INSTALL.md)'s optional env-var table.
- ModMed's `OWNCHART_MODMED_CLIENT_ID` continues to apply; **no
  `OWNCHART_MODMED_CLIENT_SECRET`** (Patient app is a public PKCE
  client). If you see a confidential-client error, you registered
  as the wrong app type — re-register as Patient.
- Google Calendar env vars (`OWNCHART_GOOGLE_CALENDAR_CLIENT_ID` /
  `_SECRET` / `_REDIRECT_URI`) exist in `infra/.env.example` for the
  forthcoming Google adapter; they may be left blank in Beta 1
  without affecting any shipped behavior. Public copy stays at
  "iOS EventKit calendar foundation" until the Google adapter
  ships and is live-verified.
- Multi-tenant invite flow: first signup on a fresh DB still
  becomes the owner automatically. Owners issue invites from
  Settings; expired or used invite tokens return 403 with explicit
  codes.
- Export UI lives at the web app's Settings → Export surface for
  owner / caregiver roles. Files land under
  `data/exports/<job_id>/` and are hard-deleted at TTL.

## Public-claim posture for Beta 1

Three rules carry forward from the M02 PM directives:

1. **Calendar copy.** Public-facing calendar language is constrained
   to "iOS EventKit calendar foundation" or "recent iOS calendar
   context." Do not name Google / ICS / CalDAV as shipped adapters,
   and do not claim "years of calendar memory" or "full calendar
   history." The 90-day default window is intentionally not
   foregrounded — public copy describes the *capability category*,
   not the window.
2. **ModMed.** Documented as implemented with the explicit vendor
   live-OAuth caveat on the setup page. Don't claim live OAuth
   verified for ModMed in marketing copy.
3. **Live verification.** Every shipped Beta 1 item above has been
   live-verified on `ownchart.dzsec.net` (or, for Cerner, against
   the connector framework + the doc walkthrough). Items in
   "Held for post-release" have not — don't promote them.

— Beta 1 release cut, 2026-05-26.

---

## Beta 1.1 addendum — HealthKit MCP bridge (2026-05-30)

A small targeted increment on top of Beta 1. One user-visible
addition; nothing in the Beta 1 verification table or held list
moves.

### Shipped in Beta 1.1

#### HealthKit MCP bridge (local-only)

A published, open-source bridge that lets a **local MCP client on
the same Mac** (Claude Desktop, Claude Code, or Codex) read
aggregated HealthKit data from the iPhone while the OwnChart iOS
app is open. The bridge runs on macOS, talks to the iOS app over
the local Wi-Fi network, and exposes two MCP tools:
`healthkit_capabilities` (the capability registry) and
`healthkit_query_daily_summary` (aggregated daily summaries —
steps, active energy, heart rate ranges, sleep duration, etc.).

- npm: [`ownchart-hk-mcp-bridge`](https://www.npmjs.com/package/ownchart-hk-mcp-bridge)
- source: [github.com/nickpdawson/ownchart-hk-mcp-bridge](https://github.com/nickpdawson/ownchart-hk-mcp-bridge)
- OwnChart-side discovery: [HEALTHKIT_MCP.md](./HEALTHKIT_MCP.md)

**Requirements:** macOS + Node 20+. Mac and iPhone on the same
Wi-Fi network. OwnChart iOS app installed and **running in the
foreground** (with a brief grace period after you leave it). One
6-digit pairing code per Mac↔iPhone pair; revoke from OwnChart
Settings.

**Scope of what's exposed.** The bridge returns **aggregated daily
summaries** plus the capability registry. It does **not** expose
raw sample streams, GPS coordinates, workout routes, or medication
dose events. The full tool schema lives in the bridge repo's
README.

**Local-only posture.** The bridge is a **local** integration with
on-device MCP clients. There is **no cloud relay**, no OwnChart
backend in the data path, and **no ChatGPT remote-connector
support implied or provided**. The OwnChart iOS app must be open
on the phone when the bridge is queried — the bridge is not
always-on background infrastructure.

This is a distinct surface from the still-roadmap **MCP server
endpoint to the OwnChart backend** (Postgres-backed records on the
self-hosted instance). The backend MCP server remains on the
longer-term roadmap; see SHIPPED_VS_ROADMAP.

### Held items unchanged

Beta 1.1 does not move any Beta 1 held item. Specifically still
held / not shipped:

- ModMed live OAuth (no real-practice OAuth round-trip verified
  yet — see Beta 1 release notes).
- Reingest date provenance for historical rows.
- Canonical Spine — Phase 2.
- Export — async-with-progress + size cap.
- HealthKit steps ingest into OwnChart's backend.
- **HealthKit medication dose events** — `HKCategoryTypeIdentifierMedicationDoseEvent`
  is not in the Beta 1.1 ingest path or the MCP bridge tool surface.
  Other medication data sources (FHIR `MedicationStatement` /
  `MedicationRequest` from EHR connectors, screenshot extraction of
  Rx labels, manual entry) continue to work; only the HealthKit
  dose-event stream is held.

### Public-claim posture for Beta 1.1

The three rules from Beta 1 carry forward unchanged:

1. **Calendar copy.** Still constrained to "iOS EventKit calendar
   foundation" / "recent iOS calendar context." No flip in this
   addendum.
2. **ModMed.** Still documented with the explicit vendor-side
   live-OAuth caveat. Beta 1.1 does not verify ModMed OAuth.
3. **Live verification.** The HealthKit MCP bridge has its own
   acceptance grid in the bridge repo; this addendum claims local
   Mac↔iPhone verification only — no cloud, no remote connector,
   no always-on background behavior.

### Operator upgrade notes

- The bridge installs as a standalone npm package on the Mac:
  `npm install -g ownchart-hk-mcp-bridge && ownchart-hk-mcp-bridge pair`.
  No OwnChart server-side change is required.
- No new env var in `infra/.env`. The bridge does not touch the
  OwnChart backend.
- The iOS-side MCP server toggle lives at OwnChart → **Settings →
  Data & Ingestion → MCP server** in the iOS app.

— Beta 1.1 addendum, 2026-05-30.
