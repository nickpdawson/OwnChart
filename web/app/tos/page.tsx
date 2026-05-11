// Public Terms of Service page. Required by Epic's app registration form
// (https://fhir.epic.com/Developer/Apps → Terms and Conditions Secure URL).
// Intentionally outside the (app)/ route group — no auth, no consent gate.

export const dynamic = "force-static";

export const metadata = {
  title: "OwnChart — Terms of Service",
  description: "Terms governing use of OwnChart's patient-owned health record platform.",
  robots: { index: true, follow: true },
};

export default function TermsOfServicePage() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-16">
      <p className="text-sm uppercase tracking-widest text-muted">OwnChart</p>
      <h1 className="mt-2 font-serif text-3xl">Terms of Service</h1>
      <p className="mt-2 text-sm text-muted">Last updated 2026-05-09.</p>

      <section className="mt-8 space-y-5 text-base leading-relaxed">
        <p>
          OwnChart is a self-hosted, patient-owned health record platform. The
          person operating an OwnChart instance is the same person whose
          health information OwnChart holds. There is no third-party operator,
          no SaaS provider, and no data broker between the patient and their
          own records.
        </p>

        <h2 className="font-serif text-xl">What OwnChart does</h2>
        <p>
          With the patient&apos;s explicit consent, OwnChart connects to
          provider patient portals via SMART on FHIR, retrieves the
          patient&apos;s own clinical data, stores it on infrastructure the
          patient controls, and helps the patient understand their own
          longitudinal record.
        </p>

        <h2 className="font-serif text-xl">What OwnChart is not</h2>
        <ul className="list-disc space-y-1 pl-6">
          <li>Not a medical device. Not a diagnostic tool.</li>
          <li>Not a substitute for clinical care.</li>
          <li>Not a service operated by a third party.</li>
          <li>Not an entity that resells, licenses, or shares your data.</li>
        </ul>

        <h2 className="font-serif text-xl">AI use</h2>
        <p>
          OwnChart can use a large-language-model provider to translate
          clinical language, extract structured facts, and answer
          natural-language questions. AI features are gated by an explicit
          per-instance consent toggle. The LLM is treated as a research
          partner — it cites the source documents it draws from, marks
          uncertainty, and never gives treatment instructions.
        </p>

        <h2 className="font-serif text-xl">Data ownership</h2>
        <p>
          The patient owns the data. OwnChart preserves original source
          records unchanged. Patient corrections create a canonical layer
          alongside the original; nothing is overwritten.
        </p>

        <h2 className="font-serif text-xl">No warranty</h2>
        <p>
          OwnChart is provided as-is. Use of OwnChart in clinical
          decision-making is at the patient&apos;s sole discretion and
          responsibility.
        </p>

        <h2 className="font-serif text-xl">Contact</h2>
        <p>
          Operator of this instance: see <code>/dashboard</code> when signed
          in. For Epic app-registration questions: register your own
          instance at <code>fhir.epic.com/Developer/Apps</code>.
        </p>
      </section>
    </main>
  );
}
