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

This is the load-bearing security control. All LLM calls in OwnChart — Anthropic, OpenAI, Gemini, **and local models** — pass through one function. Routing local-model calls through the same gate keeps the audit trail complete even though those payloads don't leave the host. Before the function assembles a payload, it checks:

1. **Global LLM consent flag** — set by the user in Settings. Default off.
2. **Per-source override** — any source can be flagged "never send to LLM", "source-only context", "exclude from Discover", or "exclude from Ask". Schema and enforcement land in v0.1b; the full UI for managing per-source flags is in scope for the v0.1b final.
3. **Per-person consent** — when an instance hosts multiple people (caregiver scenarios), each person has independent consent state.
4. **Privacy mode for this call** — one of:
   - `off` — call is refused.
   - `metadata_only` — only structured fields (dates, codes, categories). No free text. No images.
   - `selected_evidence` — excerpts the user has scoped to a specific question. **This is the default for Make Sense jobs.**
   - `full_source_allowed` — entire documents or images. Requires the user to be looking at an explicit "this will send the full source" affordance.

If any check fails, the call is refused before any PHI is loaded into memory for serialization. The refusal is logged (with no PHI in the log) to `ModelRun` so audits can answer "did this call ever go out?"

Pre-flight transparency: every AI job shows the user the scope, the privacy mode, and a summary of what evidence will be sent before execution. The user can change scope or privacy mode at the pre-flight, or cancel.

Architectural commitments that back the gate:

- **Single gate, multiple providers.** Adding a new LLM provider does not bypass consent — every provider client routes through the same gate. The gate is provider-agnostic.
- **Prompts are externalized YAML.** Hardcoded prompts can hide intent. YAML prompts version-control intent.
- **`ModelRun` audit record per call.** Provider, model, prompt version + SHA, job type, initiating user/admin, consent mode at call time, privacy mode, input source IDs and hashes, output hash, token usage, **estimated cost**, safety refusals (if any), and what the user did with the result. Two questions every audit answers: *"Why did OwnChart say this?"* and *"What did OwnChart send to the LLM?"*
- **No streaming of raw source bytes through the LLM client without an explicit privacy-mode flag.** This includes Vision OCR — Claude Vision (and any other vision model) is treated as an LLM call, gated identically.
- **Cost transparency.** Token usage and estimated cost are visible per call and per period. Optional monthly spend ceilings per user/instance.

## 4. Storage model

| Tier | What it holds | Where it lives | Encryption |
|---|---|---|---|
| Filesystem bind-mount (`data/`) | Original PDFs, page images, raw FHIR bundles, raw CCDA XML, OCR text outputs | A directory on the host filesystem you choose | At-rest, by the host (LUKS / FileVault / ZFS native) |
| Postgres | Structured facts, user corrections, episodes, queue state, audit trail, session tokens | Inside the `postgres` container, on a Docker volume | At-rest, by the host (same disk as `data/`) |
| Redis | Arq job queue, transient state | `redis` container, volume optional | None — keep ephemeral; do not persist if avoidable |
| Logs | Application logs (PHI-scrubbed by default; debug mode warns) | Stdout / files under `logs/` | At-rest, by the host |
| Secrets | API keys, DB password, session secret | `infra/.env` — gitignored | Filesystem permissions; not encrypted at rest in v0.1b |

Content-addressing: every original source is stored under `data/<sha256>/...` and looked up by its hash. Duplicate uploads dedupe to the same blob. If a file on disk has been tampered with, hash recomputation on access detects it.

## 5. Authentication, roles, and sessions

v0.1b auth:

- Local password authentication. Passwords hashed with **Argon2id**, parameters per OWASP 2023 guidance.
- Sessions are server-side; the cookie is `httpOnly`, `SameSite=Lax`, `Secure` when `OWNCHART_ENV=prod`.
- Default session max age: 14 days. Configurable in `infra/config.yaml`.
- No password reset flow unless SMTP is configured (`smtp.enabled: true` in config). Solo self-hosters run without SMTP — recover by resetting in the DB directly.

Role model (schema present in v0.1b; full UI maturing through 0.1b → 0.2):

