# Philosophy

> The doctrine behind OwnChart. Read this before you fork. The code is the easy part — what this document describes is what makes it OwnChart and not another health-tech startup.

## 1. AI-first is the interaction model. Patient empowerment is the outcome.

OwnChart is an **AI-first, evidence-grounded platform for exploring personal health and life data.** The first thing a user should understand when they open the product is: *I can ask my own health life questions, and the answers are grounded in my own evidence.*

This is not a health-data warehouse with AI features bolted on. It is closer to **Cursor for health and life data** than to **MyChart for patients**. The user brings their data. OwnChart brings structure, memory, retrieval, citations, and AI-assisted reasoning. The user remains the final authority over meaning.

The outcome is **patient parity**: a person, working with AI, reaching the same level of insight into their own record as the people who normally have the tools, training, and institutional access. AI here is not impersonating a clinician and not providing medical advice. AI is helping the patient:

- Ask better questions.
- Understand their own evidence.
- Translate clinical language.
- Explore longitudinal patterns.
- Surface what might matter.
- Challenge institutional blind spots.
- Prepare for conversations with clinicians from a position of agency.
- Build a longitudinal understanding of their body and life.

The institution is never the customer. The patient is the user, the owner, the corrector, and the canonical authority on their own story.

## 2. AI-first does not mean AI-magical

AI-first means inquiry and sensemaking are the primary user experience.

It does **not** mean:

- Unsupported medical advice.
- Silent canonicalization (AI quietly editing your record).
- Opaque summarization (a paragraph with no traceable source).
- Hiding source evidence behind a friendly answer.
- Presenting inference as truth.
- Sending all PHI to external models by default.
- Replacing deterministic statistics or data modeling with "ask the LLM."

Every AI interaction is **evidence-grounded, cited, auditable, scoped, and correctable**. If any of those properties is missing, the feature does not ship.

## 3. The Evidence Contract

Every substantive AI statement in OwnChart is exactly one of:

| Class | Meaning |
|---|---|
| **Source-backed** | Directly stated in a source you control (a PDF, a FHIR resource, a CCDA section). Citable to a page, anchor, or resource ID. |
| **User-canonical** | Confirmed or corrected by you. Overrides the source for display. The source is preserved as evidence, but your version is what shows. |
| **Inferred** | Reasoned but not directly stated. The product marks it as inference and shows the reasoning chain. |
| **Statistical** | Aggregate, trend, or correlation. The underlying method (mean over what window, what comparison group, what confidence interval) is disclosed. |
| **Unknown** | Insufficient evidence. The product says so plainly rather than fabricating coverage. |

Every claim supports the question **"why do you think that?"** — one click expands to the source, the page or section, the extracted excerpt, the confidence level, and any correction you've made.

Confidence labels are **human-readable, not numeric**: Confirmed / High / Medium / Low / Possible / Unknown. Not "0.87." Not "p < 0.05." Plain words you can challenge.

## 4. Raw sources are immutable

Every PDF, FHIR bundle, CCDA XML, page image, and OCR pass is stored content-addressed by SHA-256 and never overwritten. If a future ingest re-fetches the same document, it dedupes by hash. If the parser is buggy and re-extracts a fact wrong tomorrow, the source from which it came is bit-identical to what it was the day it landed.

This sounds obvious. It is not how most healthcare software works. Most systems "normalize" inbound records — meaning they irreversibly transform them on the way in, and the structured version *becomes* the record. The original is discarded or filed off in a way nobody ever reads.

In OwnChart, the original is the record. Everything else is a derivation.

## 5. User correction is canonical

When the source says one thing and the patient says another, the patient is right *for the patient's purposes*. The source is preserved exactly. The patient's assertion is layered on top and becomes what the UI displays. Both are visible. Both are auditable. The correction never erases the source.

Examples this matters for:

- The intake nurse wrote "denies" for a symptom the patient was actively reporting.
- A diagnosis code is on the chart because it unlocks coverage, not because anyone believes it.
- The medication list shows a drug the patient stopped taking six months ago.
- The procedure note records the wrong eye (this happens).

In a traditional system you submit an amendment request, an institution decides whether to honor it, and the original record remains the "truth." Here, you can correct it the moment you see it. The institution's version is preserved as evidence; your version is preserved as canonical.

## 6. Patient memory is evidence

