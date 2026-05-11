<p align="center">
  <img src="./OwnHealth%20Icon.svg" alt="OwnChart icon — five planets on a horizon line" width="128" height="128" />
</p>

# OwnChart

**An AI-first, evidence-grounded, patient-owned research partner for health and life data. Self-hosted. Your records, your model, your rules.**

> **0.1b — documentation drop.** This initial commit publishes the doctrine, security model, and connector setup guides for the upcoming 0.1b public beta. **The source code lands in this repo when 0.1b ships.** Until then, read [PHILOSOPHY.md](./PHILOSOPHY.md), [SECURITY.md](./SECURITY.md), and the [user-docs/](./user-docs/) connector guides — and tell us if anything in the design model needs to change before code touches it.
>
> Working title; name may change.

---

## What this is

Closer to **Cursor for health and life data** than to **MyChart for patients**.

OwnChart is not a record viewer. It is a research partner. You bring your data — every PDF, FHIR bundle, CCDA, HealthKit metric, faxed report, and personal note you can pull together — and OwnChart brings structure, memory, retrieval, citations, and AI-assisted reasoning. You ask questions of your own health life. OwnChart answers with evidence, cites every source, and admits what it doesn't know. You stay the final authority on what anything means.

The goal is **patient parity**: a person, working with AI, reaching the same level of insight into their own record as the people who normally have the tools, training, and institutional access. Not impersonating a clinician. Not giving medical advice. Helping you ask better questions, understand your own evidence, see longitudinal patterns, surface what might matter, and prepare for conversations with clinicians from a position of agency.

## Why this exists

The American medical record is fragmented by design. Your data sits in a dozen portals, formatted for billing, optimized for the institution, and made deliberately hard to take with you. When you need to make a decision — a second opinion, a surgery, a slow-burning symptom that nobody's connecting the dots on — you are the one piecing it together. From PDFs. From memory.

OwnChart is the system that should already exist for that work:

- One longitudinal record, built from every source you can pull (EHR APIs, CCDAs, faxes, your iPhone, your notes, your memory).
- Raw sources preserved exactly as received, forever. Your corrections and annotations layered on top — never overwriting the source.
- Local-first storage. PHI never leaves your machine without your explicit, scoped consent.
- AI as a thinking partner: ask in natural language, get cited answers, save conversations as part of your longitudinal learning.
- No vendor telemetry. No SaaS backend. No institutional override.

The patient is the user. The patient owns the record. The patient owns the server. The patient owns the corrections, the questions, the canonical version of their own story.

## Doctrine

The non-negotiables. Read them as constraints on every feature decision, not aspirations.

1. **AI-first is not AI-magical.** AI is the primary interaction model — but every substantive AI statement is source-backed, user-canonical, inferred, statistical, or explicitly unknown. Never opaque. Never silent canonicalization.
2. **Raw sources are immutable.** Original PDFs, FHIR bundles, CCDA XML are content-addressed (SHA-256) and never modified.
3. **User correction is canonical.** Your version becomes the displayed truth. The source is preserved as evidence, never erased.
4. **One consent gate, on the egress path.** No PHI leaves the host to any LLM provider — local or remote — without explicit, scoped opt-in. (See [SECURITY.md](./SECURITY.md).)
5. **No third-party telemetry.** Logs, prompts, embeddings, queue payloads are all treated as PHI. No Sentry, no Mixpanel, no crash reporter.
6. **FHIR-native at the edges, human-native in the core.** Standards at the boundary; lived experience in the middle.
7. **Significance over fact-count.** Source density is not meaning. The product ranks by user-confirmable significance.
8. **Provenance is auditable for every AI output.** `ModelRun` records: provider, model, prompt version, inputs sent, output, consent mode, user action. "Why did OwnChart say this?" always has an answer.
9. **Patient memory is evidence.** Your notes, photos, and corrections sit alongside clinical facts in timelines and dossiers — not as a separate "patient-reported" ghetto.
10. **Doctrine travels with the fork.** MIT license on the code; this doctrine is what makes a fork still patient-owned.

Full treatment in [PHILOSOPHY.md](./PHILOSOPHY.md).

## AI as a first-class citizen

OwnChart treats AI as the primary interaction surface, not a sidecar. Five interaction modes:

