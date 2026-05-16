# Demo data

This directory is the drop point for synthetic FHIR sample data that
the demo instance ingests at startup.

The demo seed (`api/ownchart/core/demo_data_seed.py`) looks for:

```
infra/demo_data/sample_patient.json
```

(override with `OWNCHART_DEMO_BUNDLE_PATH`)

## What ships in this repo

`sample_patient.json` — **Avery T. Walker** (DOB 1972-04-18, female,
synthetic). A hand-curated 68-resource FHIR R4 bundle covering a
five-year primary-care story:

- **12 encounters** at Memorial Family Medicine, 2019 → 2026 (annual
  physicals + hypertension follow-ups).
- **2 conditions**: Vitamin D deficiency (2023-02-08), Essential
  hypertension (2024-05-12).
- **2 prescriptions**: Lisinopril 10 mg started 2024-06-03 after the
  HTN diagnosis; Cholecalciferol 2000 IU started 2023-02-08.
- **11 immunizations**: 3 COVID-19 mRNA doses (March / April /
  November 2021), 7 annual flu shots (2019-2025), 1 Tdap (2023).
- **12 BP readings**: pre-Rx baseline 118/76 → creep to 150/94 →
  post-Rx normalization to 124/78.
- **9 HR readings** + **14 nights of sleep duration** (May 2026).
- **2 DiagnosticReports**: 2024 lipid panel, 2023 vitamin D level.

The bundle supports the canonical demo questions:
- "When did I get my COVID vaccine?"
- "What changed around starting lisinopril?"
- "How has my sleep looked recently?"
- "What does my record say about blood pressure?"

Avery Walker is not a real person. The address (100 Demo Lane,
Demoville, CA 94000), phone (555-0100), and email (demo@ownchart.me)
are deliberately non-routable placeholders.

## Substituting your own bundle

You can replace `sample_patient.json` with any R4-compliant synthetic
dataset:

- **Synthea** (https://github.com/synthetichealth/synthea, Apache-2.0)
  generates rich bundles — hundreds of resources per patient — but
  they don't always include the specific facts a focused demo needs:
  ```
  ./run_synthea -p 1 --exporter.fhir.export true
  cp output/fhir/<one_patient>.json infra/demo_data/sample_patient.json
  ```
- Public **Epic FHIR sandbox** patient bundles work for testing live
  SMART-on-FHIR ingestion against `fhir.epic.com`.

Whatever you ship, it MUST contain no real PHI.

## What gets ingested

A single `SourceDocument` (source_type=`fhir_bundle`) per bundle.
The bundle bytes are stored under `data/blobs/`; counts/metadata
live in the row's `raw_metadata` JSON. Resource-level fact
extraction happens through the same FHIR ingest path used by live
SMART-on-FHIR connections.

The demo seed is idempotent — it only runs when:
  1. `OWNCHART_DEMO_MODE=true`
  2. The demo user (`demo@ownchart.me`) has zero existing sources
  3. A bundle file is present

So if you rebuild the demo DB you'll get a fresh ingest; if you
just restart the container with existing data, nothing changes.
