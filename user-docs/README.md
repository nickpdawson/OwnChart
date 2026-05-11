# OwnChart user setup guides

Public-facing setup documentation. The guides here walk a patient through getting their OwnChart instance connected to the EHRs they actually use.

## Connector setup

Most EHR vendors require you to register OwnChart with them as a "patient app." This is typically a 30-minute, one-time task per vendor — not per provider. (One Epic app registration covers every Epic-based health system: Kaiser, Stanford, Bozeman Health, OrthoVirginia, your local hospital, etc.)

| Guide | Covers |
|---|---|
| [EPIC_SETUP.md](./EPIC_SETUP.md) | Registering an Epic FHIR app at `fhir.epic.com` ("Orchid"). Works for **every** Epic-based health system once registered. |
| [ATHENA_SETUP.md](./ATHENA_SETUP.md) | Getting an athenahealth developer account and registering a patient app. |

More guides (Cerner / Oracle Health, Kaiser Permanente patient-mediated, Allscripts/Veradigm) will land in subsequent releases. The Epic registration pattern generalizes — if you need to wire a new SMART-on-FHIR vendor before there's a dedicated guide, follow the Epic flow and open an issue with the gaps.

## What you'll need before starting

- An OwnChart instance running somewhere with a publicly reachable HTTPS URL **for the OAuth callback**. (You don't need to expose the rest of OwnChart to the internet — just the callback. Cloudflare Tunnel, Tailscale Funnel, ngrok, or a reverse proxy on a real domain all work.)
- An email account for the vendor developer portal — ideally not your personal medical-record email; use a dedicated one if you want a clean separation.
- ~30 minutes per vendor.

## What you will *not* need

- Any kind of fee. All patient-app registrations covered here are free.
- A business entity. You can register as an individual.
- A clinician sponsor. These are patient-facing app registrations.
- A HIPAA Business Associate Agreement. You are not a covered entity acting on behalf of one; you are the patient.
