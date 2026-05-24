# Registering for ModMed / EMA FHIR access

> ModMed (Modernizing Medicine) is the EHR for many specialty practices — dermatology, ophthalmology, orthopedics, gastroenterology, plastic surgery, pain management, and OB-GYN. The patient-facing product is **EMA®** (`gGastro` for gastroenterology). If your specialty care happens at a private specialty practice, the chances it runs on ModMed are higher than the chances it runs on Epic.

> **Read [CONNECTORS.md](./CONNECTORS.md) first** if you haven't — it explains the universal pattern these vendor-specific guides assume.

> **Status note:** ModMed has consolidated onto the **FHIR Vendor Dashboard** for self-service patient-app registration — you no longer need to email developer-experience to get sandbox access. Production rollout is still per-practice (the practice has to enable patient FHIR access for your `client_id`), but the developer-side registration is now a portal flow. This guide reflects the Vendor Dashboard surface as of Beta 1; if it has shifted, please open a PR.

Expected time: ~30 minutes for the Vendor Dashboard registration; **per-practice** to enable production access at each specialty practice.

## Step 1 — Read the public API docs

1. Open <https://portal.api.modmed.com>. This is ModMed's developer documentation portal.
2. Identify which API surface you need. ModMed publishes two FHIR APIs:

   | API | Standard | What it does | OwnChart wants |
   |---|---|---|---|
   | **EMA Proprietary API** | FHIR R4 (custom) | Tailored FHIR R4 implementation for EMA® and ModMed Practice Management; supports CREATE/READ/SEARCH/UPDATE | Possibly — broader operations than needed |
   | **EMA and gGastro Certified FHIR API** | HL7 FHIR R4 / v4.0.1 (USCDI v1+) | 21st Century Cures-certified; SMART on FHIR; bulk export (NDJSON) | **Yes** — this is the patient-app path |

The **Certified FHIR API** is the one that maps to the patient-app model — it's the 21st Century Cures-compliant surface that practices are required to expose. The Proprietary API is more flexible but typically gated for tighter partnerships.

## Step 2 — Register in the FHIR Vendor Dashboard

ModMed's self-service registration surface is the **FHIR Vendor
Dashboard**. From the portal:

1. Sign up / sign in to the developer portal.
2. Open the **FHIR Vendor Dashboard** (the link is on the portal
   landing page; search for "Vendor Dashboard" if it has moved).
3. Create a new application. Fill in the standard SMART-on-FHIR
   app metadata.