| Role | Capabilities |
|---|---|
| **Owner** | Full instance control: billing, secrets, config, user management, ownership transfer |
| **Admin** | Manage users, connectors, settings, jobs, instance defaults (everything except ownership transfer) |
| **Member** | Manage own profile, person records, data, consent, preferences |
| **Caregiver / Delegate** | Scoped access to another person's record (with that person's consent) |
| **Viewer** | Read-only access to demo or explicitly-shared records |
| **Demo User** | Read-only sample data; safe for App Store review or public demo |

**Person vs. user:** the schema separates the *user account* (authentication) from the *person whose record this is*. One user can have a record of their own *and* delegated caregiver access to a parent's or child's record. Each person has independent consent state — including independent LLM consent, per-source overrides, and provider preferences.

Bootstrap behavior is configurable: by default the first user to create an account on a fresh instance becomes Owner; self-registration can then be closed.

Roadmap:

- **Authentik OIDC** as the recommended SSO front-end for households or caregivers.
- Mature **caregiver delegation UI** (the schema is in place in v0.1b; the management surface lands incrementally).

## 6. Secrets

All secrets live in `infra/.env`. The file is gitignored and the deploy script refuses to start if any of the marked secrets are unset or still equal to the placeholder.

Variables you must set before running anything against your real record:

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | Postgres user password |
| `SESSION_SECRET` | Random 48+ byte URL-safe token, generated locally |
| `OWNCHART_LLM_PROVIDER` | Default provider (`anthropic`, `openai`, `google`, `local`) |
| At least one provider key | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, or a `LOCAL_LLM_ENDPOINT` URL — whichever matches your default provider |

Provider credentials can be admin-provided (set once in `.env`, available to all users on the instance), user-provided (each user enters their own key in Settings), or come from a provider OAuth login when supported. Mixed deployments are supported — the instance can offer a default model and let individual users override with their own key.

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

## 11. Safety boundary

OwnChart's AI never provides medical advice. Concretely, the system is constrained to refuse and redirect on certain categories of request:

- **Medication changes.** AI never instructs you to start, stop, change dose, or substitute a medication. It can explain a medication, summarize its history in your record, and help you frame questions for your clinician — but the action remains yours and your clinician's.
- **Diagnostic verdicts.** AI does not deliver diagnoses. It can surface what the institutional record claims, what evidence supports or contradicts that, and what questions to ask — never "you have X."
- **Self-harm intent.** If a user expresses self-harm intent, or asks for instructions or support for self-harm, the system responds with supportive, crisis-oriented guidance and a strong encouragement to reach out for immediate human help (988 Suicide & Crisis Lifeline in the US; appropriate regional services elsewhere). The system never provides instructions, planning support, or rationalization for self-harm.
- **Correlation vs. causation.** When the system surfaces a pattern (e.g., "sleep dropped in the week before this surgery"), it labels it as correlation. It does not assert causal claims the data cannot support.

These boundaries are enforced at the prompt level (system prompts explicitly instruct the model on refusal-and-redirect behavior) and at the audit level (`ModelRun.safety_refusal` records when the model refused, so audits can verify the boundary is holding).

## 12. Reporting a vulnerability

If you find a security issue in OwnChart, please **do not file a public issue**. Open a private GitHub Security Advisory at:

`https://github.com/nickpdawson/OwnChart/security/advisories/new`

Or email the project owner directly with the details and a proposed disclosure timeline. We will respond as fast as a one-person project can, which means within a few days, not minutes. If you've found something exploitable in the consent gate specifically, that's the highest-priority class — flag it as such.

## 13. Known limitations in 0.1b

Documented honestly:

- **Per-source override UI** — schema is in place and gate enforcement works in v0.1b; the management UI for setting source-level flags lands incrementally through 0.1b. The global consent gate is the load-bearing control today.
- **Caregiver / multi-person UI** — schema present in v0.1b, full delegation UI matures through 0.1b → 0.2.
- **No application-layer encryption.** Relies on host disk encryption. Application-layer encryption of `data/` is a candidate for v0.2.
- **No automated PHI scanning** on outbound LLM calls (e.g., detecting that a free-text note contains an SSN before sending). The privacy modes constrain what categories of data go out; finer-grained content scanning is a roadmap item.
- **Backups are operator-implemented.** A first-class backup tool is on the roadmap.
- **Audit log is append-only by convention, not by storage primitive.** A tamper-evident audit log (hash-chained or external-anchored) is on the roadmap.

If any of these are a blocker for your threat model, do not yet run OwnChart against your real record. v0.1b is a public beta; the doctrine is firm but the implementation is still maturing.