| Mode | What it does | Examples |
|---|---|---|
| **Ask** | Natural-language questions across your record, with cited answers | "Tell me the story of my strabismus." "What changed after my May 1 surgery?" "What should I review before Friday's appointment?" |
| **Make Sense** | Organize a source, period, dossier, or review queue. Produces *candidates*, not silent mutations | "Make sense of this Stanford import." "Organize 2026." "Clean up this review queue." "Explain this episode." |
| **Discover** | Proactive suggestions of things worth exploring, with evidence preview and signal strength | "These procedure rows may be one surgery." "Sleep and resting HR available before and after this surgery." |
| **Explain** | Translate clinical language into plain English without flattening nuance. Original always preserved | `"PLMT ADJUSTABLE SUTR STRABISMUS"` → `"Adjustable-suture strabismus surgery"` |
| **Compare** | Deterministic statistics plus AI explanation for periods, signals, eras | "Before/after surgery." "Medication era vs. baseline." "Sleep around the injury." |

### Conversations are a first-class product object

Ask conversations are **saved automatically** — with scope, sources used, citations, model, prompt version, privacy mode, and timestamp. They are searchable, resumable, and pinnable to dossiers. Your conversation history is part of your longitudinal learning, not disposable chat. The Home screen surfaces *Continue researching* alongside *Worth noticing* and *Make Sense*.

### Evidence Contract

Every substantive AI statement is one of:

- **Source-backed** — directly stated in a source you control.
- **User-canonical** — confirmed or corrected by you. Overrides the source for display.
- **Inferred** — reasoned but not directly stated. Marked as inference.
- **Statistical** — aggregate or correlation. Includes the underlying method.
- **Unknown** — insufficient evidence. The product says so plainly.

Every answer supports a one-click "why do you think that?" path into the source page, section, excerpt, confidence, and any correction you've made.

### Multi-provider, not Anthropic-only

OwnChart is designed for **model pluralism**. The consent gate is uniform across providers; the rest is your choice:

- **Anthropic Claude** — the reference provider in 0.1b.
- **OpenAI** — supported alongside Claude.
- **Google Gemini** — supported alongside the above.
- **Local models** (Ollama, llama.cpp endpoints) — for users who never want PHI to leave the host at all.
- **Admin-provided credentials** vs **user-provided API keys** vs **provider OAuth** — all configurable per instance.

Settings let you pick default provider, default model, and **per-task model preferences** (e.g., a smaller cheaper model for label translation, a stronger model for Ask). The UI shows you which provider answered any given question.

### Two architectural commitments

- **Prompts are externalized.** Every prompt lives in `api/ownchart/prompts/*.yaml`, version-controlled. No hardcoded strings. `ModelRun.prompt_version` cites file + SHA.
- **AI never mutates canonical data directly.** It produces *candidates* — suggested labels, episode groupings, duplicates, summaries. You accept, edit, or reject. The accepted version becomes your canonical assertion.

If you turn LLM consent off entirely, OwnChart still works — ingest, browse, search, timeline, manual correction, deterministic Discover all run locally. You lose Ask, Make Sense, Vision OCR fallback, and the explain/compare modes. That's the trade, and you get to make it.

## Primary product objects

OwnChart is built around what users actually think about, in priority order:

| Object | What it is |
|---|---|
| **Questions** | What you want to understand. Natural language, scoped to whole record / period / dossier / source |
| **Conversations** | Saved, searchable, resumable Ask + Make Sense threads with all their evidence |
| **Moments** | Important things that happened (surgery, diagnosis, hospitalization, turning point) |
| **Episodes** | Related moments over a period — system-proposed clusters (e.g., "May 2026 surgery + recovery") |
| **Patterns** | Trends, correlations, gaps, changes, repetitions surfaced by Discover |
| **Dossiers** | Living case files about a topic — your research workspace for a condition or thread |
| **Sources** | Evidence, available when you want to verify (PDFs, FHIR bundles, CCDA, etc.) |
| **Facts** | Supporting evidence units. Substrate, not the default view |

Facts are last on purpose. The product does not ask you to become a database administrator before you receive value.

## Security model

The headline: **PHI lives on your disk and stays there unless you explicitly send it somewhere else.**

