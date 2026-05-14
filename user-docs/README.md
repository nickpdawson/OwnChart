# OwnChart user setup guides

Public-facing setup documentation. The guides here walk an operator (you) through getting an OwnChart instance connected to the EHRs the patient actually uses.

## Start here

**[CONNECTORS.md](./CONNECTORS.md)** — the universal pattern. Every vendor's developer portal is shaped differently, but the core flow (developer account → patient-app registration → sandbox testing → wire `client_id` into OwnChart → per-customer rollout) is the same. Read this first; it makes the vendor-specific guides short.

## Vendor-specific guides

Each OwnChart deployment registers its own app with each EHR vendor whose data it ingests. There is no central OwnChart-the-company app registration — that would break the patient-owned, self-hosted model. Cost: $0 per vendor. Time: ~30 minutes of paperwork + vendor-specific review.

| EHR | Audience focus | Guide | Review path |
|---|---|---|---|
| **Epic** | Largest US hospitals, academic medical centers (Cleveland Clinic, Stanford, Mass General Brigham, Mayo, Kaiser, ....) | [EPIC_SETUP.md](./EPIC_SETUP.md) | USCDI v3 auto-download — no human review, ≤12 hours per Epic customer |
| **athenahealth** | Mid-market ambulatory practices | [ATHENA_SETUP.md](./ATHENA_SETUP.md) | Human review, 1–4 weeks |
| **ModMed** | Specialty practices (dermatology, ophthalmology, orthopedics, GI, plastic surgery, pain, OB-GYN) | [MODMED_SETUP.md](./MODMED_SETUP.md) | Contact-driven |
| **NextGen** | Mid-sized ambulatory practices, FQHCs, community health centers, behavioral health | [NEXTGEN_SETUP.md](./NEXTGEN_SETUP.md) | Self-service portal |
| **Oracle Health (Cerner)** | Health systems on Oracle Health Millennium (former Cerner customers): VA, DoD, Intermountain, Banner, Atrium, etc. | [CERNER_SETUP.md](./CERNER_SETUP.md) | Self-service sandbox + per-site production rollout |

## Vendors not yet covered

Open PR-worthy gaps:

- **Allscripts / Veradigm** — small to mid-market practices; patient-facing FHIR via the Veradigm Developer Program.
- **eClinicalWorks (eCW)** — ambulatory; patient FHIR via their developer portal.
- **MEDITECH** — community hospitals; FHIR via MEDITECH Greenfield / Expanse APIs.
- **Kaiser Permanente** — closed Epic deployment without a public patient-FHIR endpoint; the patient-mediated path is currently CCD export, not FHIR.
- **MyHealthONE / HCA Healthcare** — large hospital chain; FHIR exposure varies by site.

If you've completed registration with a vendor not yet covered, please open a PR with a new guide using the shape of the others — your concrete walkthrough is worth more than any vendor's PDF.

## What you'll need before starting (any vendor)

- An OwnChart instance running somewhere with a **publicly reachable HTTPS URL for the OAuth callback** at `https://your-instance.example.com/api/connectors/callback`. The rest of OwnChart can stay private — only the callback needs to terminate publicly. Cloudflare Tunnel, Tailscale Funnel, ngrok, or a real reverse proxy on a real domain all work.
- A stable email address for the vendor developer portal — ideally not your personal medical-record email; use a dedicated one if you want a clean separation.
- A privacy-policy URL you control. The OwnChart public site has a model one at <https://www.ownchart.me/privacy> you can adapt; serve yours at `https://your-instance.example.com/privacy`.
- A terms-of-service URL you control (same logic).
- ~30 minutes per vendor for the application form. Patience for review where applicable.

## What you will *not* need

- Any kind of fee. All patient-app registrations covered here are free.
- A business entity. You can register as an individual.
- A clinician sponsor. These are patient-facing app registrations.
- A HIPAA Business Associate Agreement. You are not a covered entity acting on behalf of one — you are the patient (or the operator running OwnChart on behalf of the patient).