| Field | Value |
|---|---|
| App name | `OwnChart` (or your fork's name) |
| App type | **Patient** (not Provider, not System, not Bulk) |
| FHIR version | **R4 / v4.0.1** |
| Redirect URI | `https://your-instance.example.com/api/connectors/callback` |
| Launch URL | `https://your-instance.example.com` |
| Client type | **Public client with PKCE (S256) — no client secret.** ModMed issues PKCE-only public clients for Patient apps. |
| Scopes | `openid fhirUser launch/patient patient/*.read` |
| Description | "Self-hosted platform for patients to maintain a canonical health record for their own personal use." |
| Privacy policy URL | Yours (e.g. `https://your-instance.example.com/privacy`) |

**No client secret.** A Patient app on ModMed is a public client.
If the Vendor Dashboard offers you a `client_secret` to copy,
you've selected the wrong app type — re-check that you picked
**Patient**, not Provider or System. Do not paste, store, or set a
`client_secret` env var for ModMed; there is no
`OWNCHART_MODMED_CLIENT_SECRET`, and you should not invent one.

## Step 3 — Find the FHIR base URL for a specific practice

This is the step that trips up most operators. **The patient
portal URL is not the FHIR base URL.** A patient who logs in at
`https://patient.somedermpractice.com` will see EMA, but
`patient.somedermpractice.com` is not where SMART-on-FHIR lives —
it's the EMA web UI. The FHIR base is a different host entirely.

Two ways to find the actual FHIR base for a practice:

1. **Use the ModMed public endpoint directory** (the per-practice
   list ModMed publishes for SMART-on-FHIR app builders). Search
   for the practice name; the entry lists the practice's
   production FHIR R4 base URL.
2. **Ask the practice directly.** A ModMed-using practice that
   has enabled patient FHIR access knows the URL — sometimes
   filed under "developer endpoints" or "API base" in their
   internal docs.

The shape of a ModMed FHIR R4 base URL looks like:

```
https://<practice-slug>.mmi.prod.fhir.ema-api.com/fhir/r4/
```

Concrete example. Forefront Dermatology (a real ModMed customer)
publishes:

```
https://forefrontdermatology.mmi.prod.fhir.ema-api.com/fhir/r4/
```

The `forefrontdermatology` slug is unique to that practice; every
other practice has its own. The host shape
(`<slug>.mmi.prod.fhir.ema-api.com/fhir/r4/`) and trailing slash
are constant across ModMed customers using the production
environment.

### Verify the FHIR base resolves to SMART-on-FHIR

Before wiring the URL into `connectors.seed.yaml`, fetch the
SMART discovery document and confirm it's JSON:

```sh
curl -sf https://forefrontdermatology.mmi.prod.fhir.ema-api.com/fhir/r4/.well-known/smart-configuration | head -20
```

You should get a JSON object with `authorization_endpoint`,
`token_endpoint`, `capabilities`, and a list of supported `scopes`
(including `patient/*.read`).

If you get **HTML** back, you've pasted the wrong URL — almost
always the patient portal instead of the FHIR base. Use the
endpoint directory to find the real FHIR base and re-verify.

## Step 4 — Sandbox testing

ModMed provides a sandbox environment with synthetic patient data. The sandbox FHIR base URL pattern mirrors production but on a sandbox host; the Vendor Dashboard shows the exact URL with your credentials.

Validation:
- OAuth round-trip succeeds, returning a code at your redirect URI.
- Token exchange returns access + (where supported) refresh tokens.
- `Patient/{id}` returns the synthetic patient.
- USCDI v3 resource categories (Condition, MedicationStatement, AllergyIntolerance, Observation, etc.) all return non-empty bundles where the synthetic patient has data.
- `/.well-known/smart-configuration` on the sandbox base returns JSON with the same shape as the production check above.

## Step 5 — Wire OwnChart

Add to `infra/.env`:

```sh
OWNCHART_MODMED_CLIENT_ID=<production client id>
OWNCHART_MODMED_CLIENT_ID_SANDBOX=<sandbox client id>
```

**No `OWNCHART_MODMED_CLIENT_SECRET`.** ModMed Patient apps are
public clients (PKCE only); there is no client-secret env var
because there is nothing to put in it.

Add to `infra/connectors.seed.yaml`:

```yaml
- slug: modmed
  name: ModMed / EMA
  ehr_vendor: modmed
  # Production FHIR base — use the URL from the ModMed endpoint
  # directory (or ask the practice). The trailing slash matters.
  fhir_base: https://<practice-slug>.mmi.prod.fhir.ema-api.com/fhir/r4/
  fhir_base_sandbox: <sandbox FHIR R4 base URL from the Vendor Dashboard>
  client_id_env: OWNCHART_MODMED_CLIENT_ID
  client_id_env_sandbox: OWNCHART_MODMED_CLIENT_ID_SANDBOX
  scopes: openid fhirUser launch/patient patient/*.read
```

Forward env vars in `infra/docker-compose.yml`; restart the API container. A **Connect ModMed / EMA** button will appear at `/connectors`.

If you connect to more than one ModMed-using practice, add one
row per practice — same `client_id_env`, distinct `slug`,
distinct `fhir_base`:

```yaml
- slug: modmed-forefrontderm
  name: Forefront Dermatology (ModMed / EMA)
  ehr_vendor: modmed
  fhir_base: https://forefrontdermatology.mmi.prod.fhir.ema-api.com/fhir/r4/
  client_id_env: OWNCHART_MODMED_CLIENT_ID
  scopes: openid fhirUser launch/patient patient/*.read

- slug: modmed-anotherpractice
  name: Another Practice (ModMed / EMA)
  ehr_vendor: modmed
  fhir_base: https://anotherpractice.mmi.prod.fhir.ema-api.com/fhir/r4/
  client_id_env: OWNCHART_MODMED_CLIENT_ID
  scopes: openid fhirUser launch/patient patient/*.read
```

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
| `/.well-known/smart-configuration` returns **HTML** instead of JSON | You pasted the **patient portal URL** (or some other web page) into `fhir_base`, not the FHIR R4 base URL. The portal and the FHIR base are different hosts. | Look the practice up in the ModMed public endpoint directory and copy the FHIR R4 base URL (shape: `https://<slug>.mmi.prod.fhir.ema-api.com/fhir/r4/`). Re-verify the discovery URL returns JSON. |
| "Application not found" at OAuth start | Sandbox `client_id` against production FHIR base (or vice versa) | Match `client_id` to the matching FHIR base (one env var per environment). |
| 401 on token exchange | PKCE S256 not implemented on the client side, or the wrong `code_verifier` was sent | ModMed Patient apps require PKCE S256. There is no client secret to add as a fix; if a confidential-client error suggests adding one, you registered as the wrong app type — re-register as Patient. |
| The SMART login page loads, the patient enters their credentials, login fails | Almost always a vendor-side **patient / firm / app entitlement** problem — the practice has not yet enabled FHIR for your `client_id`, or the patient's account is not provisioned for the practice's patient portal, or the firm-level setting that exposes patient FHIR is off. **Not an OwnChart bug.** | Ask the practice's office manager / admin to confirm (a) the patient is enrolled in their patient portal, (b) the practice has enabled patient FHIR access for your registered app, and (c) the firm-level (vs office-level) FHIR setting is on. May require a ticket with ModMed support. |
| Empty bundles from a known-good practice | Practice hasn't enabled patient FHIR for your app | Same fix as above — practice admin enables; may require ModMed support involvement. |
| `client_secret` field appears in the OAuth flow | You registered as Provider or System, not **Patient**. Patient apps are public clients (PKCE S256 only). | Re-register the app in the FHIR Vendor Dashboard with **App type: Patient**; there should be no client secret offered. Do not invent an `OWNCHART_MODMED_CLIENT_SECRET` env var. |
| Support ticket includes a copy-pasted browser URL after sign-in | Don't paste full SMART/OAuth callback URLs into tickets — they carry `code`, `state`, and `session_code` query-string values that are scoped credentials. | Redact everything after `?` before pasting. Share the response status + error code/message text, not the URL. |

## Open items / known unknowns

- The exact URL / branding of the FHIR Vendor Dashboard may shift; check <https://portal.api.modmed.com> for current routing.
- The public endpoint directory is the authoritative source for per-practice FHIR base URLs. If a practice you care about is missing from the directory, ask them to confirm their FHIR base before you guess at the slug.
- `offline_access` / refresh-token support may vary per practice enrollment.
- Firm vs office vs practice settings can interact in surprising ways — a setting that's "on" at firm level may still need a per-office toggle, depending on how the practice is organized in ModMed.

If you've completed a ModMed registration recently, please open a PR with specifics — concrete walkthroughs are the most valuable contributions here.
