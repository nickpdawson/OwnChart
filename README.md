<p align="center">
  <img src="./OwnHealth%20Icon.svg" alt="OwnChart icon — five planets on a horizon line" width="128" height="128" />
</p>

# OwnChart

**Patient-owned longitudinal health intelligence. Self-hosted. Evidence-first. AI as a research partner, not an oracle.**

> **0.1b — documentation drop.** This initial commit publishes the doctrine, security model, and connector setup guides for the upcoming 0.1b public beta. **The source code lands in this repo when 0.1b ships.** Until then, read [PHILOSOPHY.md](./PHILOSOPHY.md), [SECURITY.md](./SECURITY.md), and the [user-docs/](./user-docs/) connector guides — and tell us if anything in the design model needs to change before code touches it.
>
> Working title; name may change.

---

## Why this exists

The American medical record is fragmented by design. Your data sits in a dozen portals, formatted for billing, optimized for the institution, and made deliberately hard to take with you. When you need to make a decision — a second opinion, a surgery, a slow-burning symptom that nobody's connecting the dots on — you are the one piecing it together. From PDFs. From memory.

OwnChart is the system that should already exist for that work:

- One longitudinal record, built from every source you can pull (EHR APIs, CCDAs, faxes, your iPhone, your notes).
- Raw sources preserved exactly as received, forever. Your corrections layered on top — never overwriting the source.
- Local-first storage. PHI never leaves your machine without your explicit, scoped consent.
- AI as a thinking partner: it suggests structure, asks better questions, translates jargon, and **cites everything**. It does not make medical decisions. It does not pretend to be your doctor.
- No vendor telemetry. No SaaS backend. No institutional override.

It's a patient empowerment tool. The patient is the user. The patient owns the record. The patient owns the server.

## Doctrine

The non-negotiables. These are load-bearing — read them as constraints on every feature decision, not aspirations.

1. **Raw sources are immutable.** Original PDFs, FHIR bundles, CCDA XML are stored content-addressed (SHA-256) and never modified. Every extracted claim cites its source page.
2. **User correction is canonical.** When you correct a fact, your version becomes the displayed truth. The original source record is untouched. You can always see both.
3. **AI is gated behind explicit global PHI consent.** Before any byte of your health data leaves the host to an LLM, you must opt in. There is one consent gate, and it sits on the egress path. (See [SECURITY.md](./SECURITY.md).)
4. **No third-party telemetry.** Logs, prompts, embeddings, queue payloads are all treated as PHI. Nothing phones home. No Sentry, no Mixpanel, no crash reporter.
5. **FHIR-native at the edges, human-native in the core.** Standards-compliant at the import/export boundary. Internally, the model has room for ambiguity, user correction, partial evidence, and the parts of lived experience that don't have an ICD-10 code.
6. **Significance over fact-count.** A dense year of 1,200 extracted facts is not automatically a meaningful year. The product ranks by user-confirmable significance, not source density.
7. **Provenance is auditable for every AI output.** Every LLM job creates a `ModelRun` record: model, prompt version, inputs sent, outputs received, consent mode, user action. You can always answer "why did OwnChart say this?"

See [PHILOSOPHY.md](./PHILOSOPHY.md) for the longer treatment.

## AI as a first-class citizen — with a leash

OwnChart treats AI as core infrastructure, not a sidecar. It runs against the patient's record in well-defined jobs:

| Job | Trigger | Examples |
|---|---|---|
| **Make sense of a source** | User-initiated | "Summarize what this 47-page hospital discharge tells me", "Suggest an episode for these three records" |
| **Translate a label** | User-initiated or background candidate | `"PLMT ADJUSTABLE SUTR STRABISMUS"` → `"Adjustable-suture strabismus surgery"` (original preserved) |
| **Review queue compression** | User-initiated | "Group these 800 duplicate facts into review tasks" |
| **Retrieval & answering** | User asks a question | "Tell me the story of my strabismus" — returns answer with citations into source pages |
| **OCR escalation** | Per-source consent on import | Local Tesseract first; Claude Vision only on hard documents and only if LLM consent is on |

Two architectural commitments back this up:

- **Prompts are externalized.** Every prompt lives in `api/ownchart/prompts/*.yaml`, version-controlled. No hardcoded strings. `ModelRun.prompt_version` cites the file and SHA.
- **AI never mutates canonical data directly.** It produces *candidates* (suggested labels, suggested episode groupings, suggested duplicates). The user accepts, edits, or rejects. The accepted version becomes the user's canonical assertion.

If you turn LLM consent off, OwnChart still works — ingest, review, search, timeline, manual correction all run locally. You lose the sensemaking layer and Vision OCR fallback. That's the trade.

## Security model

The headline: **PHI lives on your disk and stays there unless you explicitly send it somewhere else.**

