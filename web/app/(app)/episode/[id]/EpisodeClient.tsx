"use client";

import { useState } from "react";
import type { EpisodeDetail, EpisodeMember } from "@/lib/api";

const SIGNIFICANCE_CHOICES = [
  "major_event",
  "major_procedure",
  "major_diagnosis",
  "major_medication",
  "major_activity_lifestyle",
  "background",
  "source_only",
] as const;

const SIGNIFICANCE_LABEL: Record<string, string> = {
  major_event: "Major event",
  major_procedure: "Major procedure",
  major_diagnosis: "Major diagnosis",
  major_medication: "Major medication",
  major_activity_lifestyle: "Major activity / lifestyle",
  background: "Background",
  source_only: "Source-only",
};

export function EpisodeClient({ episode }: { episode: EpisodeDetail }) {
  const [ep, setEp] = useState(episode);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const intelligence = (ep.payload?.intelligence ?? null) as
    | Record<string, unknown>
    | null;
  const followUps = (ep.payload?.follow_up_questions ?? []) as string[];

  // Q-A1 loud low-confidence: when the Episode Intelligence planner
  // had to fall back to "most recent major procedure" (or otherwise
  // wasn't sure which event you meant), the planner stamps the
  // candidate payload with match_confidence='low'. Show that loudly
  // so the user knows the answer is anchored on a best-guess match.
  const plannerAnchor = (
    (ep.payload?.planner as Record<string, unknown> | undefined)?.anchor ??
    null
  ) as Record<string, unknown> | null;
  const matchConfidence =
    typeof plannerAnchor?.match_confidence === "string"
      ? plannerAnchor.match_confidence
      : null;
  const matchExplanation =
    typeof plannerAnchor?.match_explanation === "string"
      ? plannerAnchor.match_explanation
      : null;

  async function markSignificance(s: string) {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`/api/episodes/${encodeURIComponent(ep.id)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ significance: s }),
      });
      if (!r.ok) throw new Error(await r.text());
      const next = (await r.json()) as EpisodeDetail;
      setEp(next);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {/* Loud low-confidence banner (Q-A1). Renders when the planner
          flagged the anchor match as 'low'. Caution-toned so the
          user can't miss that the entire answer is built on a guess. */}
      {matchConfidence === "low" && (
        <section className="mt-6 rounded-xl border-2 border-caution/50 bg-caution/10 p-4">
          <p className="text-xs uppercase tracking-widest text-caution">
            ⚠ Low-confidence match
          </p>
          <p className="mt-1 text-sm">
            {matchExplanation ??
              "OwnChart fell back to the most recent major procedure on your record. The whole answer below is anchored on that guess — verify it's the right event before trusting the narrative."}
          </p>
        </section>
      )}

      {/* Significance controls — applied to primary_fact_id so every
          ranking surface (Home, Timeline, Discover, FactContext)
          picks it up. */}
      <section className="mt-6 rounded-xl border border-muted/15 bg-bg/40 p-4">
        <p className="text-xs uppercase tracking-widest text-muted">
          Mark this episode
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {SIGNIFICANCE_CHOICES.map((s) => (
            <button
              key={s}
              type="button"
              disabled={busy}
              onClick={() => markSignificance(s)}
              className="rounded-md border border-muted/30 px-2 py-0.5 text-xs hover:bg-muted/5 disabled:opacity-50"
            >
              {SIGNIFICANCE_LABEL[s] ?? s}
            </button>
          ))}
        </div>
        {error && (
          <p className="mt-2 text-xs text-caution">Couldn&apos;t update: {error}</p>
        )}
      </section>

      {/* Intelligence sections — rendered from the structured payload
          if present. Empty payloads (manually created episodes) skip
          this block entirely. */}
      {intelligence && <IntelligenceSections payload={intelligence} />}

      {/* Members — facts/sources/candidates that belong to this episode. */}
      {ep.members.length > 0 && (
        <section className="mt-8">
          <h2 className="font-serif text-xl">Members ({ep.members.length})</h2>
          <ul className="mt-2 divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {ep.members.map((m) => (
              <MemberRow key={m.id} m={m} />
            ))}
          </ul>
        </section>
      )}

      {followUps.length > 0 && (
        <section className="mt-8">
          <h2 className="font-serif text-xl">Follow-up questions</h2>
          <ul className="mt-3 grid gap-2 sm:grid-cols-2">
            {followUps.map((q, i) => (
              <li key={i}>
                <a
                  href={`/ask?q=${encodeURIComponent(q)}`}
                  className="block rounded-xl border border-muted/15 bg-surface p-3 text-sm hover:border-accent/40"
                >
                  {q}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function IntelligenceSections({
  payload,
}: {
  payload: Record<string, unknown>;
}) {
  const sections: { key: string; label: string }[] = [
    { key: "anchor_acknowledgment", label: "Anchor" },
    { key: "what_happened", label: "What happened" },
    { key: "what_they_did", label: "What they did" },
    { key: "anesthesia", label: "Anesthesia & intraoperative meds" },
    { key: "travel_and_life", label: "Travel & life context" },
    { key: "body_response", label: "Body response" },
    { key: "interpretation", label: "Interpretation" },
  ];
  return (
    <section className="mt-8 space-y-5">
      {sections.map(({ key, label }) => {
        const value = payload[key];
        const text =
          typeof value === "string"
            ? value
            : value && typeof value === "object"
              ? String(
                  (value as { summary?: string; translation?: string }).summary ??
                    (value as { translation?: string }).translation ??
                    "",
                )
              : "";
        if (!text) return null;
        return (
          <div key={key}>
            <h3 className="text-xs uppercase tracking-widest text-muted">
              {label}
            </h3>
            <p className="mt-1 whitespace-pre-wrap font-serif text-base leading-relaxed text-ink">
              {text}
            </p>
          </div>
        );
      })}
    </section>
  );
}

function MemberRow({ m }: { m: EpisodeMember }) {
  let href: string | null = null;
  if (m.member_type === "fact") href = `?fact=${encodeURIComponent(m.subject_id)}`;
  else if (m.member_type === "source") href = `/sources/${m.subject_id}`;
  else if (m.member_type === "conversation") href = `/chat/${m.subject_id}`;
  const body = (
    <div className="flex flex-wrap items-baseline gap-2 px-4 py-3 text-sm">
      <span className="text-[10px] uppercase tracking-widest text-muted">
        {m.member_type}
      </span>
      <span className="font-medium">{m.role}</span>
      <span className="ml-auto font-mono text-xs text-muted">
        {m.subject_id.slice(0, 8)}…
      </span>
    </div>
  );
  return (
    <li>
      {href ? (
        <a href={href} className="block hover:bg-muted/5">
          {body}
        </a>
      ) : (
        body
      )}
    </li>
  );
}
