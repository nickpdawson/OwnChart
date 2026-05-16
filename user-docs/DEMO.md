# Demo walkthrough

> A public demo of OwnChart 0.1 alpha runs at **<https://demo.ownchart.me>**.
> It uses synthetic data only. Read this page before you sign in —
> there are a few things to know.

## What the demo is

A read-only OwnChart instance loaded with a synthetic patient bundle.
Same code as a self-hosted instance, same UI surfaces, same Ask /
Events / Dossiers / Review / Timeline flows. The data is fictitious;
the experience is real.

## How to sign in

| Field | Value |
|---|---|
| URL | <https://demo.ownchart.me> |
| Email | `demo@ownchart.me` |
| Password | `MYHEALTHdata` |

These credentials are intentionally public — the account is shared,
read-only, and exists exactly to be poked at.

## What you can do

- **Ask** any question against the synthetic record. Try the examples
  from the home screen, or invent your own (the patient has had
  surgeries, an ENT story, knee history, sleep and HRV data, photos
  with EXIF).
- **Open an Event** to see how Event Intelligence pulls operative
  notes, anesthesia records, discharge instructions, wearable
  windows, and travel context around a procedure date.
- **Browse Dossiers** — multi-year topic case files with patterns,
  trends, and pinned conversations.
- **Inspect sources.** Every fact in the demo points back to a
  synthetic CCDA, FHIR resource, PDF, or HealthKit-shaped sample.
- **Use the iOS companion** (TestFlight) and pair it to
  `https://demo.ownchart.me` to see how the native client renders
  the same record.

## What you cannot do

The demo is **read-only**, enforced at the API. That means:

- Uploads (photos, PDFs, voice notes, HealthKit pushes) are rejected.
  The UI may show an upload control; the backend refuses to persist.
- New conversations save transiently but do not persist across
  sessions (the read-only gate covers writes to your conversation
  history too).
- Save-as-Event, Attach-to-Event, rename, and alias actions are
  visible but no-ops.
- LLM calls happen normally so Ask / EI work, but the responses are
  not saved.
- EHR connector OAuth is disabled on the demo.

**Do not upload real PHI to the demo.** Even if a write succeeded by
accident, the demo database is publicly readable by anyone with the
shared credentials. Use the demo to evaluate the product, then stand
up your own instance for your real record.

## Demo limitations to expect

### Upload caps

The demo sits behind Cloudflare Free, which caps request bodies at
**100 MB**. Even with the demo's own write-gate aside, large PDFs and
HEIC bursts would hit the CDN cap before reaching the origin. Your
own self-hosted instance with direct DNS is bound only by your proxy
config (recommended: 200 MB — see [REVERSE_PROXY.md](./REVERSE_PROXY.md)).

### LLM rate limits

The demo runs on a shared, rate-limited Anthropic budget. If Ask /
Event Intelligence stalls with "thinking…" for unusually long, the
quota is likely throttling. On a self-hosted instance with your own
key (or a BYOK user key), you don't share that ceiling with anyone.

### Demo snapshot age

The demo is rebuilt from `dev` → `main` releases. Features in flight
on `dev` may not be live on the demo yet. The release notes always
say which commit the demo was built from.

### No connector-cred state

Provider connector rows are visible (so you can see what the connect
UI looks like) but no live OAuth tokens — the demo doesn't actually
talk to Epic / Athena / ModMed.

## Where to go after the demo

If the demo answers a question you cared about (or visibly doesn't):

- **Stand up your own** — [INSTALL.md](./INSTALL.md), then connect
  your own records via [CONNECTORS.md](./CONNECTORS.md).
- **Try the iOS companion** — <https://testflight.apple.com/join/z8QemcTe>.
  You can pair it to either the demo (read-only) or your own
  instance.
- **Understand the model** — [PHILOSOPHY.md](../PHILOSOPHY.md) for
  the doctrine, [RISK.md](./RISK.md) for the plain-English risk
  contract, [SECURITY.md](../SECURITY.md) for the threat model.
- **See what's shipped vs roadmap** — [SHIPPED_VS_ROADMAP.md](./SHIPPED_VS_ROADMAP.md).

## A note on the synthetic patient

The demo patient has a multi-decade longitudinal record assembled from
synthetic FHIR bundles, fabricated CCDAs, manufactured clinical notes,
and HealthKit-shaped sample data. They have surgeries, knee history,
hearing data, sleep / HRV / training data, and life events. They are
**not a real person**, and they are **not Nick**. If anything in the
demo looks like it might be drawn from a real chart, it isn't — the
record was constructed to exercise the product, not to publish
anyone's actual story.
