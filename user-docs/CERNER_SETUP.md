# Registering for Oracle Health (Cerner) FHIR access

> Oracle Health Millennium (formerly Cerner Millennium, since the 2022 Oracle acquisition) runs at many large health systems — VA, DoD, Intermountain Health, Banner Health, Atrium, Adventist, and many others. If your hospital is not on Epic, the second-most-likely answer is Oracle Health (Cerner).

> **Read [CONNECTORS.md](./CONNECTORS.md) first** if you haven't — it explains the universal pattern these vendor-specific guides assume.

Expected time: ~30 minutes to register for sandbox in **code Console**; production rollout is **per-site**.

## A brief naming note

The developer portal you'll interact with is still branded as **"code"** and lives at **<https://code.cerner.com>** (URL still works post-acquisition). Some docs now refer to it as the **Oracle Health Developer Program**. The platform itself is **Millennium**, the modern FHIR surface is **Ignite APIs** (FHIR R4), and the underlying technology is sometimes still called "Cerner Millennium" in conversation. They are all the same thing.

For 2026, **FHIR R4 (Ignite APIs)** is the recommended path for new integrations. The older DSTU2 surface still exists but should not be the default for new apps.

## Step 1 — Create a CernerCare account

1. Go to <https://code.cerner.com>.
2. Sign up for a **CernerCare** account if you don't already have one. (The "CernerCare" branding may be replaced by Oracle SSO in the future, but at present this is the credential that gates the developer console.)
3. Verify your email.

## Step 2 — Open the code Console

After sign-in, the **code Console** is the self-registration UI for SMART on FHIR apps. From here you can:

- Register an app for the development environment.
- Choose scopes.
- Configure callback URIs.
- Test against Oracle Health's synthetic sandbox.

The console requires a CernerCare account; you cannot register an app anonymously.

## Step 3 — Register a Standalone Patient Launch app

The Cerner / Oracle Health SMART on FHIR launch model is one of two flavors:

| Launch type | Used by | OwnChart wants |
|---|---|---|
| **EHR Launch** | Apps launched from inside the EHR by a clinician | No |
| **Standalone Launch** | Apps launched externally (web, mobile) that initiate OAuth themselves | **Yes** |

Within Standalone, the **`launch/patient` scope** is what makes the OAuth flow patient-facing rather than provider-facing. Oracle Health supports this scope explicitly: during authorization, the patient is prompted to confirm which patient context they're authorizing.

Registration fields in code Console:

| Field | Value |
|---|---|
| App name | `OwnChart` (or your fork's name) |
| Launch type | Standalone |
| Audience | Patient |
| Redirect URI | `https://your-instance.example.com/api/connectors/callback` |
| Client type | Public client with PKCE |
| SMART version | R4 (Ignite APIs) |
| Scopes | `openid fhirUser launch/patient patient/*.read` |
| Description | "Self-hosted platform for patients to maintain a canonical health record for their own personal use." |

## Step 4 — Sandbox testing

Oracle Health's sandbox is hosted as part of code Console and provides synthetic patient data. The base URL pattern looks like:

```
https://fhir-myrecord.cerner.com/r4/<sandbox-tenant-id>/
```

(The exact tenant ID appears in your code Console app detail page.)

Validate:

- OAuth round-trip succeeds with a code at your callback.
- Token exchange returns access + refresh (if `offline_access` is requested).
- `Patient/{id}` returns synthetic patient data.
- USCDI v3 resource categories return non-empty bundles for the test patient.
- The `iss` claim in the SMART launch matches the FHIR base URL.

## Step 5 — Wire OwnChart

Add to `infra/.env`:

```sh
OWNCHART_CERNER_CLIENT_ID=<production client id>
OWNCHART_CERNER_CLIENT_ID_SANDBOX=<sandbox client id>
```

Note: Oracle Health typically issues **PKCE-only public clients** for patient apps. No `client_secret`. If your registration came out as a confidential client, double-check that with code Console — patient-facing apps should be public.

Add to `infra/connectors.seed.yaml`:

```yaml
- slug: cerner
  name: Oracle Health (Cerner)
  ehr_vendor: cerner
  fhir_base: <production FHIR R4 base URL — per-site, see Step 6>
  fhir_base_sandbox: https://fhir-myrecord.cerner.com/r4/<sandbox-tenant-id>/
  client_id_env: OWNCHART_CERNER_CLIENT_ID
  client_id_env_sandbox: OWNCHART_CERNER_CLIENT_ID_SANDBOX
  scopes: openid fhirUser launch/patient patient/*.read
```

Forward env vars through `infra/docker-compose.yml`; restart the API container; **Connect Oracle Health** appears at `/connectors`.

## Step 6 — Production rollout (per-site)

Oracle Health's production model is **per-customer / per-site**, not auto-distributed.

What this means concretely:

- Each Oracle Health customer (each hospital or health system) hosts its own FHIR endpoint at its own base URL.
- The patient app's `client_id` must be **whitelisted** at each customer's site by their IT administration.
- Apps go through a per-site app-registration review — sometimes light (an admin clicks a button in their console), sometimes heavier (security review, technical review).

For a patient running OwnChart at home connecting to their own hospital's Cerner instance:

1. They (or you, the OwnChart instance operator) reach out to the hospital's IT team or patient portal team.
2. The hospital admin registers your `client_id` against their Oracle Health Millennium instance.
3. They give the patient the FHIR base URL for their site.
4. That base URL goes into your `connectors.seed.yaml` (you may need multiple `cerner_*` connector rows for multiple health systems — one per FHIR base URL).

This is more friction than Epic's auto-download model. The upside: it scales gracefully when you only need one or two hospital systems' data; you're not asking every hospital in the country to enable a unilateral patient app.

## Multi-site setup pattern

If you connect to more than one Oracle Health–using hospital, you'll have multiple FHIR base URLs but typically **one production `client_id`** (some health systems issue per-site client IDs, others accept your existing one). Wire it as multiple rows in `connectors.seed.yaml`:

```yaml
- slug: cerner-myhealthsystem
  name: My Health System (Oracle Health)
  ehr_vendor: cerner
  fhir_base: https://fhir-myrecord.cerner.com/r4/<my-health-system-tenant-id>/
  client_id_env: OWNCHART_CERNER_CLIENT_ID
  scopes: openid fhirUser launch/patient patient/*.read

- slug: cerner-anotherhospital
  name: Another Hospital (Oracle Health)
  ehr_vendor: cerner
  fhir_base: https://fhir-myrecord.cerner.com/r4/<another-tenant-id>/
  client_id_env: OWNCHART_CERNER_CLIENT_ID
  scopes: openid fhirUser launch/patient patient/*.read
```

Each row becomes a separate "Connect ..." button in the OwnChart UI.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| OAuth fails with "client not authorized for this issuer" | Your `client_id` isn't whitelisted at that hospital's site | Hospital IT registers your app on their Millennium instance |
| Token exchange 401 | Sandbox `client_id` against a production base URL (or vice versa) | Match env var to FHIR base |
| Sandbox works, production returns empty bundles | Patient not enrolled in that hospital's patient portal | Patient enrolls in MyChart-equivalent for that hospital |
| `fhirUser` returns null after auth | Standalone launch didn't bind a patient context | Confirm `launch/patient` is in your registered scopes |

## Open items / known unknowns

- The eventual full migration of code.cerner.com → docs.oracle.com / Oracle Cloud Console is in progress. Bookmark current URLs but expect them to redirect over the next few years.
- Some Oracle Health customer sites still expose DSTU2 only; the recommended path is R4 (Ignite) but field-by-field, a few sites may not have completed the cutover.
- Per-site app whitelist friction varies wildly. Some health systems are well-staffed for patient app review; others have never done it before.

If you've completed a registration with a specific health system recently, please open a PR — concrete per-site notes are the most valuable.