- **Self-hosted only.** Docker Compose on your hardware. No SaaS backend exists.
- **Bind-mount storage** for raw sources; Postgres 16 + pgvector for the structured layer. Disk-level encryption (LUKS / FileVault) is the deployer's responsibility — OwnChart assumes it.
- **Consent gate as the egress checkpoint.** Every external LLM call passes through one function that checks: global consent flag, per-source override, per-person consent, and privacy mode. If any check fails, the call is refused before any payload is assembled. Local-model calls also flow through the gate so the audit trail is complete, even though they don't leave the host.
- **Privacy modes:** `off`, `metadata_only`, `selected_evidence` (default for Make Sense), `full_source_allowed` (requires explicit acknowledgment).
- **Per-source overrides:** any source can be marked "never send to LLM", "source-only context", "exclude from Discover", or "exclude from Ask".
- **No telemetry. No analytics. No crash reporter.** Errors stay on the host.
- **Safety boundary.** AI never instructs you to start, stop, or change medication. Self-harm intent gets crisis-oriented support and a referral to human help — never assistance with the act.
- **Cost transparency.** Every `ModelRun` records token usage and estimated cost. Optional monthly spend ceilings.
- **Secrets in env vars only.** Never in YAML. Never in git. `infra/.env` is gitignored; `infra/.env.example` is the template.
- **HAR redaction.** Browser captures used for connector development have cookies and tokens stripped before display; HAR files are aggressively gitignored.
- **Argon2id local passwords** in v0.1b. Authentik OIDC + caregiver delegation next.

Full threat model, role model, and operator checklist: [SECURITY.md](./SECURITY.md).

## What's in 0.1b

Shipping:

- **Ask** — natural-language questions across your record with cited answers, scope controls, retrieved-evidence disclosure, and visible privacy mode.
- **Make Sense** — user-initiated sensemaking of sources, periods, dossiers, and review queues. Produces candidates the user accepts/edits/rejects.
- **Discover** — deterministic + LLM-assisted suggestions (dense periods, episode candidates, duplicates, source conflicts, metric changes, suggested dossiers, data-quality issues).
- **Conversations** — saved, searchable, resumable. Pinnable to dossiers.
- **Dossiers** — living case files per topic with hero metrics, executive brief, evidence clusters, and conversation thread.
- **Timeline** — global and topic-scoped, ranked by user-confirmable significance.
- **Review Inbox** — confirm/correct extracted facts. Lane split, bulk triage, source-level summaries. Distinct from Make Sense candidates.
- **Evidence Vault** — every claim links back to its source page; sources grouped by time, type, and contribution.
- **Connections** — see EHR / HealthKit / Auto Export status, last sync, what changed, what's available.
- **User correction layer** — canonical assertions layered over source facts; source preserved.
- **Document ingest** — PDF, image, CCDA/CCD XML, CCDA archives, with local Tesseract OCR; Claude Vision (or other vision model) on consent.
- **Epic SMART-on-FHIR connector** — patient-mediated OAuth, USCDI v3 auto-download (no Epic review required for the patient-app path).
- **Health Auto Export REST push endpoint** — interim path for HealthKit metrics via the third-party Health Auto Export iOS app.
- **Multi-provider LLM** — Anthropic primary, with OpenAI, Gemini, and local-model endpoints supported.
- **Global LLM consent gate + per-source + per-person overrides + `ModelRun` audit trail.**
- **Demo mode** — read-only synthetic sample data; safe for App Store review of HealthKit sync flows.
- **Configuration-as-code** — full parity between `infra/config.yaml` and the settings GUI. Edit in files or UI; both are first-class. Driven by a settings registry.

On the roadmap, **not** in 0.1b:

