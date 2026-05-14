# Registering an Epic FHIR app (Orchid)

> Epic exposes patient-facing FHIR access through a developer portal informally called "Orchid" at <https://fhir.epic.com>. One registration covers every Epic-based health system. Once your app is approved for auto-download, individual Epic customers (Cleveland Clinic, Stanford, Mass General Brigham, Mayo, Kaiser, your local hospital) automatically gain the ability to connect to your OwnChart instance — usually within 12 hours of you flipping the "Ready for Production" switch.

> **Read [CONNECTORS.md](./CONNECTORS.md) first** if you haven't — it explains the universal pattern. Epic is the easiest of the major vendors thanks to USCDI v3 auto-download.

Expected time: ~30 minutes. One-time setup per OwnChart deployment.

## Prerequisites

- An OwnChart instance reachable on a public HTTPS URL **for the OAuth callback path only**. The callback path is:

  ```
  https://your-instance.example.com/api/connectors/callback
  ```

  You can keep the rest of OwnChart on your LAN or tailnet. Only the callback needs to terminate publicly. Cloudflare Tunnel, Tailscale Funnel, a reverse proxy on a real domain, or ngrok (for testing) all work.
- An email address you're willing to associate with the registration.
- A "Terms of Service" URL you control. A simple `/tos` page on the same domain is fine; the content can be a one-paragraph "this is my self-hosted patient record" notice.

## Step 1 — Create an Epic developer account

1. Go to <https://fhir.epic.com> and click **Sign up**.
2. Use your real name and a stable email. Epic does verify deliverability.
3. After confirming your email, sign in and accept the Epic Developer Terms.

You now have access to the developer portal and a sandbox endpoint.

## Step 2 — Create a new app

From the developer portal:

1. Open **Apps** → **Create New App**.
2. Fill out the **Identification** panel with the following values:

   | Field | Value | Notes |
   |---|---|---|
   | **App name** | `OwnChart` | Or your fork's name |
   | **Audience** | **Patients** | Not "Healthcare Administrator," not "Clinical Team" |
   | **Endpoint URI (redirect URI)** | `https://your-instance.example.com/api/connectors/callback` | Must match exactly — Epic compares the string |
   | **Confidential client** | **No** | OwnChart uses PKCE; no client_secret |
   | **Dynamic client registration** | **No** | Not applicable for patient apps |
   | **SMART on FHIR Version** | **R4** | Required for USCDI v3 |
   | **SMART Scope Version** | **v1** | |
   | **FHIR ID Generation Scheme** | **Unconstrained FHIR IDs** | |
   | **Automatic Client Distribution** | **USCDI v3 + Enable Auto-download** | This is what skips Epic review |
   | **Terms of Service URL** | `https://your-instance.example.com/tos` | Any URL you control |

3. **App description** — paste this (or your own variant):

   > Self-hosted platform for patients to maintain a canonical health record for their own personal use.

## Step 3 — Set scopes

Under **OAuth 2.0 Scopes**, request:

```
openid fhirUser launch/patient patient/*.read
```

**Deliberately do not request `offline_access`** unless you understand the trade-off.

The trade-off: `offline_access` lets you refresh tokens silently. But once you ask for it, the USCDI v3 auto-download path requires you to upload per-customer client credentials to every Epic installation that uses your app. That's operational drag for marginal benefit — without `offline_access` you simply re-authenticate every ~60 minutes when actively syncing, and OwnChart handles that gracefully.

If you do want `offline_access`, plan for the per-customer credential upload workflow before flipping to production.

## Step 4 — Intended Purposes and Users

| Section | Check |
|---|---|
| **Intended Purposes** | ✓ Educational Resources, ✓ Individuals' Access to their EHI |
| **Intended Users** | ✓ Individual / Caregiver |

Leave everything else unchecked. OwnChart is a patient app, not a clinical-team or population-health app.

## Step 5 — Data Use Questionnaire

Epic's data use questionnaire is filled out from the patient-tool perspective. Recommended answers for an OwnChart-style deployment:

| Question | Answer |
|---|---|
| Does the app developer allow users to obtain a complete record of the data that have been collected about them? | **Yes, complete record** |
| Does the app developer use data about a user for reasons other than providing direct services to the user? | **No** |
| For how long does this app store user data? | **Indefinitely** *(the longitudinal record is the product)* |
| Does this app allow users to delete all of the data that have been stored about them? | **Yes** |
| Other than the user, who has access to user data? | **No one** *(self-hosted, single-tenant; no third-party operator)* |
| Does this app allow users to obtain a complete record of who has accessed data about them? | **Yes** *(the `ModelRun` audit log)* |
| Is user data retained after a user deletes the app and closes their account? | **No** *(tearing down the OwnChart instance erases the data)* |

> **Save each section before moving on.** Epic's UI doesn't always make it obvious which section still needs saving. The "Save & Ready for Production" button stays disabled while any required answer is unsaved.

## Step 6 — Grab your Client IDs

Once the form is saved, the app detail page shows two client IDs:

- **Non-Production / Sandbox Client ID** — usable immediately against Epic's sandbox.
- **Production Client ID** — distributed to Epic customers on the auto-download cycle once you mark the app Ready for Production.

> **Client IDs are public OAuth identifiers, not secrets.** They identify the app to Epic; the security boundary is PKCE. It's fine to put them in `infra/.env` and to share them with anyone else running your fork. Don't try to invent a secrets-management workflow for client IDs.

## Step 7 — Wire OwnChart

1. Copy the two client IDs into your `infra/.env`:

   ```sh
   OWNCHART_EPIC_CLIENT_ID=00000000-0000-0000-0000-000000000000
   OWNCHART_EPIC_CLIENT_ID_SANDBOX=00000000-0000-0000-0000-000000000000
   OWNCHART_PUBLIC_BASE_URL=https://your-instance.example.com
   ```

2. Confirm `infra/connectors.seed.yaml` has the Epic row (it ships seeded by default). The row references `client_id_env: OWNCHART_EPIC_CLIENT_ID`, which the startup seeder resolves against the environment.

3. Restart the API container. The seeder upserts the connector row, and the **Connect Epic** button appears on the `/connectors` page in the OwnChart UI.

## Step 8 — Test against the sandbox first

Before flipping the app to production, validate end-to-end against Epic's sandbox:

- **Sandbox FHIR base:** `https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/`
- **Test credentials** (publicly published by Epic): `fhircamila / epicepic1`

From OwnChart's `/connectors` page, click **Connect Epic (Sandbox)**, sign in as `fhircamila`, and confirm you receive a callback, a Patient record, and the standard USCDI categories (Conditions, MedicationStatements, AllergyIntolerances, Observations, etc.) start landing in your timeline.

If the callback fails, the most common cause is a redirect-URI mismatch — Epic compares the registered URI string-for-string, including trailing slash, scheme, and port.

## Step 9 — Flip to production

When sandbox testing passes:

1. In the Epic developer portal, mark the app **Ready for Production**.
2. **Wait up to 12 hours.** Epic customers receive the client record on a rolling cycle; until your local hospital's Epic instance has the record, attempts to authenticate produce a generic OAuth error from their side. There is no way to short-circuit this.
3. After the wait, return to OwnChart and click **Connect Epic** against the real production endpoint of any health system that uses you. The OAuth flow takes you to that health system's MyChart login, then back to OwnChart with a token. Records start importing.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| OAuth redirect lands on a 404 | Redirect URI mismatch | Recheck registered URI against actual callback URL. Case- and slash-sensitive. |
| "App not authorized for this organization" | The 12-hour auto-download window hasn't elapsed yet | Wait. Then retry. |
| Token works but no records appear | Patient context didn't bind | Confirm `launch/patient` was in scopes; check the EHR's patient-portal opt-ins |
| Sandbox `fhircamila` returns empty bundles | Sandbox occasionally rebuilds | Try the other sandbox patients listed in Epic's docs |

## Generalizing to other Epic-side tools

The Epic developer portal also covers **CareEverywhere, Bridges, and Workshop** apps. None of those are what you want — those are clinician-facing or institution-facing integrations and are gated behind different review processes. The only path for a self-hosted patient app is the **Patients audience** registration described above.
