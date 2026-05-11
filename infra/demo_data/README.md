# Demo data

This directory is the drop point for synthetic FHIR sample data that
the demo instance ingests at startup.

The demo seed (`api/ownchart/core/demo_data_seed.py`) looks for:

```
infra/demo_data/sample_patient.json
```

(override with `OWNCHART_DEMO_BUNDLE_PATH`)

## How to generate one

The simplest source of synthetic FHIR R4 bundles is **Synthea**:

```bash
git clone https://github.com/synthetichealth/synthea
cd synthea
./run_synthea -p 1 --exporter.fhir.export true
cp output/fhir/<one_patient>.json /path/to/ownchart/infra/demo_data/sample_patient.json
```

Synthea-generated bundles are Apache-2.0 licensed and contain no
real PHI — they're suitable for a public demo.

You can also use the public Epic FHIR sandbox patient bundles or
any other R4-compliant synthetic dataset; the ingest pipeline
treats them the same way.

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
