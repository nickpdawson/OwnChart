# Registering for NextGen Healthcare FHIR access

> NextGen Healthcare is the EHR used by many ambulatory practices, community health centers, FQHCs, and behavioral health providers — typically mid-sized practices that are not on Epic but are larger than a solo clinic. If you've ever been to a community health clinic, an OB-GYN group, or a multi-specialty mid-size practice, the chance it runs on NextGen Office or NextGen Enterprise is nontrivial.

> **Read [CONNECTORS.md](./CONNECTORS.md) first** if you haven't — it explains the universal pattern these vendor-specific guides assume.

Expected time: ~30 minutes to register the app in the developer portal; portal-side approval typically lands in **hours to a few days**.

## NextGen has two product lines

NextGen sells two distinct EHR products, and the FHIR developer flow is slightly different for each:

| Product | Who uses it | Patient FHIR access |
|---|---|---|
| **NextGen Office** | Small to mid-sized ambulatory practices (cloud-based) | Patient Access APIs (FHIR R3 + R4) + SMART App Launch FHIR API |
| **NextGen Enterprise** | Larger ambulatory practices, FQHCs, behavioral health (server-based) | Patient Access APIs (FHIR DSTU2 + R4) |

For a patient app, **register against both FHIR DSTU2 and FHIR R4** when the registration form asks — that ensures your app works with the broadest set of NextGen-using practices. OwnChart's import pipeline is R4-native but DSTU2 fallback is supported by the SMART-on-FHIR spec.

## Step 1 — Create a NextGen API developer account

1. Go to <https://www.nextgen.com/api>.
2. Click through to the **Developer Portal** (the exact label changes; look for "Developers," "API Developer Portal," or "Get Started").
3. Sign up with a real email. NextGen verifies email and may follow up for identity confirmation, but a personal/individual developer account is acceptable for patient apps.

## Step 2 — Read the patient-app developer guides

NextGen publishes detailed FHIR developer guides — read these *before* registering, not after. They tell you which OAuth scopes the platform supports, which resources are exposed via patient/*.read, and where the sandbox is.

Key guides:
- **NextGen Office Patient Access APIs Developer Guide** (FHIR R3/R4) — for patient apps targeting NextGen Office practices.
- **NextGen Office SMART App Launch FHIR API Developer Guide** — for the SMART launch flow; this is the 21st Century Cures-compliant pathway.

The latest versions live under <https://www.nextgen.com/api/-/media/files/ngo/> (filenames change as NextGen updates them).

## Step 3 — Register the app in the developer portal

The NextGen API Developer Portal has a self-service app registration flow. Required fields:

| Field | Value |
|---|---|
| App name | `OwnChart` (or your fork's name) |
| Audience | Patient Access |
| OAuth Callback URL(s) | `https://your-instance.example.com/api/connectors/callback` |
| FHIR version(s) | **Check both DSTU2 and R4** (broadens reachable practices) |
| Client type | PKCE-only public client (preferred) or confidential |
| SMART on FHIR version | Per the SMART App Launch guide |
| Scopes | `openid fhirUser launch/patient patient/*.read` (DSTU2 uses `patient/*.read` similarly) |
| Description | "Self-hosted platform for patients to maintain a canonical health record for their own personal use." |
| Privacy URL | Yours |
| Terms URL | Yours |

**Important:** NextGen distinguishes between Patient Access APIs and the SMART App Launch FHIR API. They're related but not identical. The SMART App Launch path is the modern patient-mediated OAuth flow OwnChart needs. If you're given a choice, register for both — they share the same client_id but use slightly different endpoints.

## Step 4 — Get your sandbox credentials

After registration, the developer portal shows you:

- A **sandbox `client_id`** and (if applicable) `client_secret`.
- Sandbox FHIR base URLs (one for R4, one for DSTU2 if you registered for both).
- Sandbox patient credentials — NextGen publishes test patients in the portal documentation.

## Step 5 — Sandbox testing

Validate against the sandbox:

- OAuth authorization redirects to your callback with a code.
- Token exchange returns access (+ refresh if `offline_access`).
- `Patient/{id}` returns the synthetic patient.
- The USCDI v3 resource categories return non-empty bundles for the synthetic patient.
- Where you registered for both DSTU2 and R4, test both — the bundle shapes differ.

## Step 6 — Wire OwnChart

Add to `infra/.env`:

```sh
OWNCHART_NEXTGEN_CLIENT_ID=<production client id>
OWNCHART_NEXTGEN_CLIENT_ID_SANDBOX=<sandbox client id>
# Only if confidential:
OWNCHART_NEXTGEN_CLIENT_SECRET=<production client secret>
```

Add to `infra/connectors.seed.yaml`:

```yaml
- slug: nextgen
  name: NextGen Healthcare
  ehr_vendor: nextgen
  fhir_base: <production FHIR R4 base URL from the portal>
  fhir_base_sandbox: <sandbox FHIR R4 base URL from the portal>
  client_id_env: OWNCHART_NEXTGEN_CLIENT_ID
  client_id_env_sandbox: OWNCHART_NEXTGEN_CLIENT_ID_SANDBOX
  # Only if confidential:
  client_secret_env: OWNCHART_NEXTGEN_CLIENT_SECRET
  scopes: openid fhirUser launch/patient patient/*.read
```

Forward env vars through `infra/docker-compose.yml`. Restart the API container. **Connect NextGen** appears at `/connectors`.

## Step 7 — Production rollout

NextGen production access is mostly self-service (faster than Athena's human review), but individual practices still need to expose the Patient Access API for their patient population. This is a practice-side setting, not yours.

For NextGen Office practices: most have FHIR patient access on by default (21st Century Cures requires it). NextGen Enterprise practices vary more — server-based installations may require an explicit admin action.

The patient-side prerequisite is the same as for Athena and ModMed: the patient must be enrolled in that practice's patient portal first.

## Difference from Epic: per-practice, not auto-distributed

Unlike Epic's USCDI v3 auto-download (where one client_id "Ready for Production" flag silently makes your app available at every Epic customer on a 12-hour cycle), NextGen production rollout is per-practice or per-installation. Expect more manual coordination — and expect some practices to have never enabled an external patient app before.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| OAuth lands on a 404 after consent | Redirect URI mismatch (NextGen's flow is strict about exact-match URIs) | Re-verify the registered URI vs. what OwnChart sends |
| `patient/*.read` returns empty for a patient who definitely has data at that practice | Practice hasn't enabled FHIR patient access for your app | Practice admin enables it (may require NextGen support involvement) |
| Sandbox works but production token exchange returns 401 | Wrong `client_id` for the environment (sandbox vs. production) | Confirm env-var resolution in `connectors.seed.yaml` |
| Bundle is in DSTU2 shape but OwnChart expected R4 | The practice is on NextGen Enterprise + DSTU2, not R4 | Either register for both DSTU2 and R4 (recommended) or stick to R4-only practices |

## Open items / known unknowns

- The exact URL of NextGen's developer portal changes; the canonical entry point is <https://www.nextgen.com/api>.
- The split between NextGen Office and NextGen Enterprise developer flows has been documented but evolves; check the portal's current product-selector before assuming one path covers both.
- Whether the same `client_id` works at *every* NextGen-using practice depends on whether the practice has FHIR patient access enabled at all. Some smaller practices have never turned it on.

If you've recently run through NextGen registration, please open a PR with corrections.
