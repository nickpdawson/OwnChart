# Philosophy

> The doctrine behind OwnChart. Read this before you fork. The code is the easy part — what this document describes is what makes it OwnChart and not another health-tech startup.

## 1. Patient empowerment is the primary outcome

The American medical record is owned by institutions, formatted for billing, and made structurally hostile to the patient who is supposed to be its subject. Twenty-one Cures Act has cracked the door — patients can now pull their own data through SMART-on-FHIR — but having the data and being able to *use* it are different problems. The portals export PDFs. The PDFs are unsearchable. The "summary" is templated marketing. The continuity of care document was designed for a referring clinician, not for you.

OwnChart's job is to close that gap. The patient is the user. The patient owns the record. The patient owns the server. The patient owns the corrections, the questions, the canonical version of their own story.

What this implies in practice:

- The institution is never the customer.
- The patient's correction outweighs the source record in every display surface.
- Features that exist to make the institution comfortable (lock-down workflows, "verified clinician only" gates, opaque AI recommendations) do not belong here.
- Features that exist to make the patient *more capable* (translation of jargon, retrieval with citations, gap-finding across records) are the product.

## 2. Raw sources are immutable

Every PDF, FHIR bundle, CCDA XML, page image, and OCR pass is stored content-addressed by SHA-256 and never overwritten. If a future ingest re-fetches the same document, it dedupes by hash. If the parser is buggy and re-extracts a fact wrong tomorrow, the source from which it came is bit-identical to what it was the day it landed.

This sounds obvious. It is not how most healthcare software works. Most systems "normalize" inbound records — meaning they irreversibly transform them on the way in, and the structured version *becomes* the record. The original is discarded or filed off in a way nobody ever reads.

In OwnChart, the original is the record. Everything else is a derivation.

## 3. User correction is canonical

When the source says one thing and the patient says another, the patient is right *for the patient's purposes*. The source is preserved exactly. The patient's assertion is layered on top and becomes what the UI displays. Both are visible. Both are auditable. The correction never erases the source.

Examples this matters for:

- The intake nurse wrote "denies" for a symptom the patient was actively reporting.
- A diagnosis code is on the chart because it unlocks coverage, not because anyone believes it.
- The medication list shows a drug the patient stopped taking six months ago.
- The procedure note records the wrong eye (this happens).

In a traditional system you submit an amendment request, an institution decides whether to honor it, and the original record remains the "truth." Here, you can correct it the moment you see it. The institution's version is preserved as evidence; your version is preserved as canonical.

## 4. AI is a research partner, not an oracle

AI is core infrastructure in OwnChart. It is also leashed.

The leash:

- **Consent gate.** No PHI leaves the host without explicit, scoped opt-in. There is one gate, sitting on the egress path. Off by default. (See [SECURITY.md](./SECURITY.md).)
- **Prompts externalized.** Every prompt is a versioned YAML file. `ModelRun.prompt_version` records which file and which SHA produced any given output. No hidden behavior.
- **Audit trail.** Every LLM job creates a `ModelRun` record: model, prompt version, input hashes, output hash, consent mode at call time, token usage, what the user did with the result. "Why did OwnChart say this?" always has an answer.
- **Candidates, not commits.** AI produces suggestions — episode groupings, deduplication candidates, label translations, summaries. The user accepts, edits, or rejects. AI never silently mutates the canonical layer.

The research-partner framing matters. A research partner:

- Cites sources.
- Says "I don't know" when it doesn't.
- Suggests questions the user hasn't asked yet.
- Translates jargon without flattening nuance.
- Surfaces inconsistencies — including ones in the institutional record.
- Does not make medical decisions on the user's behalf.

This framing is informed by the [Critical AI Health Literacy](https://www.aipatients.org/) lens (Hugo Campos and others): AI in healthcare can either silence the patient (institutional decision-support that hides its reasoning) or amplify them (a partner that increases agency and accumulated literacy through reflection). OwnChart is built to do the second.

## 5. Significance over fact-count

A year with 1,200 extracted facts is not automatically a meaningful year. Most of those facts are templated noise: every encounter re-asserts the same problem list, every medication refill re-emits the same active-meds block, every preventive-care visit fires the same screenings. Source density is a measure of how often the patient touched the system, not of what mattered.

OwnChart ranks by **user-confirmable significance**:

- Was this a turning point in a condition?
- Was this a procedure, hospitalization, new diagnosis, or new medication class?
- Does this fact connect to an unresolved question the user is asking?
- Did the user themselves mark this as significant?

User overrides always win. The model proposes; the user disposes.

## 6. FHIR-native at the edges, human-native in the core

Standards live at the boundary. OwnChart speaks FHIR R4 at the import surface (so it can pull from Epic, Athena, and every system that supports SMART-on-FHIR), and the model can re-emit standards-compliant documents on the export surface (so the patient is never trapped).

Inside the core model, FHIR's institutional vocabulary stops being load-bearing. The internal representation is built around things humans actually experience:

- **HealthEvent** — something that happened on a timeline.
- **Episode** — a thread of related events the user has confirmed belongs together (e.g., "my 2008 strabismus surgery and its three follow-ups").
- **Fact** — an extracted, attributable claim with confidence, source, and lineage.
- **UserAssertion** — the patient's canonical version of any fact.
- **SourceDocument** — the immutable thing the fact came from.

This is the layer where lived experience exists: partial evidence, ambiguity, "I'm not sure when this started," "the chart says X but I remember Y." FHIR can't represent any of that and was never trying to. The core can.

## 7. No third-party telemetry

Logs, prompts sent to LLMs, embeddings, queue payloads, job artifacts, error messages — all of it is treated as PHI. None of it leaves the host except through the explicit consent gate.

That means:

- No Sentry. No Datadog. No Mixpanel. No Segment. No Google Analytics. No PostHog.
- No "anonymous usage stats" toggle that's on by default and ships you metrics anyway.
- No crash reporter.
- No vendor analytics in the frontend bundle.
- No "we send hashes, not data" half-measures.

OwnChart cannot tell when you use it, what features you use, or whether it crashed. If you want to know, you read your own logs.

## 8. Provenance is a first-class data type

Every fact in the system has a chain:

```
SourceDocument (immutable, SHA-256)
   → SourceFact (parser/OCR/LLM extracted, with confidence + extractor lineage)
      → UserAssertion (optional correction or confirmation; canonical for display)
         → HealthEvent (timeline-ready)
```

Every link in the chain is auditable. You can ask any displayed claim: "where did this come from?" and the answer is a page in a PDF or a span in an XML, with the extractor version that produced it. There is no UI surface in OwnChart where a fact appears without a traceable source.

## 9. The patient owns the deployment

OwnChart is self-hosted, full stop. There is no hosted version. There is no SaaS tier. There is no "premium cloud sync" coming later. The deployment topology is:

- Your hardware (laptop, NAS, server, home lab — your choice).
- Docker Compose.
- A reverse proxy you control (or none, if you keep it on a tailnet).
- Your encrypted disk.
- Your LLM API key, billed to you, used only when you consent.

This is the entire trust boundary. There is no third party to compromise, no vendor to subpoena, no service to discontinue, no acquisition to render the data inaccessible.

## 10. Doctrine travels with the fork

The MIT license covers the code. This document covers the project. A fork is welcome — please use it for whatever you want. But if you call your fork "patient-owned," these principles are the meaning of the phrase. Strip the consent gate and you have an EHR scraper. Add telemetry and you have a SaaS. Let the institution override the user's correction and you have what we already had.

Patient-owned means: the patient. Owns it.
