# Security Model

> The threat model, the controls, and what you have to do as an operator. OwnChart is self-hosted PHI infrastructure — there is no managed service standing between you and the consequences of misconfiguration. Read this end-to-end before you run it against your own record.

## 1. What OwnChart is defending

The asset is the patient's record. In OwnChart that asset includes:

- Source documents (PDFs, CCDA XML, FHIR bundles, page images).
- Extracted facts and user corrections (the structured chart).
- LLM prompts and responses (every prompt against the record contains PHI).
- Logs, queues, and job artifacts (assume any of them may contain PHI fragments).
- OAuth refresh tokens for connected EHRs (effectively a re-fetchable copy of the record).
- Authentication credentials for the OwnChart instance itself.

Every control below exists to protect those assets from one of three threat classes:

| Threat | Example |
|---|---|
| **Egress** | PHI leaving the host without explicit consent. The headline risk. |
| **Unauthorized access** | Someone other than the patient reading the record locally or remotely. |
| **Loss of integrity** | A bug or attacker silently overwriting source records or user corrections. |

## 2. What OwnChart is **not** defending against

Stated explicitly so you don't assume coverage that isn't there:

- **Compromise of the host OS.** If an attacker has root on your server, they have your PHI. OwnChart cannot save you from that. Patch your kernel.
- **Compromise of your laptop while you're logged into the web UI.** Session cookies are session cookies.
- **Compromise of your LLM provider account.** If your Anthropic API key is exfiltrated, the attacker can talk to Claude on your bill, but cannot pull your PHI back through that channel (it flows host → API, not the reverse). Still — rotate the key.
- **Disk-level encryption.** OwnChart assumes the disk under `data/` is encrypted at rest by the host (LUKS, FileVault, ZFS native encryption). Application-layer encryption is not implemented in 0.1b.
- **Court orders / lawful subpoena against you personally.** OwnChart has no plaintext-key escrow, but if you control the keys, you can be compelled to produce them. This is a feature of self-hosting, not a bug.

## 3. The consent gate is the egress checkpoint

This is the load-bearing security control. All LLM calls in OwnChart pass through one function. Before that function assembles a payload, it checks:

1. **Global LLM consent flag** — set by the user in Settings. Default off.
2. **Per-source override** — any source can be flagged "never send to LLM" (v0.1b: schema in place; full UI in v0.2).
3. **Privacy mode for this call** — one of:
   - `off` — call is refused.
   - `metadata` — only structured fields (dates, codes, categories). No free text. No images.
   - `selected_evidence` — excerpts the user has scoped to a specific question.
   - `full_source` — entire documents or images. Requires the user to be looking at an explicit "this will send the full source" affordance.

If any check fails, the call is refused before any PHI is loaded into memory for serialization. The refusal is logged (with no PHI in the log) to `ModelRun` so audits can answer "did this call ever go out?"

Architectural commitments that back the gate:

- **Single egress path.** The Anthropic client is the only LLM client in the codebase. Adding a second provider would require routing through the same gate.
- **Prompts are externalized YAML.** Hardcoded prompts can hide intent. YAML prompts version-control intent.
- **`ModelRun` audit record per call.** Includes model, prompt version, input source IDs and hashes, output hash, consent mode at call time, token usage, and what the user did with the result.
- **No streaming of raw source bytes through the LLM client without an explicit privacy-mode flag.** This includes Vision OCR — Claude Vision is treated as an LLM call, gated identically.

## 4. Storage model

| Tier | What it holds | Where it lives | Encryption |
|---|---|---|---|
| Filesystem bind-mount (`data/`) | Original PDFs, page images, raw FHIR bundles, raw CCDA XML, OCR text outputs | A directory on the host filesystem you choose | At-rest, by the host (LUKS / FileVault / ZFS native) |
| Postgres | Structured facts, user corrections, episodes, queue state, audit trail, session tokens | Inside the `postgres` container, on a Docker volume | At-rest, by the host (same disk as `data/`) |
| Redis | Arq job queue, transient state | `redis` container, volume optional | None — keep ephemeral; do not persist if avoidable |
| Logs | Application logs (PHI-scrubbed by default; debug mode warns) | Stdout / files under `logs/` | At-rest, by the host |
| Secrets | API keys, DB password, session secret | `infra/.env` — gitignored | Filesystem permissions; not encrypted at rest in v0.1b |

Content-addressing: every original source is stored under `data/<sha256>/...` and looked up by its hash. Duplicate uploads dedupe to the same blob. If a file on disk has been tampered with, hash recomputation on access detects it.

## 5. Authentication & sessions

v0.1b:

- Local password authentication only. Passwords hashed with **Argon2id**, parameters per OWASP 2023 guidance.
- Sessions are server-side; the cookie is `httpOnly`, `SameSite=Lax`, `Secure` when `OWNCHART_ENV=prod`.
- Default session max age: 14 days. Configurable in `infra/config.yaml`.
- No password reset flow unless SMTP is configured (`smtp.enabled: true` in config). Solo self-hosters run without SMTP — recover by resetting in the DB directly.
- No multi-user separation yet. v0.1b assumes one human per instance. Multi-user with caregiver delegation is on the roadmap.

Roadmap:

- **Authentik OIDC** as the recommended SSO front-end for households or caregivers.
- **Per-record consent boundaries** when one server hosts multiple people (parent + child + parent's parent).

## 6. Secrets

All secrets live in `infra/.env`. The file is gitignored and the deploy script refuses to start if any of the marked secrets are unset or still equal to the placeholder.

Variables you must set before running anything against your real record:

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | Postgres user password |
| `SESSION_SECRET` | Random 48+ byte URL-safe token, generated locally |
| `ANTHROPIC_API_KEY` | Your Anthropic API key, billed to you |

Never:

- Commit `.env` to git. The gitignore is aggressive on `.env`, `.env.*`, `*key.txt`, `*secret*` — but operator vigilance is still the control of last resort.
- Bake secrets into Docker images.
- Put secrets in `infra/config.yaml`. That file is intended to be diffable in code review.

Rotation:

- `SESSION_SECRET` rotation invalidates all existing sessions. Run on suspected compromise.
- `ANTHROPIC_API_KEY` rotation: revoke at console.anthropic.com, replace in `.env`, restart the API container.
- Database password rotation: standard Postgres `ALTER USER`, update `.env`, restart.

## 7. HAR captures and reverse engineering

For connectors to portals that don't expose FHIR, the patient may capture a HAR file from their authenticated browser session to understand the request shape. This is a legitimate, patient-side workflow — but HAR files contain auth cookies, OAuth tokens, and frequently the full payload of clinical API responses.

OwnChart's policy:

- `*.har` and `*.har.gz` are gitignored aggressively.
- Any HAR analysis happens **locally only**. The redactor strips cookies, `Authorization` headers, CSRF tokens, and known sensitive headers before any display surface.
- HAR contents are not sent to LLMs without explicit per-file scoping and the consent gate's `selected_evidence` mode.
- The `KP_API.md` style of reverse-engineering analysis (analyzing one's own portal session to design a patient-mediated connector) is treated as an internal artifact, not a public one. It lives outside the public repo.

## 8. Logging posture

Default:

- Application logs structured JSON to stdout.
- A PHI-safe logger wrapper redacts known PHI fields and refuses to log raw request/response bodies.
- `OWNCHART_DEBUG_PAYLOADS=true` (or `privacy.debug_payloads_default: true` in config) flips on raw-body logging. This is **an operator decision with a PHI risk**, surfaced with a warning at startup.

What never appears in logs by default:

- Anthropic API keys (logger redacts `sk-ant-` prefixes).
- Session cookies / tokens.
- Full source document text.
- Full LLM prompts or responses (only `ModelRun.id` references).

What does appear:

- Request method, path, status, latency.
- `ModelRun.id` references for audit trail correlation.
- Errors with stack traces (no payload bodies).
- Job queue events with source IDs but not contents.

## 9. Reverse proxy / network posture

Recommended deployment:

- Bind OwnChart only to localhost (`127.0.0.1:8800`) inside the host.
- Front with a reverse proxy you control (Nginx Proxy Manager, Caddy, nginx, Traefik).
- Terminate TLS at the proxy with a real cert (Let's Encrypt or your internal CA).
- Restrict access to your tailnet, VPN, or LAN. OwnChart is not designed to be exposed to the public internet, even with auth, in v0.1b.

Headers the proxy should set or pass:

- `X-Forwarded-Proto: https` — so OwnChart marks cookies `Secure`.
- `X-Forwarded-For` — for accurate audit logs.

Headers OwnChart sets:

- `Strict-Transport-Security` (when `OWNCHART_ENV=prod`).
- `X-Content-Type-Options: nosniff`.
- `X-Frame-Options: DENY`.
- `Content-Security-Policy` — restrictive default; configurable for forks adding embeds.

## 10. Backups and recovery

OwnChart does not ship a backup system. You back up:

1. `data/` — your bind-mount directory (encrypted snapshot to an encrypted destination).
2. The Postgres volume — `pg_dump` on a schedule, encrypted at rest at the destination.
3. `infra/.env` and `infra/config.yaml` — separately, treated as secret material.

A good drill: restore both `data/` and the Postgres dump to a clean OwnChart instance, log in, and verify a known-correct source still resolves with its user corrections intact.

## 11. Reporting a vulnerability

If you find a security issue in OwnChart, please **do not file a public issue**. Open a private GitHub Security Advisory at:

`https://github.com/nickpdawson/OwnChart/security/advisories/new`

Or email the project owner directly with the details and a proposed disclosure timeline. We will respond as fast as a one-person project can, which means within a few days, not minutes. If you've found something exploitable in the consent gate specifically, that's the highest-priority class — flag it as such.

## 12. Known limitations in 0.1b

Documented honestly:

- **Per-source "never send to LLM" override** — schema is in place, full UI is v0.2. The global consent gate is the load-bearing control today.
- **No application-layer encryption.** Relies on host disk encryption. Application-layer encryption of `data/` is a candidate for v0.2.
- **No multi-user isolation.** One human per instance.
- **No automated PHI scanning** on outbound LLM calls (e.g., detecting that a free-text note contains an SSN before sending). The privacy modes constrain what categories of data go out; finer-grained content scanning is a roadmap item.
- **Backups are operator-implemented.** A first-class backup tool is on the roadmap.
- **Audit log is append-only by convention, not by storage primitive.** A tamper-evident audit log (hash-chained or external-anchored) is on the roadmap.

If any of these are a blocker for your threat model, do not yet run OwnChart against your real record. v0.1b is a public beta; the doctrine is firm but the implementation is still maturing.