Your own notes, photos, and corrections are not a separate "patient-reported" ghetto attached to the side of the record. They are **first-class evidence**, appearing in timelines and dossiers alongside clinical facts.

Concretely, this includes:

- Free-text notes scoped to a period, episode, or event.
- Photos with EXIF dates (pre/post-surgery, an injury, a recovery milestone).
- Calendar context (when something happened in relation to your life).
- Patient-reported symptoms or events the institution doesn't know about.
- Caregiver annotations on someone else's record (with their consent).
- Conversations you had with OwnChart's AI partner about a topic — those are evidence too.

If the institutional record says "patient denies headaches" and your photo, your note, and your conversation history all say "I had a migraine that week," your record reflects what you experienced.

## 7. AI as a leashed research partner

AI is core infrastructure in OwnChart. It is also leashed.

The leash:

- **Consent gate.** No PHI leaves the host without explicit, scoped opt-in. There is one gate, sitting on the egress path, applied uniformly across all providers. Off by default. (See [SECURITY.md](./SECURITY.md).)
- **Prompts externalized.** Every prompt is a versioned YAML file. `ModelRun.prompt_version` records which file and which SHA produced any given output. No hidden behavior.
- **Audit trail.** Every LLM job creates a `ModelRun` record: provider, model, prompt version, input hashes, output hash, consent mode at call time, privacy mode, token usage, estimated cost, what the user did with the result.
- **Candidates, not commits.** AI produces suggestions — episode groupings, deduplication candidates, label translations, summaries. The user accepts, edits, or rejects. AI never silently mutates the canonical layer.
- **Multi-provider.** OwnChart is not Anthropic-only. Claude, OpenAI, Gemini, and local models (Ollama, llama.cpp endpoints) are all first-class. Local-model paths exist specifically so users who never want PHI to leave the host can still use the sensemaking layer.
- **Safety boundary.** AI never instructs you to start, stop, or change medication. Self-harm intent gets crisis-oriented support and a referral to human help — never instructions for the act.

The research-partner framing matters. A research partner:

- Cites sources.
- Says "I don't know" when it doesn't.
- Suggests questions the user hasn't asked yet.
- Translates jargon without flattening nuance.
- Surfaces inconsistencies — including ones in the institutional record.
- Does not make medical decisions on the user's behalf.

## 8. Conversations are first-class product objects

When you ask OwnChart a question, the conversation is **saved automatically** — with scope (whole record / period / dossier / source), sources referenced, citations, the model and prompt version used, privacy mode, and a timestamp.

You can:

- Search across every conversation you've ever had with your record.
- Resume any thread.
- Pin a useful answer to a dossier.
- Turn an answer into a note.
- Ask follow-up questions scoped to the same evidence.
- See exactly what was sent to the LLM and which provider answered.

Your conversation history becomes part of the user's longitudinal learning — not disposable chat. Over months and years, you build cumulative research across your health life. The Home screen surfaces *Continue researching* as a primary module, on equal footing with timelines and review queues.

## 9. Primary product objects

OwnChart is built around what users actually think about, in priority order:

1. **Questions** — what the user wants to understand. Natural language, with scope.
2. **Conversations** — saved, searchable, resumable Ask + Make Sense threads.
3. **Moments** — important things that happened (a surgery, a diagnosis, a turning point).
4. **Episodes** — system-proposed clusters of related moments over a period.
5. **Patterns** — trends, correlations, gaps, changes, repetitions surfaced by Discover.
6. **Dossiers** — living case files about a topic. Your research workspace per condition or thread.
7. **Sources** — evidence, available when you want to verify (PDFs, FHIR bundles, CCDA, etc.).
8. **Facts** — supporting evidence units. Substrate, not the default view.

**Facts are last on purpose.** The product does not ask the user to become a database administrator before they receive value. Facts are reachable through the "why do you think that?" path on every claim — they are the foundation, not the foreground.

### Episode vs. Dossier

A common confusion worth being explicit about:

- An **Episode** is a *system-proposed cluster of timeline events* (e.g., "a recent surgery + recovery" — the procedure date plus the post-op follow-up window).
- A **Dossier** is a *user-confirmed case file*: the whole longitudinal story of a topic (e.g., "Strabismus" — every event, every conversation, every annotation, from childhood through today).

