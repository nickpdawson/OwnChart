"use client";

import { useEffect, useState } from "react";
import { BriefThread } from "./BriefThread";

type Brief = {
  topic_slug: string;
  brief_id: string | null;
  model_run_id: string | null;
  prompt_version: string | null;
  generated_at: string | null;
  error: string | null;
  narrative: string | null;
  well_supported: { statement: string; fact_ids: string[] }[];
  uncertain: { statement: string; why_uncertain?: string; fact_ids?: string[] }[];
  suggested_questions: string[];
  citations: { fact_id: string; note?: string }[];
  safety_response: string | null;
};

function fmtWhen(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function ExecBriefPanel({ slug }: { slug: string }) {
  const [brief, setBrief] = useState<Brief | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);

  // Hydrate the latest persisted brief on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/topics/${slug}/brief`, {
          credentials: "include",
          cache: "no-store",
        });
        if (!cancelled && r.ok) {
          const out = (await r.json()) as Brief | null;
          if (out) setBrief(out);
        }
      } catch {
        /* ignore */
      } finally {
        if (!cancelled) setHydrated(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [slug]);

  async function run() {
    if (busy) return;
    if (
      brief &&
      !confirm(
        "Regenerate the brief? This sends your dossier facts to Claude and incurs ~$0.10–0.30 in API spend.",
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`/api/topics/${slug}/brief`, {
        method: "POST",
        credentials: "include",
      });
      if (r.status === 412) {
        setError("Enable global LLM consent first (top-right consent toggle).");
        return;
      }
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j?.detail ? ` — ${j.detail}` : "";
        } catch {
          /* ignore */
        }
        setError(`Brief generation failed (HTTP ${r.status})${detail}`);
        return;
      }
      const out = (await r.json()) as Brief;
      setBrief(out);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-8 rounded-xl border border-muted/15 bg-surface p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-serif text-xl">Executive brief</h2>
          {brief?.generated_at && (
            <p className="mt-0.5 text-xs text-muted">
              Last generated {fmtWhen(brief.generated_at)}
              {brief.prompt_version ? ` · ${brief.prompt_version}` : ""}
            </p>
          )}
        </div>
        <button
          onClick={run}
          disabled={busy || !hydrated}
          className="rounded-lg bg-accent px-3 py-1.5 text-sm text-surface disabled:opacity-50"
        >
          {busy ? "Generating…" : brief ? "Regenerate" : "Generate"}
        </button>
      </div>

      {error && <p className="mt-3 text-sm text-caution">{error}</p>}

      {brief?.safety_response && (
        <p className="mt-3 rounded-md bg-caution/10 p-3 text-sm">{brief.safety_response}</p>
      )}

      {brief && !brief.safety_response && (
        <div className="mt-4 space-y-5">
          {brief.error && (
            <p className="rounded-md bg-caution/10 p-3 text-sm text-caution">
              LLM error — {brief.error}
            </p>
          )}
          {brief.narrative && (
            <div>
              <p className="whitespace-pre-line text-base leading-relaxed">{brief.narrative}</p>
            </div>
          )}
          {brief.well_supported.length > 0 && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-widest text-muted">Well supported</p>
              <ul className="mt-2 space-y-1.5 text-sm">
                {brief.well_supported.map((w, i) => (
                  <li key={i}>
                    <span>{w.statement}</span>
                    {w.fact_ids?.length > 0 && (
                      <span className="ml-2 text-xs">
                        {w.fact_ids.map((fid, j) => (
                          <a
                            key={fid + j}
                            href={`#fact-${fid}`}
                            title="Jump to this fact below"
                            className="mr-1 inline-block rounded border border-muted/20 px-1 py-0.5 font-mono text-muted hover:border-accent/40 hover:text-ink"
                          >
                            {fid.slice(0, 8)}…
                          </a>
                        ))}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {brief.uncertain.length > 0 && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-widest text-muted">Uncertain</p>
              <ul className="mt-2 space-y-1.5 text-sm">
                {brief.uncertain.map((u, i) => (
                  <li key={i}>
                    <span>{u.statement}</span>
                    {u.why_uncertain && <span className="ml-1 text-muted">— {u.why_uncertain}</span>}
                    {u.fact_ids && u.fact_ids.length > 0 && (
                      <span className="ml-2 text-xs">
                        {u.fact_ids.map((fid, j) => (
                          <a
                            key={fid + j}
                            href={`#fact-${fid}`}
                            title="Jump to this fact below"
                            className="mr-1 inline-block rounded border border-muted/20 px-1 py-0.5 font-mono text-muted hover:border-accent/40 hover:text-ink"
                          >
                            {fid.slice(0, 8)}…
                          </a>
                        ))}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {brief.suggested_questions.length > 0 && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-widest text-muted">Useful next questions</p>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
                {brief.suggested_questions.map((q, i) => (
                  <li key={i}>{q}</li>
                ))}
              </ul>
            </div>
          )}
          {brief.model_run_id && (
            <p className="text-xs text-muted">
              Audit: model_run_id {brief.model_run_id.slice(0, 8)}…
            </p>
          )}
        </div>
      )}

      {!brief && !error && !busy && hydrated && (
        <p className="mt-3 text-sm text-muted">
          The brief is generated by Claude using only this dossier&apos;s facts as context. Cited and reviewable. Briefs are saved so refreshing the page won&apos;t lose them; click Regenerate to refresh after new evidence lands.
        </p>
      )}

      {/* Continue thinking with the dossier — threaded follow-up. The
          brief sets the starting point; the thread is where the
          research-partner conversation actually happens. */}
      {brief && !brief.safety_response && (
        <BriefThread slug={brief.topic_slug} suggestedQuestions={brief.suggested_questions || []} />
      )}
    </section>
  );
}
