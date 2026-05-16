# Risk, Privacy, Legal — Plain English

> Read this before pointing OwnChart at your own health record, before
> connecting it to an EHR, and before recommending it to anyone else.
> OwnChart is alpha software. Self-hosted. Your decisions, your risks.

This page restates, in plain English, what's in the
[LICENSE](../LICENSE), the [SECURITY.md](../SECURITY.md) threat model,
and the [PRIVACY.md](../PRIVACY.md) policy. Where those documents
disagree with this one, those documents win.

## 1. OwnChart is not a doctor

OwnChart helps you understand evidence. It does not:

- Diagnose you.
- Tell you to start, stop, or change a medication.
- Tell you what to do in an emergency.
- Replace your clinician, your therapist, your pharmacist, your
  trainer, or your judgment.

AI features in OwnChart are a **research partner**, not a clinician.
They cite evidence, surface patterns, and answer questions. When they
don't have the evidence, they should say so. If they ever sound
authoritative in a way that conflicts with what your care team is
telling you, your care team is the answer, not OwnChart.

If you are in an acute medical situation, call your country's
emergency number. In the US that is **911**. If you are in mental
health crisis, in the US, dial or text **988** (Suicide & Crisis
Lifeline). OwnChart cannot triage you.

## 2. OwnChart is not HIPAA-protected by default

HIPAA applies to covered entities (clinicians, hospitals, insurers)
and to business associates who sign a BAA with them. **OwnChart, as
self-hosted patient software, is none of those.**

If you are a patient running OwnChart for your own record:

- HIPAA doesn't apply to your own data once it's on your own hardware
  in your own custody. You are not regulated; you are the patient.
- The protections you get are the ones you build: encrypted disk,
  strong password, a firewall you understand, off-host encrypted
  backups, an audit habit.

If you are a clinician or covered entity considering OwnChart as part
of a workflow that touches *patients other than yourself*:

- **This is commercial / institutional use under the [LICENSE](../LICENSE).**
  You need written permission from Nick Dawson.
- HIPAA obligations are yours. OwnChart cannot satisfy them on your
  behalf. You will need a BAA with whatever LLM provider you
  configure (Anthropic, OpenAI, etc.) — none of which is wired into
  OwnChart for you.
- The alpha is not the version of OwnChart you should be doing this
  with. Wait.

## 3. You are responsible for your deployment

This is what self-hosted means in practice. **You** decide:

- Where the server lives (your closet, your basement, a VPS you
  rent).
- Whose hardware it is.
- Who can reach it on the network ([NETWORK_ACCESS.md](./NETWORK_ACCESS.md)).
- What reverse proxy fronts it ([REVERSE_PROXY.md](./REVERSE_PROXY.md)).
- Whether the disk is encrypted.
- Whether anyone else has the password.
- Whether you back it up (and whether the backup is encrypted, and
  whether you've ever restored from it).
- Whether to ever expose it publicly.
- Whether to connect EHR vendors to it.
- Whether to send PHI to an LLM provider.

OwnChart cannot make any of those decisions for you. It can refuse to
do dangerous things without your explicit consent — the egress gate
exists for exactly that — but the operating posture is yours.

If you don't have a clear answer to every bullet above, slow down.
Read [SECURITY.md](../SECURITY.md). Run the demo first
([DEMO.md](./DEMO.md)) to see what the product is before standing up
your own.

## 4. Sending PHI to LLM providers — consent and provider terms

OwnChart's AI features use external LLM providers (Anthropic today;
OpenAI and local-model paths in progress; Gemini on the roadmap).

When you ask Ask, Event Intelligence, or any other AI surface a
question, **some portion of your record gets sent to whichever
provider you configured.** OwnChart's egress consent gate is the
load-bearing protection here:

- **Default-off.** No PHI leaves your host until you turn on LLM
  consent.
- **Scoped.** Per-source overrides can mark a source "never send to
  LLM" or "metadata-only."
- **Privacy modes.** `metadata_only` / `selected_evidence` /
  `full_source_allowed` constrain what categories of bytes can ship
  for a given call.
- **Audited.** Every call writes a `ModelRun` row that records exactly
  what was sent (by hash and source ID), to whom, in what mode.

Two things to be clear-eyed about:

1. **OwnChart cannot change the LLM provider's terms.** When you send
   bytes to Anthropic / OpenAI / Google / whoever, *their* privacy
   policy and *their* data-retention practices apply to those bytes.
   Read them. The fact that OwnChart is patient-owned does not buy
   you a BAA-equivalent on the provider side.
2. **Local-model paths are the only way to keep PHI strictly on your
   host.** Anthropic by default trains on consumer / free-tier inputs
   and doesn't on API. OpenAI's default API posture has changed over
   time. If "PHI does not leave my host, ever" is a hard requirement,
   the alpha is not the version for you — local-model first-class
   support is on the roadmap, not shipped.

## 5. What OwnChart *cannot* protect against

Stated up front so you don't assume coverage that isn't there:

- **Root on your host.** If an attacker has root on the box, they
  have your PHI. OwnChart is application software, not a kernel
  hypervisor.
- **A compromised laptop you're logged in from.** Session cookies are
  session cookies.
- **A subpoena to you personally.** You hold the keys; you can be
  compelled to produce them. Self-hosting is many things, including
  this thing.
- **A leaked API key.** If your `ANTHROPIC_API_KEY` is exfiltrated, an
  attacker can spend on your bill but cannot pull your PHI back
  through that key (calls flow host → API, not the reverse). Still:
  rotate.
- **A misconfigured reverse proxy.** If your proxy disables HTTPS or
  drops the auth cookie, OwnChart sees a request and trusts that
  it's authenticated. Your proxy is part of your trust boundary.

## 6. No warranty, no liability — and we mean it

The [LICENSE](../LICENSE) (PolyForm Noncommercial 1.0.0) says, in
[bold and capitals](../LICENSE#no-liability):

> **As far as the law allows, the software comes as is, without any
> warranty or condition, and the licensor will not be liable to you
> for any damages arising out of these terms or the use or nature of
> the software, under any kind of legal claim.**

Plain English:

- OwnChart may have bugs.
- OwnChart may extract a fact wrong.
- OwnChart may surface an inference that, read incorrectly, could
  affect a decision you make about your health.
- The model may hallucinate; the consent gate may have an edge case;
  the backup script may corrupt your evidence on a disk-full event;
  a future migration may delete a column you cared about.

You are not buying a clinical-grade product. You are using a
patient-owned research tool. Treat its outputs as inputs to your own
thinking, not as the final word on anything important. Where the
output points at a high-stakes decision (medication, procedure,
emergency), the answer is your clinician, not OwnChart.

By using OwnChart, you accept that no warranty applies, no liability
attaches, and you remain the responsible party for what you do with
the information.

## 7. Commercial use is gated

The alpha license is **PolyForm Noncommercial 1.0.0**. That means:

- **Allowed without asking:** personal use, household / family /
  caregiver use, educational use, noncommercial research,
  noncommercial-organization use (charity, public-health,
  public-research, government).
- **Not allowed without written permission:** resale, hosted
  commercial service, enterprise / employer / institutional
  deployment, data brokerage, derivative commercial product, or any
  use directed at commercial advantage.

If you are unsure whether your use is commercial, the rule of thumb:
**am I, my organization, or anyone I'm hosting for, deriving
monetary or commercial benefit from this software?** If yes, contact
Nick Dawson before deploying. Open a GitHub issue with the subject
"Commercial license inquiry."

## 8. Reporting a problem

- **Security issues:** open a private GitHub Security Advisory at
  <https://github.com/nickpdawson/OwnChart/security/advisories/new>.
  Do not file these as public issues.
- **Privacy concerns about the demo or the public site:** email the
  contact in [PRIVACY.md](../PRIVACY.md).
- **Bugs / feedback / questions:** GitHub issues.
- **A wrong fact in your own record that OwnChart surfaced:** that's
  what user-canonical correction is for — fix it in the app. The
  original source stays preserved.

## 9. The contract, restated

Reading the license is not optional. The plain-English version of the
deal is:

> OwnChart is free for you to use for yourself, for your household,
> for your education, for your research, and to share with other
> patients. It comes with no promises, no warranty, no clinician on
> staff, and no liability for anything that happens because of it.
> The author keeps the right to license it for commercial use
> separately. If you don't want this deal, don't use the software.

If that's a deal you can live with — read [INSTALL.md](./INSTALL.md)
and stand it up.
