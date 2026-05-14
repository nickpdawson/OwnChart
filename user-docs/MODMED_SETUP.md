# Registering for ModMed FHIR access

> ModMed (Modernizing Medicine) is the EHR for many specialty practices — dermatology, ophthalmology, orthopedics, gastroenterology, plastic surgery, pain management, and OB-GYN. If your specialty care happens at a private specialty practice, the chances it runs on ModMed (`EMA®` or `gGastro`) are higher than the chances it runs on Epic.

> **Read [CONNECTORS.md](./CONNECTORS.md) first** if you haven't — it explains the universal pattern these vendor-specific guides assume.

> **Status note:** ModMed's developer portal is **contact-driven**, not self-service in the way Epic or NextGen are. You can read about the API publicly, but actual sandbox + production access is gated through ModMed's developer-experience team. Expect to email and have a short conversation about what you're building before getting credentials. This guide reflects what is published at `portal.api.modmed.com` and `modmed.com/api-platform/` as of 0.1b; if details have shifted since, please open a PR.

Expected time: ~30 minutes to email and describe your app; **days to a few weeks** to receive sandbox credentials depending on ModMed's pipeline.

## Step 1 — Read the public API docs

1. Open <https://portal.api.modmed.com>. This is ModMed's developer documentation portal.
2. Identify which API surface you need. ModMed publishes two FHIR APIs:

   | API | Standard | What it does | OwnChart wants |
   |---|---|---|---|
   | **EMA Proprietary API** | FHIR R4 (custom) | Tailored FHIR R4 implementation for EMA® and ModMed Practice Management; supports CREATE/READ/SEARCH/UPDATE | Possibly — broader operations than needed |
   | **EMA and gGastro Certified FHIR API** | HL7 FHIR R4 (USCDI v1+) | 21st Century Cures-certified; SMART on FHIR; bulk export (NDJSON) | **Yes** — this is the patient-app path |

The **Certified FHIR API** is the one that maps to the patient-app model — it's the 21st Century Cures-compliant surface that practices are required to expose. The Proprietary API is more flexible but typically gated for tighter partnerships.

## Step 2 — Request developer access

Unlike Epic's self-service sign-up, ModMed's developer access is **request-driven**. From the portal:

1. Look for a **Contact** / **Request Access** / **Become a Developer** action. The page may direct you to email a developer-experience address (currently typically `apidevelopers@modmed.com` — check the portal for the current contact).
2. Draft a short message describing:
   - Who you are (individual developer, patient-app author).
   - What you're building (OwnChart: a self-hosted, patient-owned health record).
   - Which API you want (Certified FHIR R4 for SMART on FHIR patient apps).
   - That you're complying with 21st Century Cures Act / ONC information-blocking rules as a patient-mediated app.

   A short, accurate paragraph is better than a long pitch. ModMed's team is gating for legitimacy, not enthusiasm.

3. ModMed will respond with next steps — usually credentials for the sandbox environment, an NDA or terms acknowledgment, and the production-onboarding process.

## Step 3 — App registration metadata

When ModMed asks for registration details, provide the standard SMART-on-FHIR app metadata (same as every other vendor):

| Field | Value |
|---|---|
| App name | `OwnChart` (or your fork's name) |
| Audience | Patient-facing |
| Redirect URI | `https://your-instance.example.com/api/connectors/callback` |
| Client type | Public client with PKCE if offered; otherwise confidential |
| SMART on FHIR version | R4 |
| Scopes | `openid fhirUser launch/patient patient/*.read` |
| Description | "Self-hosted platform for patients to maintain a canonical health record for their own personal use." |
| Privacy URL | Yours |

If ModMed issues a `client_secret` (confidential client), treat it as a real secret. Don't commit it.

## Step 4 — Sandbox testing

ModMed provides a sandbox environment with synthetic patient data. The sandbox FHIR base URL pattern is account-specific; ModMed will give you the exact URL with your credentials.

Validation:
- OAuth round-trip succeeds, returning a code at your redirect URI.
- Token exchange returns access + (where supported) refresh tokens.
- `Patient/{id}` returns the synthetic patient.
- USCDI v3 resource categories (Condition, MedicationStatement, AllergyIntolerance, Observation, etc.) all return non-empty bundles where the synthetic patient has data.

## Step 5 — Wire OwnChart

Add to `infra/.env`:

```sh
OWNCHART_MODMED_CLIENT_ID=<production client id>
OWNCHART_MODMED_CLIENT_ID_SANDBOX=<sandbox client id>
# Only if confidential:
OWNCHART_MODMED_CLIENT_SECRET=<production client secret>
```

Add to `infra/connectors.seed.yaml`:

```yaml
- slug: modmed
  name: ModMed
  ehr_vendor: modmed
  fhir_base: <production FHIR R4 base URL ModMed gave you>
  fhir_base_sandbox: <sandbox FHIR R4 base URL ModMed gave you>
  client_id_env: OWNCHART_MODMED_CLIENT_ID
  client_id_env_sandbox: OWNCHART_MODMED_CLIENT_ID_SANDBOX
  # Only if confidential:
  client_secret_env: OWNCHART_MODMED_CLIENT_SECRET
  scopes: openid fhirUser launch/patient patient/*.read
```

Forward env vars in `infra/docker-compose.yml`; restart the API container. A **Connect ModMed** button will appear at `/connectors`.

## Step 6 — Production access per practice

Once your app is approved for production, individual ModMed practices need to enable patient FHIR access for your `client_id`. This is **per-practice** rather than auto-distributed (unlike Epic). The practice's ModMed administrator (or office manager) handles this — patients sometimes need to ask explicitly:

> "I have a patient app called OwnChart that I'd like to connect to your patient portal. Can your office enable the SMART on FHIR Patient Access API for this app? The vendor is ModMed."

A practice that has never enabled patient FHIR access before may need ModMed support's help. Be patient.

## Specialty notes

Because ModMed is specialty-focused, the data you'll get is **specialty-scoped**. From a ModMed dermatology practice, expect:

- Conditions (rashes, lesion diagnoses)
- Procedures (biopsies, excisions, laser treatments)
- Medications (topical and systemic)
- Visit notes
- Pathology results (if integrated)

You will *not* typically get a full longitudinal cardiology / endocrinology / lab record — that lives on whatever EHR your primary care or hospital uses. The point of OwnChart is to pull from multiple sources and stitch them; ModMed's contribution is the specialty slice.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Application not found" at OAuth start | Sandbox `client_id` against production FHIR base (or vice versa) | Match `client_id` to the matching FHIR base |
| 401 on token exchange | Missing `client_secret` for a confidential client, or PKCE not used on a public client | Re-confirm client type with ModMed |
| Empty bundles from a known-good practice | Practice hasn't enabled patient FHIR for your app | Practice admin enables; this often requires ModMed support involvement |
| ModMed support is slow to respond | Their developer-experience team is small and request-driven | Be specific about what you're building; mention 21st Century Cures compliance |

## Open items / known unknowns

- The exact URL of ModMed's developer-contact form changes; check `portal.api.modmed.com` for current routing.
- Whether ModMed currently issues `client_secret` for all patient apps, or accepts PKCE-only public clients, is determined per-application.
- ModMed has historically been more restrictive than the largest vendors about who gets API access. A patient-app pitch tied to ONC compliance is your strongest framing.
- `offline_access` / refresh-token support may vary per practice enrollment.

If you've completed a ModMed registration recently, please open a PR with specifics — the public portal is sparse and concrete walkthroughs are valuable.
