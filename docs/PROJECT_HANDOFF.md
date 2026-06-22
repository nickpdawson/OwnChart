# OwnChart Project Handoff

**Snapshot date:** 2026-06-22
**Audience:** the next PM, engineering lead, or operator taking over OwnChart
**Purpose:** preserve the product decisions, release boundaries, and current handoffs needed to continue safely. This document is deliberately PHI-free and safe to track in Git.

## Start Here

OwnChart is a self-hosted, person-owned health research workspace. It is not a patient portal or an EMR clone. The product turns durable evidence into a longitudinal record that a person can inspect, correct, and ask questions about.

The governing rule is simple: **accuracy beats fluency**. A useful answer or import must stay tied to source evidence, state uncertainty plainly, and never turn an empty result into a successful result.

Before changing behavior, read:

- `README.md` for product framing and public claims.
- `user-docs/SHIPPED_VS_ROADMAP.md` for the authoritative shipped-versus-held split.
- `user-docs/RELEASE_NOTES_BETA1.md` for public release history.
- `docs/HEALTHKIT_MCP_SPIKE.md` and `docs/HEALTHKIT_MCP_BRIDGE_SPEC.md` for the tracked HealthKit MCP contract.
- `Working Docs/` for private design notes and current follow-ups. It is intentionally gitignored; it is not a substitute for this handoff or public operator documentation.

## Repository And Ownership Map

| Surface | Source of truth | Notes |
|---|---|---|
| OwnChart server and web app | This repository | `api/`, `web/`, migrations, infra, public user documentation. |
| iOS app | `OwnChartiOS/` on the operator's local disk | Intentionally gitignored in this repository. iOS handoff documents must name source paths and provide a reviewable diff or archive procedure. |
| Local HealthKit MCP bridge | `github.com/nickpdawson/ownchart-hk-mcp-bridge` | Separate MIT repository and npm package. The bridge README is canonical for install and troubleshooting. |
| Public operator documentation | `user-docs/` | Safe to publish; do not put private discovery notes, credentials, PHI, or local network details here. |
| Private working notes | `Working Docs/` | Local-only. Good for work plans and raw handoffs, not the only record of a product decision. |

## Non-Negotiable Product Rules

1. **Evidence is primary.** Preserve original sources and provenance. Facts, summaries, and AI output are derived layers, not replacements for source material.
2. **Health is longitudinal.** Clinical care, HealthKit data, calendar context, notes, documents, and life events belong in one person-owned record when the person chooses to connect them.
3. **Record boundaries are a security boundary.** Every data operation is scoped to an explicit person record. A sync must pin the active record at orchestration start. Do not quietly fall back to another membership when multiple records are available.
4. **An attempt is not a success.** A sync timestamp, HTTP 200, or empty bundle must not imply that source data arrived. Use explicit outcomes such as `ok`, `empty`, `partial`, `auth_expired`, and `failed`.
5. **Do not infer HealthKit read authorization from `authorizationStatus(for:)`.** It cannot distinguish all of the states that matter for read access. Use explicit user authorization flows and actual query outcomes.
6. **No PHI or secrets in Git, logs, demos, or agent reports.** Use count-only diagnostics when investigating real records. Keep secrets in environment configuration, never source files.
7. **Public claims follow verified behavior.** A feature remains held until its end-to-end acceptance gate passes. Do not market a scaffold, a UI control, or an unverified connector as shipped.
8. **No silent clinical authority.** The app may explain source-backed language and uncertainty; it must not issue treatment orders, dosing changes, or replace a care plan.

## Release Baseline

| Release | Status | Highlights |
|---|---|---|
| `v0.1.0-beta1` | Shipped | Multi-record safety fixes, connector removal, Cerner support, export UI, release stabilization. |
| `v0.1.0-beta1.1` | Shipped | Local iOS HealthKit MCP foundation and published Mac bridge. |
| `v0.1.0-beta1.2` | Shipped | Pictal Health Record v1.0 JSON export. |
| `v0.1.0-beta1.3` | Shipped | `general_ask` v5 pathology/clinical-phrase interpretation behavior. |

At this handoff, `main` and `origin/main` are at `v0.1.0-beta1.3` (`3dece87`). Always verify branch and deployment state before making a new release decision.

### Release Discipline

