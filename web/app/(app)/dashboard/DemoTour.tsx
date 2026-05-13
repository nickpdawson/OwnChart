"use client";

// Demo-only Home page module. When the instance is in demo_mode, this
// renders alongside the normal Home content with 3 curated questions
// that demonstrate OwnChart's research-partner moment over Camila's
// data. Each chip deep-links to /ask?q=… so the visitor experiences
// the actual AI flow rather than reading a static brochure.
//
// Server-side renders nothing when demo_mode is false — the parent
// passes a boolean and we early-return.

type Props = { demoMode: boolean };

// Tour questions curated against Camila Maria Lopez's Epic R4 sandbox
// patient bundle:
//   - June 2023 appendectomy (rich episode + perioperative cluster)
//   - June 2023 TTE Echo with "consistent with dilated cardiomyopathy"
//     conclusion text — meaningful research-partner moment
//   - PCOS diagnosed 2005 — the long-running through-line
const TOUR_QUESTIONS: { headline: string; q: string; why: string }[] = [
  {
    headline: "Walk me through her appendectomy and surrounding workup.",
    q: "Tell me the story of the appendectomy in June 2023 — what led up to it, what happened during the workup that week, and what showed up in the days around it.",
    why: "Episode Intelligence pulls the surgery, the same-day workup (X-ray, TTE Echo, pathology), and the post-op trajectory into one cited timeline.",
  },
  {
    headline: "What did the TTE Echo conclude?",
    q: "What did the transthoracic echocardiogram from June 2023 actually conclude? Is anything in the conclusion worth following up on?",
    why: "Demonstrates how OwnChart surfaces the meaningful interpretation text buried inside a DiagnosticReport — including the radiologist's note about dilated cardiomyopathy.",
  },
  {
    headline: "What's the longest-running concern in this record?",
    q: "Looking across the entire record, what's been going on the longest? What does the record say about it over time?",
    why: "Tests OwnChart's ability to spot through-lines (PCOS since 2005) versus recent events — research-partner pattern recognition, not record summarization.",
  },
];

export function DemoTour({ demoMode }: Props) {
  if (!demoMode) return null;

  return (
    <section className="mt-8 rounded-xl border border-evidence/30 bg-evidence/5 p-5">
      <p className="text-xs uppercase tracking-widest text-evidence">
        Take the tour
      </p>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        Three questions that demonstrate what OwnChart does. Click any
        chip to open it in Ask — every answer cites the underlying facts.
      </p>
      <ul className="mt-4 space-y-3">
        {TOUR_QUESTIONS.map((t, i) => (
          <li
            key={i}
            className="rounded-lg border border-evidence/20 bg-surface p-3"
          >
            <a
              href={`/ask?q=${encodeURIComponent(t.q)}`}
              className="font-medium text-ink underline-offset-4 hover:underline"
            >
              {t.headline}
            </a>
            <p className="mt-1 text-xs text-muted">{t.why}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