- **Self-hosted only.** Docker Compose on your hardware. No SaaS backend exists.
- **Bind-mount storage** for raw sources; Postgres 16 + pgvector for the structured layer. Disk-level encryption (LUKS / FileVault) is the deployer's responsibility — OwnChart assumes it.
- **Consent gate as the egress checkpoint.** Every LLM call passes through a single function that checks the global consent flag, the per-source override, and the privacy mode (off / metadata / selected evidence / full source). If consent is off, the call is refused before any payload is assembled.
- **No telemetry. No analytics. No crash reporter.** Errors stay on the host.
- **Secrets in env vars only.** Never in YAML. Never in git. `infra/.env` is gitignored; `infra/.env.example` is the template.
- **HAR redaction.** If you capture a browser session for connector development, OwnChart can analyze it locally — but tokens and cookies are stripped before display, and HAR files themselves are gitignored aggressively (`*.har`, `*.har.gz`).
- **Argon2id for local passwords** in v0.1b. Authentik OIDC is the next step for households / caregivers.

Full threat model and operator checklist: [SECURITY.md](./SECURITY.md).

## What's in 0.1b

Shipping in this release:

- Document ingest (PDF, image, CCDA XML, CCDA archive) with local OCR.
- Epic SMART-on-FHIR connector (patient-mediated OAuth, auto-download via USCDI v3).
- Global timeline across every imported source.
- Review inbox with lane split, bulk triage, source-level summaries.
- Evidence Vault — every claim links back to its source page.
- Natural-language Ask with citations.
- User correction layer (canonical assertions over source facts).
- Global LLM consent gate + per-source override + `ModelRun` audit trail.
- Health Auto Export REST push endpoint (HealthKit metrics via the third-party Health Auto Export iOS app).
- Demo mode with synthetic sample data.
- Configuration-as-code (`infra/config.yaml`) with parity to a settings GUI.

On the roadmap, **not** in 0.1b:

- Native OwnChart iOS app (HealthKit sync, planned next release).
- Athena, Cerner/Oracle Health, Kaiser Permanente patient-mediated connectors.
- DICOM ingestion and radiology study timeline.
- Nightly automated sensemaking pass.
- Pictal Health integration / export.
- Authentik OIDC, household/caregiver roles.
- Plugin architecture for community-contributed connectors.

## Stack

| Layer | Choice |
|---|---|
| API | Python 3.12 + FastAPI (async), SQLAlchemy 2 + Alembic, Pydantic v2, uv |
| Workers | Arq (Redis-backed) |
| DB | Postgres 16 + pgvector + pg_trgm |
| Storage | Filesystem bind-mount, SHA-256 content-addressed |
| Frontend | Next.js 15 (App Router) + TypeScript + Tailwind + shadcn/ui |
| LLM | Anthropic SDK only, gated behind global consent |
| OCR | Tesseract via OCRmyPDF (local); Claude Vision only with consent |
| Auth | Local password (Argon2id) — Authentik OIDC planned |
| Reverse proxy | Bring your own (nginx, Caddy, NPM, Traefik) |
| Deploy | Docker Compose |

## Repo layout (when code lands)

```
api/                FastAPI app + Arq workers
  ownchart/
    core/           config, security, consent gate, PHI-safe logger
    models/         SQLAlchemy models
    prompts/        LLM prompts in YAML — never hardcoded
    llm/            prompt loader + Anthropic client wrapper
    ingest/         per-lane ingestion (pdf, ccda, fhir, auto_export, notes)
    extract/        Tesseract OCR + Claude Vision (consent-gated)
    canonical/      equivalence + significance ranking
    routes/         FastAPI routers
    workers/        Arq tasks
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
> # fill in ANTHROPIC_API_KEY, SESSION_SECRET, POSTGRES_PASSWORD
> cp infra/config.example.yaml infra/config.yaml
>
> docker compose -f infra/docker-compose.yml up --build
> # web at http://localhost:8800, api at http://localhost:8801
> ```

## Connecting your records

Most EHR connectors require you to register an app with the vendor as a "patient app" — typically a 30-minute task done once. Setup guides under [`user-docs/`](./user-docs/):

- [Registering an Epic FHIR app](./user-docs/EPIC_SETUP.md) — works for any health system on Epic (Kaiser, Stanford, Bozeman Health, OrthoVirginia, etc.).
- [Getting an Athena developer account](./user-docs/ATHENA_SETUP.md) — for athenahealth-based providers.

## License

MIT. See [LICENSE](./LICENSE). The license covers OwnChart's code; the doctrine that comes with it (in [PHILOSOPHY.md](./PHILOSOPHY.md)) is what should travel with any fork that calls itself patient-owned.

## Status

This repo currently contains the **0.1b documentation drop**: the doctrine, the security model, the connector setup guides, and the brand. The source code is coming as 0.1b solidifies.

The point of publishing the design first is to expose three load-bearing decisions to scrutiny before more people depend on them:

- the **consent gate** as the single egress checkpoint for any PHI leaving the host,
- the **user-correction-as-canonical** model that lets the patient override the source record without erasing it, and
- the **`ModelRun` audit trail** that makes every AI output traceable to its prompt and inputs.

If any of those look wrong to you, open an issue. Better to fix the model before there's code committed to it.