Episodes live on the timeline. Dossiers live in your research workspace.

## 10. Significance over fact-count

A year with 1,200 extracted facts is not automatically a meaningful year. Most of those facts are templated noise: every encounter re-asserts the same problem list, every medication refill re-emits the same active-meds block, every preventive-care visit fires the same screenings. Source density is a measure of how often the patient touched the system, not of what mattered.

OwnChart ranks by **user-confirmable significance**:

- Was this a turning point in a condition?
- Was this a procedure, hospitalization, new diagnosis, or new medication class?
- Does this fact connect to an unresolved question the user is asking?
- Did the user themselves mark this as significant?

User overrides always win. The model proposes; the user disposes.

## 11. Deduplication preserves provenance without repetition

OwnChart must preserve every source while not making the user look at the same event five times. Dedup has three distinct jobs:

- **Replay dedup** — the same import doesn't create duplicate rows. Keys: file SHA-256, FHIR resource ID, HealthKit sample UUID, import batch ID.
- **Cross-source equivalence** — recognize that *this* event in HealthKit and *that* event in the FHIR feed and *that other* one in a scanned op note are all the same real-world thing.
- **Presentation collapse** — show one card per real-world event, with every source listed underneath. ("Strabismus surgery · 5 supporting claims · 3 sources · 1 conflict.")

You can always expand a collapsed card to see every source separately. No source ever disappears — they just stop crowding the view.

## 12. Source-only context is preserved, not surfaced

Not everything extracted from a source is meaningful to the patient's life graph. Fax numbers. Recipient names. Records-custodian addresses. Signature blocks. Template boilerplate.

These get marked **source-only**: preserved, searchable from the source page, but not promoted into the timeline or the dossiers. "Source-only" is distinct from "rejected." The information is still true and still findable when you ask a question that needs it; it just doesn't clutter the meaningful surfaces.

## 13. Medications are stories, not single facts

A medication is described differently by different sources:

- **Prescribed** (by an EHR, with a date written and a prescriber).
- **Filled** (by a pharmacy, with a date dispensed and a quantity).
- **Scheduled** (in your Apple Health medication schedule).
- **Taken** (by you, when you logged a dose).
- **Skipped** (by you, when you noted a missed dose).
- **Discontinued** (by an EHR or by you).

OwnChart treats medications as **eras and stories**, not as flat single facts. The Review Inbox doesn't show you 800 individual dose-log rows; it shows you "the lisinopril era, 2024–present, with these patterns." Medication review surfaces are designed for stories with bulk actions, not whack-a-mole.

## 14. FHIR-native at the edges, human-native in the core

Standards live at the boundary. OwnChart speaks FHIR R4 at the import surface (so it can pull from Epic, Athena, and every system that supports SMART-on-FHIR), and the model can re-emit standards-compliant documents on the export surface (so the patient is never trapped).

Inside the core model, FHIR's institutional vocabulary stops being load-bearing. The internal representation is built around things humans actually experience:

- **HealthEvent** — something that happened on a timeline.
- **Episode** — a thread of related events the user has confirmed belongs together.
- **Fact** — an extracted, attributable claim with confidence, source, and lineage.
- **UserAssertion** — the patient's canonical version of any fact.
- **SourceDocument** — the immutable thing the fact came from.
- **Conversation** — a saved Ask or Make Sense thread.

This is the layer where lived experience exists: partial evidence, ambiguity, "I'm not sure when this started," "the chart says X but I remember Y." FHIR can't represent any of that and was never trying to. The core can.

## 15. No third-party telemetry

Logs, prompts sent to LLMs, embeddings, queue payloads, job artifacts, error messages — all of it is treated as PHI. None of it leaves the host except through the explicit consent gate.

That means:

- No Sentry. No Datadog. No Mixpanel. No Segment. No Google Analytics. No PostHog.
- No "anonymous usage stats" toggle that's on by default and ships you metrics anyway.
- No crash reporter.
- No vendor analytics in the frontend bundle.
- No "we send hashes, not data" half-measures.

OwnChart cannot tell when you use it, what features you use, or whether it crashed. If you want to know, you read your own logs.

## 16. Provenance is a first-class data type

Every fact in the system has a chain:

