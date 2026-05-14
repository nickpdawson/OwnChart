# Getting an athenahealth developer account

> athenahealth exposes patient-facing FHIR access through <https://www.athenahealth.com/developer-portal> with API reference docs at <https://docs.athenahealth.com/api/docs/fhir-apis>. The registration flow follows the same SMART-on-FHIR shape as Epic, but the developer experience and reviewer interaction are notably different. Expect more back-and-forth with athena's review team and longer turnaround.

> **Read [CONNECTORS.md](./CONNECTORS.md) first** if you haven't — it explains the universal pattern. Athena adds a human-review step that Epic skips.

Expected time: 30 minutes to fill out the application, then **1–4 weeks** of review before your app is approved for production.

> **Status note (v0.1b):** This guide reflects athenahealth's public developer documentation and a first-pass walkthrough. If you spot something stale or wrong, open a PR — connector setup docs improve in lockstep with people actually running through them.

## Prerequisites

Same as the Epic setup:

- An OwnChart instance with a publicly reachable HTTPS callback URL:

  ```
  https://your-instance.example.com/api/connectors/callback
  ```

- A stable email address for the developer account.
- A Terms of Service URL you control.
- ~30 minutes for the application; **patience** for the review.

## Step 1 — Create a developer account

1. Start at <https://www.athenahealth.com/developer-portal>. Click through to the developer portal sign-up.
2. The current portal canonical URL is `developer.athenahealth.com`; the API reference and FHIR specs live at <https://docs.athenahealth.com/api/docs/fhir-apis>.
3. Use your real name and a deliverable email. Athena verifies the email and may follow up with identity questions for production access.
4. Confirm the email and sign in.

## Step 2 — Choose the right product / API surface

Athena's developer portal segments access into several products:

- **athenahealth Marketplace Partner** — full integrator path, requires partnership agreement. **Not what you want.**
- **Patient API** / **Patient-facing FHIR** — patient-mediated access, SMART-on-FHIR R4. **This is the one.**
- **Provider API** — clinician-facing integration. Not what you want.

Look for the patient-facing FHIR product (the exact label has shifted over time — "Patient FHIR API," "Patient Apps," or "Athena Patient API" depending on when you're reading). If you can't find it, athena support can point you to it; mention specifically that you're building a patient-mediated SMART-on-FHIR app under the 21st Century Cures Act / ONC information-blocking rules.

## Step 3 — Create the app

The application form will ask for, at minimum:

| Field | Value |
|---|---|
| **App name** | `OwnChart` (or your fork's name) |
| **App type / audience** | **Patient-facing** |
| **Redirect URI** | `https://your-instance.example.com/api/connectors/callback` |
| **Client type** | **Public client (PKCE)** if offered; otherwise confidential and athena issues a `client_secret` |
| **SMART on FHIR version** | **R4** |
| **Scopes** | `openid fhirUser launch/patient patient/*.read` |
| **Description** | "Self-hosted platform for patients to maintain a canonical health record for their own personal use." |
| **Terms of Service / Privacy URL** | Your own |

If athena requires a **confidential client** (with a `client_secret`), you'll receive both a `client_id` and a `client_secret` after approval. Store them as:

```sh
OWNCHART_ATHENA_CLIENT_ID=...
OWNCHART_ATHENA_CLIENT_SECRET=...
```

Both go in `infra/.env`. The secret is a **real** secret — never commit it, never put it in `infra/config.yaml`.

## Step 4 — Submit for review

Unlike Epic's auto-download path, athena production access requires human review. Expect to provide:

- A description of how the patient consents to the data flow.
- A description of where data is stored and who has access.
- A privacy policy that addresses PHI handling for the patient.
- Possibly a screen recording or screenshot of the OAuth flow working against athena's sandbox.

For an OwnChart-style deployment, the answers map cleanly:

| Reviewer question | Answer |
|---|---|
| Where is patient data stored? | "On the patient's own server. Self-hosted, no third-party cloud." |
| Who has access to patient data? | "Only the patient — single-tenant, no operator." |
| How is consent obtained? | "Patient initiates the OAuth flow themselves and reviews scopes at the athena login screen." |
| What third parties receive PHI? | "None by default. Optional: an LLM provider (Anthropic) when the patient explicitly enables that feature." |
| Data retention | "Indefinite, controlled entirely by the patient. Tearing down the instance erases the data." |

## Step 5 — Sandbox first

Athena provides a sandbox FHIR endpoint and synthetic test patients. The exact URL varies; the developer portal app detail page shows the right base URL for your registered app.

From the sandbox, validate:

- OAuth round-trip succeeds, with a callback containing an authorization code.
- Token exchange returns access and (if `offline_access`) refresh tokens.
- A `Patient` resource fetch returns synthetic patient data.
- `patient/*.read` covers the USCDI v3 resource categories you expect.

## Step 6 — Wire OwnChart

Once you have your athena `client_id` (and `client_secret` if confidential):

1. Add to `infra/.env`:

   ```sh
   OWNCHART_ATHENA_CLIENT_ID=...
   # only if athena issued a secret (confidential client)
   OWNCHART_ATHENA_CLIENT_SECRET=...
   ```

2. Add an athena row to `infra/connectors.seed.yaml`:

   ```yaml
   - slug: athena
     name: athenahealth
     ehr_vendor: athenahealth
     fhir_base: https://api.platform.athenahealth.com/fhir/r4
     client_id_env: OWNCHART_ATHENA_CLIENT_ID
     # if confidential:
     client_secret_env: OWNCHART_ATHENA_CLIENT_SECRET
     scopes: openid fhirUser launch/patient patient/*.read
   ```

3. Forward the env vars through `infra/docker-compose.yml`.
4. Restart the API container. The startup seeder upserts the connector and a **Connect athenahealth** button appears at `/connectors`.

## Step 7 — Production

After athena approves your app:

- The same `client_id` works against every athena-based practice that lists your app as available for patient connection.
- Unlike Epic's auto-download, **patients may need to enable the app explicitly** within their athena patient portal in some configurations. The athena patient portal UI labels this as "Connected Apps" or similar.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Application not found" at OAuth start | `client_id` is for sandbox, target is production (or vice versa) | Use the right `client_id` for the FHIR base you're hitting |
| Token exchange returns 401 | Confidential client with wrong `client_secret`, or missing PKCE on public client | Reverify which client type athena registered; the portal app page shows it |
| `patient/*.read` returns empty bundles | The patient hasn't completed the practice's portal enrollment, or the practice hasn't enabled patient API access | Patient confirms portal access; practice admin enables patient API if needed |

## Open items / known unknowns

This guide reflects athena's documented developer flow but several details vary by region and product tier:

- Whether athena currently requires confidential clients for all patient apps, or whether PKCE-only public clients are accepted, can change.
- The exact sandbox base URL is sometimes account-scoped rather than global.
- Production rollout to individual practices is *not* the seamless 12-hour cycle that Epic offers — expect more manual coordination.

If you've run through this guide recently with details that differ from what's written here, please open a PR with corrections.