- Keep unrelated work out of a release branch. Do not stage or delete pre-existing local artifacts such as `.wrangler/`, `api/uv.lock`, or `web/package-lock.json` without an explicit repository-policy decision.
- Treat a fast-forward merge, push, tag, production deploy, and demo deploy as separate approvals.
- Back up production before migrations or deploys. Verify migration revision and database checks after deployment.
- A release requires focused tests plus the relevant live smoke. For health data, report counts and invariant checks rather than values, tokens, patient names, or full payloads.
- Do not use destructive Git recovery commands against a dirty worktree. Preserve unrelated work.

## Shipped Capabilities And Important Boundaries

### FHIR Connectors

- Cerner / Oracle Health was verified live and is a Beta 1 capability.
- Epic and athena flows exist under their documented registration models.
- ModMed / EMA implementation and documentation are staged, but live production OAuth remains unverified because of vendor-side patient/firm/app entitlement behavior. It is a post-release hotfix lane, not a feature to promote.
- A connector that expires or receives all-401 source responses must become visibly expired or failed. Never update `last_synced_at` and report success after an all-401 run.

### Exports And Pictal Health

- Pictal Health Record v1.0 JSON is shipped as an explicit export choice. OwnChart produces a download; it never sends records to Pictal.
- The mapper is deterministic and contains no LLM.
- High-volume HealthKit / auto-export body signals are excluded.
- CCDA export remains held.
- Medication dose events remain excluded from Pictal until a separate, intentional mapping and privacy review is accepted.
- Small UI follow-up: when Pictal JSON is selected, body-signal selection should be visibly disabled or labelled as ignored rather than appearing to affect the export.

### HealthKit MCP And The Bridge

The initial topology is local only:

```text
MCP client on Mac -> ownchart-hk-mcp-bridge (stdio) -> iPhone local HTTP/Bearer -> HealthKit
```

- Install path: `npm install -g ownchart-hk-mcp-bridge`.
- The iOS server is foreground-first, with up to five minutes of background grace only when iOS permits it. It is not a cloud relay or an always-on daemon.
- Pairing is persistent until explicitly revoked. Discovery uses Bonjour plus a stable iPhone `server_id`; transport failures do not erase trust tokens.
- The bridge is deliberately narrow. Its original tool surface is `healthkit.capabilities` and `healthkit.query_daily_summary`; it does not provide HealthKit writes, raw sample streams, routes/GPS, a remote ChatGPT connector, or backend MCP access.
- Workouts and sleep were added to the daily-summary path. Medication dose events are intentionally not part of `query_daily_summary`.
- The iOS HealthKit access screen must be explicit about what the app can request. A completed bridge handshake proves transport, not permission or data availability.

## Current Hold: HealthKit Medication Dose Events

**Status:** held. Do not deploy, archive, tag, or make public claims for this lane until the acceptance gate below is satisfied.

Medication dose events require their own Apple authorization and query model. They are not an ordinary quantity sample and must not be bolted into `healthkit.query_daily_summary`.

### Existing Groundwork On `dev`

The following commits are intentionally ahead of `main` and are not a release candidate by themselves:

| Commit | Meaning | Release posture |
|---|---|---|
| `649cce6` | Canonical medication dose-event identifier correction and public wording correction. | Held. |
| `cb9c1f7` | Backend accepts medication wire fields and canonical identifier. | Held. |
| `421d1e0` | Historical identifier alias normalization before registry validation. | Held. |
| `f58999f` | Initial acceptance-test scaffold. | Held; it documents aggregate behavior that must be replaced before shipping. |

### Required Build-47 Correction

The current aggregate-oriented medication payload is not safe to ship. The next implementation must satisfy all of these conditions:

1. **One source event per `HKMedicationDoseEvent`.** Do not collapse multiple doses into a single daily cell during ingestion.
2. **Stable identity from the HealthKit event UUID.** Derive `client_sample_key` from the canonical identifier and event UUID, preferably with SHA-256. Never derive identity from display text, local day, or a user-facing label.
3. **Preserve source truth.** Each event carries canonical identifier, Apple event time, coded medication reference, source provenance, and an Apple-supported controlled status.
4. **Unknown is not zero.** Omit or set null for unavailable optional medication fields. Do not transmit zero as a placeholder.
5. **Explicit record choice.** The iOS upload must use the pinned active record. If more than one eligible record exists and none is selected, stop and show the picker; do not fall back to the first membership.
6. **No implicit authorization.** Only the Medications screen's user action may invoke the medication picker. Normal HealthKit sync, app launch, and MCP requests must never prompt.
7. **No premature downstream surface.** Do not add medication doses to MCP daily summaries, Pictal export, or broad retrieval/LLM exposure in this slice.

