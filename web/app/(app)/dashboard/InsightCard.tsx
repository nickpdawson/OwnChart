"use client";

import { useState } from "react";

// Client component so the manual "Make sense of what changed" button
// can re-fetch the insight without a full Home reload. Initial render
// uses the server-fetched insight from getHomeAiPartner(); the button
// hits POST /api/home/insight/refresh which bypasses the daily cache.

type Insight = {
  body: string;
  question: string | null;
  related_fact_ids: string[];
  generated_at: string;
};

export function InsightCard({ initial }: { initial: Insight | null }) {
  const [insight, setInsight] = useState<Insight | null>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch("/api/home/insight/refresh", {
        method: "POST",
        credentials: "include",
      });
      if (!r.ok) {
        if (r.status === 422) {
          setError("Not enough recent activity to observe a meaningful change yet.");
        } else {
          setError(`Refresh failed (HTTP ${r.status})`);
        }
        return;
      }
      const fresh = (await r.json()) as Insight;
      setInsight(fresh);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!insight) {
    // No initial insight from the server (sparse record). Still allow
    // a manual try — gives the user a way to invite the system to look
    // again after they've added new data.
    return (
      <section className="mt-8 rounded-xl border border-muted/15 bg-surface p-5">
        <p className="text-xs uppercase tracking-widest text-muted">
          Something I noticed
        </p>
        <p className="mt-2 text-sm text-muted">
          Not enough recent activity yet to surface a meaningful
          observation. Add a source, sync HealthKit, or ask a question
          to give the system something to work with.
        </p>
        <button
          type="button"
          onClick={refresh}
          disabled={busy}
          className="mt-3 rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5 disabled:opacity-50"
        >
          {busy ? "Looking…" : "Make sense of what changed"}
        </button>
        {error && <p className="mt-2 text-xs text-caution">{error}</p>}
      </section>
    );
  }

  return (
    <section className="mt-8 rounded-xl border border-accent/30 bg-accent/5 p-5">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-xs uppercase tracking-widest text-accent">
          Something I noticed
        </p>
        <button
          type="button"
          onClick={refresh}
          disabled={busy}
          className="text-xs text-muted underline-offset-4 hover:underline disabled:opacity-50"
          title="Bypass the daily cache and ask the model to look at what's changed since the last refresh."
        >
          {busy ? "Looking…" : "Make sense of what changed"}
        </button>
      </div>
      <p className="mt-2 font-serif text-base leading-relaxed text-ink">
        {insight.body}
      </p>
      {insight.question && (
        <a
          href={`/ask?q=${encodeURIComponent(insight.question)}`}
          className="mt-3 inline-block rounded-md border border-accent/40 px-3 py-1 text-sm text-accent hover:bg-accent/10"
        >
          {insight.question}
        </a>
      )}
      <p className="mt-3 text-[10px] uppercase tracking-widest text-muted">
        Generated{" "}
        {new Date(insight.generated_at).toLocaleDateString(undefined, {
          month: "short", day: "numeric",
        })}
        {" · "}refreshes daily
      </p>
      {error && <p className="mt-2 text-xs text-caution">{error}</p>}
    </section>
  );
}
