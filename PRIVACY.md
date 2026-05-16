# Privacy

*Last updated: 2026-05-11*

OwnChart is a self-hosted, patient-owned platform. The most important sentence in this policy:

> **The developer of OwnChart does not operate a service that receives your health data.** The OwnChart iOS app and OwnChart server-side software talk only to a server you set up and control. Nothing about you, your record, or your activity reaches us — because there is no "us" running anything that could.

This document describes the narrow circumstances in which any data *is* handled by the project maintainer, and what is promised about it.

## The short version

- **The marketing site (`www.ownchart.me`)** — static pages served from Cloudflare's edge. No analytics. No tracking pixels. No cookies set by us.
- **The OwnChart iOS app** — reads HealthKit data with your permission and sends it only to the OwnChart server URL you configure. We never receive it. There is no developer-operated backend.
- **The OwnChart server software** — runs on hardware you control. Your data stays on your disk. AI features are off by default and require explicit consent per call.
- **The public demo at `demo.ownchart.me`** — operated by the project maintainer for App Store review and curious visitors. Preloaded with synthetic FHIR-sandbox data; no real patient records. Web-server logs are kept ≤14 days for abuse prevention, then deleted.

## What the iOS app collects

Nothing that reaches the developer.

The app does not contain:

- Analytics SDKs (no Mixpanel, Amplitude, Segment, Google Analytics, PostHog, or similar)
- Crash reporters (no Crashlytics, Sentry, Bugsnag)
- Advertising or attribution SDKs
- Any networking other than to the server URL you configure

When you grant HealthKit permission, the app reads the data classes you allowed and transmits them to your configured server. That is the only network destination.

## What HealthKit data the app reads

Only what you grant. Apple's HealthKit framework requires you to approve each data category individually. Common categories OwnChart can read when you enable them:

- Activity (steps, distance, active energy)
- Heart (heart rate, HRV, resting heart rate, VO₂ max)
- Sleep (analysis, duration, stages)
- Workouts
- Body measurements (weight, height, body composition)
- Medications and symptoms logged in Apple Health
- Mindfulness sessions

Data is sent to your server on a schedule you control. The app maintains a local pending-upload queue if your server is offline; this queue is cleared on successful delivery and persists no longer than necessary.

## What the server software handles (on your own hardware)

Your medical records and HealthKit data live on disk on the server *you* operate. The project maintainer does not have access. The complete source of the server software is open in this repository; the relevant doctrine and security model are documented in [PHILOSOPHY.md](./PHILOSOPHY.md) and [SECURITY.md](./SECURITY.md).

When you enable AI features on your server:

- Your server may send the relevant portion of your record to an LLM provider you configure — Anthropic, OpenAI, Google, or a local-model endpoint you run yourself.
- This requires your explicit, scoped consent before each call (the consent gate).
- Each call creates a local audit record (`ModelRun`) on your server. We do not see it.
- That LLM provider's privacy policy then governs whatever you sent. The project does not have a relationship with those providers on your behalf.

## What `demo.ownchart.me` handles

This is the one surface where the project maintainer operates a service that touches user activity. It has been kept deliberately minimal:

- The demo server runs the same OwnChart software you would run yourself.
- It is preloaded with synthetic FHIR-sandbox patient data. There is no real patient information.
- The single shared `demo@ownchart.me` account is public; anyone may sign in.
- Standard NGINX access logs are kept (IP address, timestamp, request path, user agent) for **no more than 14 days** for abuse mitigation. These logs are not joined to any identity, not analyzed for product purposes, not shared with third parties.
- The demo server uses an LLM provider configured by the maintainer. AI features run against synthetic data with the standard consent gate.

**Don't put real medical information into the demo.** Anything you type into a form on the demo (e.g., the Ask box) is processed by the demo's LLM provider; because the underlying record is synthetic and the account is shared, your input may also be visible to other people using the demo account after you.

## What the marketing site does

A static site served from Cloudflare Pages. To make it work:

- Cloudflare's edge may set short-lived essential cookies (`__cf_bm`, `cf_clearance`) for bot protection. These are first-party, short-lived, required to defend the site from automated abuse, and do not identify you across other sites. We do not read them.
- Cloudflare retains technical edge logs for its own bot-management purposes per its [privacy policy](https://www.cloudflare.com/privacypolicy/).
- The site has no analytics scripts. No Google Analytics, no Plausible, no Fathom, no anything. We do not know when you visit.
- The site embeds no third-party iframes, videos, or web fonts. All fonts are system fonts; all images are first-party.

## GitHub

The source code lives on GitHub. When you view or interact with the repository there, [GitHub's privacy policy](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement) applies. The project does not receive anything beyond what GitHub publicly shows (issue and pull-request authorship, stars, etc.).

## Children

OwnChart is intended for adults managing their own (or a delegated person's) health record. The project does not knowingly collect data from anyone because it does not collect data at all. If a parent installs the iOS app on a child's device, the child's HealthKit data flows only to the parent's chosen server.

## International data transfers

Because no personal data is transferred to the project maintainer, this section is short. Cloudflare serves the marketing site from edge nodes worldwide. GitHub hosts the source on its own infrastructure. The demo server is located in the United States.

## Your rights

You can:

- Read every line of code that touches your data — the source is published on GitHub.
- Inspect every AI call your server made, with a full audit record.
- Delete everything by tearing down your server.
- Use the project under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) — fork it, modify it, and self-host for personal, educational, research, or caregiver use. See [LICENSE](./LICENSE) for what counts as commercial use (which requires written permission).

You do not need to request a data export from the developer because the developer does not have your data.

## Changes to this policy

Material changes appear in the git history of `site/privacy.html` and `PRIVACY.md` in this public repository. The current version is always at <https://www.ownchart.me/privacy>. The *Last updated* date at the top of this document reflects the most recent material change.

## Contact

For privacy questions, open an issue at <https://github.com/nickpdawson/OwnChart/issues>. For sensitive disclosures, open a private GitHub Security Advisory at <https://github.com/nickpdawson/OwnChart/security/advisories/new>.

---

*OwnChart is not a medical device, does not provide medical advice, and is not a substitute for clinical judgment. AI outputs are research-partner suggestions, not diagnoses or treatment recommendations.*
