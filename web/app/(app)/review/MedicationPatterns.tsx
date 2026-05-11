"use client";

// docs/08 Review Queue Triage — pattern-level medication AND
// provider/contact candidates. Accepting a pattern flips its member
// facts to review_state='deferred' and writes a
// pattern_managed_suppression audit pointer back to the candidate.
// Dismissing leaves the members in their current state.

import { useEffect, useState } from "react";
import type { SensemakingCandidate } from "@/lib/api";

type PatternCategory = "medication" | "provider";

export function MedicationPatterns() {
  const [medPatterns, setMedPatterns] = useState<SensemakingCandidate[]>([]);
  const [providerPatterns, setProviderPatterns] = useState<
    SensemakingCandidate[]
  >([]);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, []);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      // No "list pending pattern candidates" route yet, so trigger
      // both triages. Each is deterministic + idempotent (skips
      // pattern_keys already in pending candidates), so re-running
      // on every page render is safe and cheap.
      const [medR, provR] = await Promise.all([
        fetch(
          `/api/sensemaking/review/medication-patterns?min_group_size=5`,
          { method: "POST", credentials: "include" },
        ),
        fetch(
          `/api/sensemaking/review/provider-patterns?min_group_size=3`,
          { method: "POST", credentials: "include" },
        ),
      ]);
      if (!medR.ok) throw new Error(await medR.text());
      if (!provR.ok) throw new Error(await provR.text());
      const medJob = (await medR.json()) as { candidates: SensemakingCandidate[] };
      const provJob = (await provR.json()) as { candidates: SensemakingCandidate[] };
      setMedPatterns(
        (medJob.candidates ?? []).filter(
          (c) =>
            c.candidate_type === "medication_pattern" &&
            c.disposition === "pending",
        ),
      );
      setProviderPatterns(
        (provJob.candidates ?? []).filter(
          (c) =>
            c.candidate_type === "provider_pattern" &&
            c.disposition === "pending",
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function patch(
    id: string,
    disposition: "accepted" | "dismissed",
    category: PatternCategory,
  ) {
    setActing(id);
    setError(null);
    try {
      const r = await fetch(`/api/sensemaking/candidates/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ disposition }),
      });
      if (!r.ok) throw new Error(await r.text());
      if (category === "medication") {
        setMedPatterns((prev) => prev.filter((p) => p.id !== id));
      } else {
        setProviderPatterns((prev) => prev.filter((p) => p.id !== id));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setActing(null);
    }
  }

  if (loading) {
    return <p className="mt-6 text-sm text-muted">Loading patterns…</p>;
  }
  if (medPatterns.length === 0 && providerPatterns.length === 0 && !error) {
    return null;
  }

  return (
    <div className="mt-6 space-y-5">
      {error && (
        <p className="rounded-md border border-caution/30 bg-caution/10 p-3 text-sm text-caution">
          Couldn&apos;t load patterns: {error}
        </p>
      )}
      {medPatterns.length > 0 && (
        <PatternSection
          title="Medication patterns"
          patterns={medPatterns}
          category="medication"
          onPatch={patch}
          acting={acting}
          onRefresh={load}
        />
      )}
      {providerPatterns.length > 0 && (
        <PatternSection
          title="Provider / contact patterns"
          patterns={providerPatterns}
          category="provider"
          onPatch={patch}
          acting={acting}
          onRefresh={load}
        />
      )}
    </div>
  );
}

function PatternSection({
  title,
  patterns,
  category,
  onPatch,
  acting,
  onRefresh,
}: {
  title: string;
  patterns: SensemakingCandidate[];
  category: PatternCategory;
  onPatch: (
    id: string,
    disposition: "accepted" | "dismissed",
    category: PatternCategory,
  ) => Promise<void>;
  acting: string | null;
  onRefresh: () => Promise<void>;
}) {
  return (
    <section className="rounded-xl border border-evidence/30 bg-evidence/5 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-xs uppercase tracking-widest text-evidence">
          {title}
        </p>
        <button
          type="button"
          onClick={onRefresh}
          className="text-xs text-muted underline-offset-4 hover:underline"
        >
          Refresh
        </button>
      </div>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        Accept a pattern to collapse N individual entries into one
        decision. Members get marked <code>deferred</code> with an audit
        pointer back to this pattern.
      </p>
      <ul className="mt-3 space-y-2">
        {patterns.map((p) => (
          <li
            key={p.id}
            className="rounded-lg border border-muted/15 bg-surface p-3"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="font-medium">{p.title ?? "(untitled pattern)"}</p>
              <p className="text-xs text-muted">
                {p.fact_ids.length} member{p.fact_ids.length === 1 ? "" : "s"}
              </p>
            </div>
            {p.summary_text && (
              <p className="mt-1 text-sm text-muted">{p.summary_text}</p>
            )}
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                type="button"
                disabled={acting === p.id}
                onClick={() => onPatch(p.id, "accepted", category)}
                className="rounded-md bg-accent px-3 py-1 text-xs text-surface hover:opacity-90 disabled:opacity-50"
              >
                {acting === p.id ? "…" : "Accept (suppress members)"}
              </button>
              <button
                type="button"
                disabled={acting === p.id}
                onClick={() => onPatch(p.id, "dismissed", category)}
                className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
              >
                Dismiss
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