- **Native OwnChart iOS app** — direct on-device HealthKit sync, replacing the Auto Export bridge for new users.
- **Athena, Cerner/Oracle Health, Kaiser Permanente** patient-mediated connectors. (Kaiser remains export-via-CCD; KP doesn't expose patient FHIR yet.)
- **Nightly cleanup digest** — optional, user-enabled. Bounded suggestions, never silent mutations.
- **DICOM ingestion and radiology study timeline.**
- **Authentik OIDC and caregiver/household roles** — schema is partially in place in v0.1b; full delegation UI is roadmap.
- **Plugin architecture** for community-contributed connectors.
- **Pictal Health integration / export.**

## Stack

| Layer | Choice |
|---|---|
| API | Python 3.12 + FastAPI (async), SQLAlchemy 2 + Alembic, Pydantic v2, uv |
| Workers | Arq (Redis-backed) |
| DB | Postgres 16 + pgvector + pg_trgm |
| Storage | Filesystem bind-mount, SHA-256 content-addressed |
| Frontend | Next.js 15 (App Router) + TypeScript + Tailwind + shadcn/ui |
| LLM | Multi-provider (Anthropic, OpenAI, Google, local) — all gated by the consent layer |
| OCR | Tesseract via OCRmyPDF (local); vision-model OCR (Claude Vision et al.) on consent |
| Auth | Local password (Argon2id) — Authentik OIDC + caregiver delegation planned |
| Reverse proxy | Bring your own (nginx, Caddy, NPM, Traefik) |
| Deploy | Docker Compose |

## Repo layout (when code lands)

```
api/                FastAPI app + Arq workers
  ownchart/
    core/           config, security, consent gate, PHI-safe logger
    models/         SQLAlchemy models
    prompts/        LLM prompts in YAML — never hardcoded
    llm/            multi-provider client + prompt loader + ModelRun audit
    ingest/         per-lane ingestion (pdf, ccda, fhir, auto_export, notes)
    extract/        Tesseract OCR + vision-model OCR (consent-gated)
    canonical/      equivalence + significance ranking
    sensemaking/    Make Sense + Discover job runners + candidate model
    conversations/  saved Ask/Make Sense threads
    routes/         FastAPI routers
    workers/        Arq tasks
    settings/       settings registry — drives GUI + file config parity
  alembic/          migrations
web/                Next.js app
infra/              docker-compose.yml, deploy.sh, .env.example, config.example.yaml
user-docs/          Public setup guides (Epic, Athena, etc.)
scripts/            helper scripts
data/               PHI bind-mount target — gitignored
```

## Quick start

> Code lands when 0.1b ships. The runtime shape will be roughly:
>
> ```sh
> cp infra/.env.example infra/.env
> # fill in at least one LLM provider key, SESSION_SECRET, POSTGRES_PASSWORD
> cp infra/config.example.yaml infra/config.yaml
>
> docker compose -f infra/docker-compose.yml up --build
> # web at http://localhost:8800, api at http://localhost:8801
> ```

## Connecting your records

Most EHR connectors require you to register OwnChart with the vendor as a "patient app" — typically a 30-minute task done once per vendor. Setup guides under [`user-docs/`](./user-docs/):

- [Registering an Epic FHIR app](./user-docs/EPIC_SETUP.md) — works for any health system on Epic (Kaiser, Stanford, Bozeman Health, OrthoVirginia, etc.) once registered.
- [Getting an Athena developer account](./user-docs/ATHENA_SETUP.md) — for athenahealth-based providers.

## License

MIT. See [LICENSE](./LICENSE). The license covers OwnChart's code; the doctrine that comes with it (in [PHILOSOPHY.md](./PHILOSOPHY.md)) is what should travel with any fork that calls itself patient-owned.

## Status

This repo currently contains the **0.1b documentation drop**: the doctrine, the security model, the connector setup guides, and the brand. The source code is coming as 0.1b solidifies.

The point of publishing the design first is to expose four load-bearing decisions to scrutiny before more people depend on them:

- the **consent gate** as the single egress checkpoint for any PHI leaving the host,
- the **user-correction-as-canonical** model that lets the patient override the source record without erasing it,
- the **Evidence Contract** that requires every AI statement to be source-backed, user-canonical, inferred, statistical, or unknown, and
- the **`ModelRun` audit trail** that makes every AI output traceable to its prompt and inputs.

If any of those look wrong to you, open an issue. Better to fix the model before there's code committed to it.

## Acknowledgments

OwnChart's design is rooted in **Critical AI Health Literacy** and the **AI Patients** tradition, particularly the work of [Hugo Campos](https://www.aipatients.org/) and the AI Patients community. The product translation — AI that serves the patient and increases agency rather than dependency — comes from that lens.
