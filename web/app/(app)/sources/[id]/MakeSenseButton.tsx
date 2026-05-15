"use client";

// docs/08 — "Make sense of this source." The user-initiated entry
// point into LLM sensemaking. Posts to
// /api/sources/{id}/sensemake, then renders the resulting
// SensemakingCandidate(s) inline as a draft the user can dismiss.
// Promotion (creating Episode rows, marking source-only) lives in
// future iterations; V1 shows the draft and audits the disposition.

import { useState } from "react";
import type { SensemakingCandidate, SensemakingJob } from "@/lib/api";

type Props = {
  sourceId: string;
  initialCandidates?: SensemakingCandidate[];
};

export function MakeSenseButton({ sourceId, initialCandidates = [] }: Props) {
  const [job, setJob] = useState<SensemakingJob | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<SensemakingCandidate[]>(
    initialCandidates,
  );

  async function run() {
    setRunning(true);
    setError(null);
    try {
      const r = await fetch(
        `/api/sources/${encodeURIComponent(sourceId)}/sensemake`,
        {
          method: "POST",
          credentials: "include",
        },
      );
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      const out = (await r.json()) as SensemakingJob;
      setJob(out);
      setCandidates(out.candidates);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  async function patchDisposition(
    id: string,
    disposition: "accepted" | "dismissed",
  ) {
    try {
      const r = await fetch(
        `/api/sensemaking/candidates/${encodeURIComponent(id)}`,
        {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ disposition }),
        },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const updated = (await r.json()) as SensemakingCandidate;
      setCandidates((prev) =>
        prev.map((c) => (c.id === updated.id ? updated : c)),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const summary = candidates.find((c) => c.candidate_type === "source_summary");
  const episodes = candidates.filter((c) => c.candidate_type === "episode");

  // Job-level status messaging — only render after a run completes.
  const refused = job?.status === "refused";
  const failed = job?.status === "failed";

  return (
    <section className="mt-6 rounded-xl border border-accent/30 bg-accent/5 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-widest text-accent">
            LLM sensemaking
          </p>
          <h2 className="mt-1 font-serif text-xl">
            What this source means for your record
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Ask OwnChart to draft a patient-readable summary, propose
            episode candidates, and call out scaffolding noise. The
            draft never overwrites anything — accept, edit, or dismiss.
          </p>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={running}
          className="rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
        >
          {running
            ? "Thinking…"
            : candidates.length > 0
              ? "Re-run sensemaking"
              : "Make sense of this source"}
        </button>
      </div>

      {error && (
        <p className="mt-4 rounded-md border border-caution/30 bg-caution/10 p-3 text-sm text-caution">
          Couldn&apos;t run sensemaking: {error}
        </p>
      )}

      {refused && (
        <p className="mt-4 rounded-md border border-caution/30 bg-caution/10 p-3 text-sm">
          Refused: {job?.error ?? "unknown reason"}. Check the Privacy
          and AI settings if this surprised you.
        </p>
      )}

      {failed && (
        <p className="mt-4 rounded-md border border-caution/30 bg-caution/10 p-3 text-sm">
          The LLM call failed: {job?.error ?? "unknown error"}. Try
          again, or check the Audit log for the matching model run.
        </p>
      )}

      {summary && (
        <article className="mt-4 rounded-lg border border-muted/15 bg-surface p-4">
          <p className="text-[10px] uppercase tracking-widest text-muted">
            {summary.disposition === "pending"
              ? "Draft"
              : summary.disposition}
            {summary.claim_label && (
              <> · {summary.claim_label.replace(/_/g, " ")}</>
            )}
          </p>
          <p className="mt-1 font-serif text-base leading-relaxed text-ink">
            {summary.summary_text}
          </p>
          {summary.payload.source_only_recommendation ? (
            <p className="mt-3 text-sm text-muted">
              <strong className="text-ink">Source-only recommendation:</strong>{" "}
              {String(summary.payload.source_only_recommendation)}
            </p>
          ) : null}
          {Array.isArray(summary.payload.suggested_questions) &&
            summary.payload.suggested_questions.length > 0 && (
              <div className="mt-4">
                <p className="text-xs uppercase tracking-widest text-muted">
                  Questions worth asking
                </p>
                <ul className="mt-2 space-y-1 text-sm">
                  {(summary.payload.suggested_questions as string[]).map(
                    (q, i) => (
                      <li key={i} className="flex items-baseline gap-2">
                        <span className="text-accent">·</span>
                        <a
                          href={`/ask?q=${encodeURIComponent(q)}`}
                          className="hover:underline"
                        >
                          {q}
                        </a>
                      </li>
                    ),
                  )}
                </ul>
              </div>
            )}
          {summary.disposition === "pending" && (
            <div className="mt-4 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => patchDisposition(summary.id, "accepted")}
                className="rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90"
              >
                Accept
              </button>
              <button
                type="button"
                onClick={() => patchDisposition(summary.id, "dismissed")}
                className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5"
              >
                Dismiss
              </button>
            </div>
          )}
        </article>
      )}

      {episodes.length > 0 && (
        <div className="mt-4">
          <p className="text-xs uppercase tracking-widest text-muted">
            Event candidates
          </p>
          <ul className="mt-2 space-y-2">
            {episodes.map((e) => (
              <li
                key={e.id}
                className="rounded-lg border border-muted/15 bg-surface p-3"
              >
                <p className="font-medium">{e.title}</p>
                {e.summary_text && (
                  <p className="mt-1 text-sm text-muted">{e.summary_text}</p>
                )}
                <p className="mt-1 text-xs text-muted">
                  {e.fact_ids.length} related fact
                  {e.fact_ids.length === 1 ? "" : "s"}
                </p>
                {e.disposition === "pending" && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => patchDisposition(e.id, "accepted")}
                      className="rounded-md border border-accent/40 px-2.5 py-1 text-xs text-accent hover:bg-accent/10"
                    >
                      Save as Event
                    </button>
                    <button
                      type="button"
                      onClick={() => patchDisposition(e.id, "dismissed")}
                      className="rounded-md border border-muted/30 px-2.5 py-1 text-xs hover:bg-muted/5"
                    >
                      Dismiss
                    </button>
                  </div>
                )}
                {e.disposition !== "pending" && (
                  <p className="mt-1 text-xs italic text-muted">
                    {e.disposition}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {job?.model_run_id && (
        <p className="mt-4 text-[10px] uppercase tracking-widest text-muted">
          ModelRun: <span className="font-mono">{job.model_run_id}</span> ·
          privacy mode: {job.privacy_mode.replace(/_/g, " ")}
        </p>
      )}
    </section>
  );
}