```
SourceDocument (immutable, SHA-256)
   → SourceFact (parser/OCR/LLM extracted, with confidence + extractor lineage)
      → UserAssertion (optional correction or confirmation; canonical for display)
         → HealthEvent (timeline-ready)
```

Every link is auditable. You can ask any displayed claim **"why do you think that?"** and the answer is a page in a PDF, a span in an XML, or a FHIR resource, with the extractor version that produced it. There is no UI surface in OwnChart where a fact appears without a traceable source.

For AI outputs the same chain applies, anchored by a `ModelRun` record. Every Ask answer, every Make Sense candidate, every Discover suggestion can be opened up to show: what evidence was retrieved, which provider answered, what prompt version was used, and what consent/privacy mode applied at the time.

## 17. The patient owns the deployment

OwnChart is self-hosted, full stop. There is no hosted version. There is no SaaS tier. There is no "premium cloud sync" coming later. The deployment topology is:

- Your hardware (laptop, NAS, server, home lab — your choice).
- Docker Compose.
- A reverse proxy you control (or none, if you keep it on a tailnet).
- Your encrypted disk.
- Your LLM API key (or local model endpoint), used only when you consent.

This is the entire trust boundary. There is no third party to compromise, no vendor to subpoena, no service to discontinue, no acquisition to render the data inaccessible.

## 18. Configuration is a peer to the GUI

OwnChart is influenced by Home Assistant's design here: **text files and the GUI are peers, not a hierarchy.** Every setting can be edited in `infra/config.yaml` or in the UI; both round-trip. Operators can pin a setting from a file (admin-locked, can't be overridden in the UI). Power users can move from the UI to the file without losing fidelity.

This is enabled by a **settings registry**: every configurable option is declared once, with scope (instance/user/person/source), storage (db/file/env/secret), type, default, sensitivity, restart behavior, and whether admins can lock it. The registry drives the UI, validation, documentation, and the config file's schema.

The config file is not a legacy escape hatch. It's a first-class administration interface.

## 19. The CAIHL lens

OwnChart's design is rooted in **Critical AI Health Literacy** and the **AI Patients** tradition, particularly the work of [Hugo Campos](https://github.com/hugooc) and the [AI Patients](https://www.aipatients.org/) community.

The product translation:

- AI should serve the patient, not the institution.
- AI should increase agency, not dependency.
- AI should make hidden structure visible.
- AI should help the user think, question, and advocate.
- AI should be transparent about uncertainty and provenance.
- AI should not collapse lived experience into institutional categories.

For every feature, the design checklist:

1. Who does this serve?
2. Does this increase patient agency?
3. Does this make hidden structure visible?
4. Does this help the user think more clearly?
5. Does it preserve the user's right to disagree, correct, and contextualize?
6. Does it avoid mistaking institutional data for the whole story?

This is the difference between an AI gimmick and a liberation-oriented patient tool.

## 20. Lineage

OwnChart is not a clean-room invention. The doctrine here carries forward work from two people whose thinking informs the project at its load-bearing points:

- **[Hugo Campos](https://github.com/hugooc)** — Critical AI Health Literacy as a design lens, and the long-running insistence that AI in healthcare can either silence patients or amplify them. OwnChart picks the second.
- **[Josh Mandel](https://github.com/jmandel)** — SMART-on-FHIR as the patient-side data path, plus a pair of open demonstrations that patient-AI-on-real-records is a now-thing, not a future paper: [`health-record-mcp`](https://github.com/jmandel/health-record-mcp) (Model Context Protocol server for SMART-on-FHIR records) and [`health-skillz`](https://github.com/jmandel/health-skillz) (a Claude Skill for analyzing personal health records via SMART on FHIR, at [health-skillz.joshuamandel.com](https://health-skillz.joshuamandel.com)). OwnChart's import surface and evidence-citation model owe a lot to that lineage.

Crediting them up front; the implementation choices and mistakes here are the project maintainer's.

## 21. Doctrine travels with the fork

The MIT license covers the code. This document covers the project. A fork is welcome — please use it for whatever you want. But if you call your fork "patient-owned," these principles are the meaning of the phrase.

Strip the consent gate and you have an EHR scraper. Add telemetry and you have a SaaS. Let the institution override the user's correction and you have what we already had. Replace the Evidence Contract with confident-sounding summaries and you have the failure mode this project exists to refuse.

Patient-owned means: the patient. Owns it.