### Required Acceptance Before TestFlight Or Deployment

- Two taken doses for the same medication on the same local day upload as two distinct source events.
- Repeating the same upload creates no duplicate events.
- A display-name change leaves the event identity unchanged.
- Unknown optional values serialize as absent or null.
- Declined access, no tracked medications, and no events are distinguishable in UX and diagnostics.
- Multi-record upload refuses until an explicit record is selected.
- The full API suite passes with no excluded coordination tests; the iOS payload test is tracked and green.
- A physical-device smoke validates the explicit picker, source-event upload, and target-record behavior using count-only output.

Only after this gate should the compatible backend deploy and the iOS build archive. Public documentation continues to say medication dose events are held until the full vertical slice, including privacy review and downstream policy, is accepted.

## Known Follow-Ups And Risks

| Priority | Area | Current direction |
|---|---|---|
| P0 | Stanford / Epic sync outcome truthfulness | An all-401 Stanford sync previously reported success, advanced `last_synced_at`, and wrote empty placeholder bundles. Confirm whether the planned `auth_expired` / explicit sync-status fix was implemented. If not, treat it as release-critical connector correctness work. |
| High | Medication dose events | Complete Build-47 source-event correction before any deployment or TestFlight archive. |
| High | Multi-record HealthKit uploads | Do not allow a new iOS feature to inherit an unsafe first-membership fallback. |
| Medium | ModMed / EMA | Await vendor-side entitlement/patient-login resolution; keep public verification caveat intact. |
| Medium | Pictal UI | Make body-signal exclusion clear in the Pictal export form. |
| Medium | HealthKit medication MCP | Design a dedicated, consented tool after direct-sync correctness and privacy gates land. Do not add it to daily summaries. |
| Low | iOS HealthKit query coverage | Investigate gaps such as BodyMass only through actual query results and explicit permission flows, not inferred authorization status. |

## AI Answering Policy: `general_ask` v5

The active `general_ask` prompt version adds a pathology/radiology/clinical-phrase interpretation scaffold. When a person asks what a phrase means, the answer should:

1. Give a direct plain-language interpretation with appropriate hedging.
2. Explain important non-equivalences rather than flattening them.
3. Connect the phrase to retrieved evidence and the clinical sequence.
4. Name the practical remaining confirmation.
5. State the specific evidence gap, if one exists.

Avoid making "ask your doctor" the dominant conclusion when the retrieved evidence already supports a useful interpretation. Do not demand a full PDF merely to explain language that is already present in evidence. The safety boundary remains: no treatment orders, dosing changes, or replacement of a clinician's plan.

## How To Take Over Safely

1. Start with `git status --short`, current branch, current tags, and deployment health. Do not assume this handoff's snapshot is still current.
2. Read the relevant handoff and tests before editing shared contracts, especially iOS wire shapes, person-record scoping, and export formats.
3. Keep iOS, backend, bridge, and docs lanes separate. A change to one must name what it expects from the others and what remains held.
4. Use a narrow vertical slice for health-data changes: Apple authorization/query behavior, iOS wire shape, backend validation/storage, actual device test, then public documentation.
5. Release only claims that are live-verified. Mark everything else as held, including an implemented but vendor-blocked connector.
6. Preserve source evidence and diagnostics. When investigating live data, use counts and invariants; never paste records, tokens, URLs containing session state, or identifiers into commits or reports.

## Maintenance Rule

Update this document whenever any of these change:

- a release is tagged or deployed;
- a cross-platform contract becomes authoritative;
- a feature moves between held and shipped;
- an unresolved P0 changes owner or disposition;
- source-of-truth locations or release procedure change.

For day-to-day implementation details, use focused handoffs. This document should stay short enough to orient a new owner, but complete enough to prevent accidental feature claims, cross-record data leakage, or misleading health-data success states.
