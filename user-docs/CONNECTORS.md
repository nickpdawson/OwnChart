# FHIR Connector Setup — the universal pattern

> **Read this first.** Every EHR vendor's developer portal is shaped differently, but the core flow is the same. This page explains the pattern so the vendor-specific guides can be short.

## Why every install registers its own app

OwnChart is self-hosted. There is no central OwnChart-the-company holding a shared app registration with Epic, Athena, ModMed, NextGen, or Oracle Health. Each operator (you) registers their own app with each EHR vendor whose data they want to ingest.

This is not a workaround — it is the model. A vendor app registration is the operator's contract with that vendor. It binds:

- A specific OAuth `redirect_uri` (yours)
- A specific app description (yours)
- A specific privacy posture (yours)
- A specific support contact (yours)

If one shared registration existed, every OwnChart user would inherit one entity's compliance posture and one redirect URI. That breaks the patient-owned model. Per-install registration is the price of self-hosting; it also means a future audit, breach, or compliance change affects only that operator's deployment.

Cost: $0 for every vendor covered in these guides. Time: ~30 minutes per vendor for the application, plus 0–4 weeks of vendor review depending on the vendor.

## The pattern, in seven steps

Every vendor's flow comes down to the same seven steps. The vendor-specific guides in this directory walk through each step with vendor-specific specifics.

### 1. Create a developer account

You sign up for the vendor's developer portal with a real email. Some require identity verification or business information; for patient-app categories, most accept individual developers without a business entity.

### 2. Pick the right app category

Every vendor segments app registrations by audience:

| Audience | What it does | Want this? |
|---|---|---|
| **Patient / Consumer** | Patient-mediated FHIR access. OAuth flow runs from the patient's perspective. | **Yes** |
| **Provider / Clinician** | EHR-launched apps used by clinicians during charting. | No |
| **Partner / Vendor / Marketplace** | Full business integrations with BAA, contract, and revenue share. | No |
| **Bulk / Population** | Population-level data export for analytics. | No |

OwnChart is a **patient app**. Pick that category. If it's labeled differently (Athena calls it "Patient API," NextGen calls it "Patient Access APIs," Oracle Health calls it "Standalone Patient Launch"), the shape is the same.

### 3. Register the app — at minimum, this metadata

> **Single-origin.** OwnChart serves the API and the web UI from the
> **same host**. The redirect URI you register with every vendor is
> `https://your-instance.example.com/api/connectors/callback` — never
> a separate `api.your-instance.example.com` subdomain. If you register
> a different hostname here than the one your iOS app and browser hit,
> every callback will fail.

| Field | What you typically enter |
|---|---|
| App name | `OwnChart` (or whatever your fork is called) |
| Audience | Patient / Consumer |
| Redirect URI | `https://your-instance.example.com/api/connectors/callback` |
| Client type | Public client with PKCE (preferred) — or confidential (vendor-issued `client_secret`) when the vendor requires it |
| SMART on FHIR version | R4 |
| Scopes | `openid fhirUser launch/patient patient/*.read` |
| Description | "Self-hosted platform for patients to maintain a canonical health record for their own personal use." |
| Privacy policy URL | Yours (e.g., `https://your-instance.example.com/privacy`) |
| Terms of service URL | Yours |

### 4. Sandbox first

Every vendor offers a sandbox with synthetic patient data and pre-known test credentials. Always validate the full OAuth + FHIR-fetch round-trip against the sandbox **before** marking your app production-ready.

Sandbox validation should confirm:
- OAuth authorization returns an authorization code via your redirect URI.
- Token exchange returns an access token (and refresh, if `offline_access` is in scope).
- A `Patient/{id}` GET returns synthetic patient data.
- `patient/*.read` covers the USCDI v3 resource categories the vendor advertises.

### 5. Submit for production review (vendor-dependent)

| Vendor | Review path | Typical turnaround |
|---|---|---|
| Epic | USCDI v3 auto-download — **no human review** | ≤12 hours per customer site |
| Athena | Human review with privacy + flow documentation | 1–4 weeks |
| ModMed | Contact-driven; sales/integration team gates production access | Variable |
| NextGen | Self-service in the developer portal, with portal-side approval | Hours to days |
| Oracle Health (Cerner) | Self-service via code console for sandbox; customer-side enablement for production | Variable per site |

### 6. Get your credentials

After registration (and review, where applicable), the vendor's portal shows you:

- A **production `client_id`** (the public OAuth identifier; not secret).
- A **sandbox `client_id`** (distinct from production).
- A **`client_secret`** *only if* the vendor required a confidential client type. This **is** secret.
- One or more **FHIR base URLs** — the sandbox base differs from the production base.

Client IDs are public OAuth identifiers. They identify your app to the vendor; they are not authentication. With PKCE-only public clients, no secret exists. With confidential clients, the secret is what authenticates the token-exchange step.

### 7. Wire OwnChart

For each vendor, add three things:

**a.** Environment variables in `infra/.env`:

```sh
OWNCHART_<VENDOR>_CLIENT_ID=<production client id>
OWNCHART_<VENDOR>_CLIENT_ID_SANDBOX=<sandbox client id>
# Only if confidential:
OWNCHART_<VENDOR>_CLIENT_SECRET=<production client secret>
```

**b.** A row in `infra/connectors.seed.yaml`:

```yaml
- slug: <vendor>
  name: <vendor display name>
  ehr_vendor: <vendor>
  fhir_base: <production FHIR R4 base URL>
  fhir_base_sandbox: <sandbox FHIR R4 base URL>
  client_id_env: OWNCHART_<VENDOR>_CLIENT_ID
  client_id_env_sandbox: OWNCHART_<VENDOR>_CLIENT_ID_SANDBOX
  # Only if confidential:
  client_secret_env: OWNCHART_<VENDOR>_CLIENT_SECRET
  scopes: openid fhirUser launch/patient patient/*.read
```

**c.** Forward the env vars through `infra/docker-compose.yml` so the API container can read them at startup.

Then restart the API container. The startup seeder upserts the connector row, and a **Connect <vendor>** button appears on the `/connectors` page.

## What changes per vendor

The pattern is universal; the specifics differ in these dimensions:

| Dimension | Variation |
|---|---|
| Where the developer portal lives | Each vendor has its own URL |
| What "patient app" is called | Patient API / Patient Apps / Patient Access APIs / Standalone Patient Launch |
| Whether `offline_access` is supported | Epic supports it but adds operational drag; Athena requires it for refresh; ModMed varies |
| Whether the client is public (PKCE) or confidential | Trending public-with-PKCE; some vendors still issue secrets |
| How production review works | Auto-download (Epic) vs. human review (Athena) vs. contact-driven (ModMed) vs. self-service (NextGen, Oracle Health) |
| Whether per-customer enablement is required | Epic auto-distributes; Athena requires per-practice enablement; Oracle Health requires per-site sign-off |
| Sandbox patient credentials | Each vendor publishes its own test patient |

## Prerequisites for the whole flow

Before you start with any vendor:

- An OwnChart instance running somewhere with a **publicly reachable HTTPS URL for the OAuth callback** at `https://your-instance.example.com/api/connectors/callback`. (The rest of OwnChart can stay private — only the callback path needs to be public. Cloudflare Tunnel, Tailscale Funnel, ngrok, or a real reverse proxy all work.)
- A stable email address. Ideally not your personal medical-record email — use a dedicated one.
- A privacy-policy URL you control. If your OwnChart instance is on `your-instance.example.com`, add a `/privacy` page (the public OwnChart site has one at <https://www.ownchart.me/privacy> you can copy from).
- A terms-of-service URL you control (same logic).
- ~30 minutes per vendor for the application form. Patience for the review where applicable.

## Vendor guides

| EHR | Audience focus | Guide | Review path |
|---|---|---|---|
| **Epic** | Largest US hospitals, academic medical centers (Cleveland Clinic, Stanford, Mass General Brigham, Mayo, Kaiser, ...) | [EPIC_SETUP.md](./EPIC_SETUP.md) | USCDI v3 auto-download |
| **athenahealth** | Mid-market ambulatory practices | [ATHENA_SETUP.md](./ATHENA_SETUP.md) | Human review |
| **ModMed** | Specialty practices (dermatology, ophthalmology, orthopedics, gastroenterology, plastic surgery) | [MODMED_SETUP.md](./MODMED_SETUP.md) | Contact-driven |
| **NextGen** | Ambulatory practices, community health centers | [NEXTGEN_SETUP.md](./NEXTGEN_SETUP.md) | Self-service portal |
| **Oracle Health (Cerner)** | Health systems on Cerner Millennium / Oracle Health | [CERNER_SETUP.md](./CERNER_SETUP.md) | Self-service sandbox; per-site production |

More vendors (Allscripts/Veradigm, eClinicalWorks, MEDITECH, Kaiser patient-mediated, MyHealthONE) land as people run through them. If you complete a registration for a vendor not yet covered, please open a PR with a new guide following the shape above — your concrete walkthrough is worth more than any vendor's PDF.

## Reality check on `offline_access`

`offline_access` is the OAuth scope that grants refresh tokens (long-lived re-auth). Most vendors gate it differently from `patient/*.read`:

- **Epic** supports `offline_access` but doing so flips your app onto the "per-customer credential upload" path — every Epic customer (every hospital) must individually accept refresh-token-issuing apps. That's operational drag for marginal benefit. OwnChart's default is **not** to request `offline_access`; the user re-authenticates every ~60 minutes during active syncing.
- **Athena** typically issues refresh tokens with `offline_access` for patient apps.
- **ModMed**, **NextGen**, **Oracle Health** — `offline_access` support varies; check the vendor-specific guide.

For OwnChart's design (patient runs sync periodically, server is self-hosted, no background daemon that needs always-on tokens), skipping `offline_access` for Epic specifically and accepting it where the vendor offers it cleanly elsewhere is a reasonable default. Add it later per-vendor if your usage pattern warrants.

## What goes wrong (vendor-agnostic)

| Symptom | Cause | Fix |
|---|---|---|
| OAuth lands on a 404 after consent screen | Redirect URI mismatch — case, slash, scheme, or port differs from what you registered | Re-check exact match; vendors compare strings, not URLs |
| "App not authorized" or generic OAuth error | App still pending vendor review, or per-customer enablement hasn't happened | Wait the documented window for that vendor; for per-customer cases, the customer's portal admin enables it |
| Token works but `Patient/{id}` returns 401 | Scope mismatch — `patient/*.read` requires the user to actually be the patient | Confirm the user is the patient in that EHR (not a delegate); some vendors require explicit "Connected Apps" enrollment on the patient's side |
| Token works but bundles are empty | Patient enrolled in the portal but the practice hasn't enabled patient API access | Practice admin enables FHIR patient access |
| Sandbox returns 200, production returns 401 | You're using the sandbox `client_id` against the production base URL (or vice versa) | Check which env var resolves; the `connectors.seed.yaml` row's `client_id_env` should match the FHIR base |
