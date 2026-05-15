"use client";

// docs/08 Review Queue Triage — pattern-level medication AND
// provider/contact candidates.
//
// 2026-05-15 (Nick's correction): accepting a pattern flips member
// facts to `pattern_managed`, NOT `deferred`. The facts remain
// available to Ask / Event Intelligence / Timeline / Dossiers — they
// just stop showing up in the Review Inbox as individual decisions.
// "Accept" = "I've reviewed this pattern, treat it as known signal"
// rather than "delete." Dismissing leaves members untouched.

import { useEffect, useState } from "react";
import type { SensemakingCandidate } from "@/lib/api";

// Shape of the medication-pattern payload (built in
// api/ownchart/llm/medication_triage.py). Provider patterns ship a
// subset (no adherence / dose / cluster). All fields optional so
// older candidates render gracefully.
type MedPatternPayload = {
  pattern_key?: string;
  label_examples?: string[];
  fact_type?: string;
  total_entries?: number;
  taken_count?: number;
  skipped_count?: number;
  unknown_count?: number;
  needs_review_count?: number;
  date_min?: string | null;
  date_max?: string | null;
  active_days?: number;
  active_months?: number;
  entries_per_active_month?: number | null;
  dose_examples?: string[];
  clustered_window?: {
    start: string;
    end: string;
    days_in_window: number;
    share_of_active_days: number;
  } | null;
};

function fmtDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

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
          `/api/review/medication-patterns?min_group_size=5`,
          { method: "POST", credentials: "include" },
        ),
        fetch(
          `/api/review/provider-patterns?min_group_size=3`,
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
        Accept a pattern to mark its log entries as reviewed. The
        entries stay available for timelines, Ask, Event
        Intelligence, and adherence analysis — they just stop
        showing up as individual review-inbox decisions.
      </p>
      <ul className="mt-3 space-y-2">
        {patterns.map((p) => (
          <PatternCard
            key={p.id}
            pattern={p}
            category={category}
            acting={acting === p.id}
            onPatch={onPatch}
          />
        ))}
      </ul>
    </section>
  );
}

function PatternCard({
  pattern,
  category,
  acting,
  onPatch,
}: {
  pattern: SensemakingCandidate;
  category: PatternCategory;
  acting: boolean;
  onPatch: (
    id: string,
    disposition: "accepted" | "dismissed",
    category: PatternCategory,
  ) => Promise<void>;
}) {
  const payload = (pattern.payload || {}) as MedPatternPayload;
  const isMedication = category === "medication";

  const totalEntries = payload.total_entries ?? pattern.fact_ids.length;
  const taken = payload.taken_count ?? 0;
  const skipped = payload.skipped_count ?? 0;
  const unknown = payload.unknown_count
    ?? Math.max(0, totalEntries - taken - skipped);
  const dateMinShort = payload.date_min
    ? new Date(payload.date_min).toLocaleDateString(undefined, { month: "short", year: "numeric" })
    : null;
  const dateMaxShort = payload.date_max
    ? new Date(payload.date_max).toLocaleDateString(undefined, { month: "short", year: "numeric" })
    : null;
  const mostRecent = fmtDate(payload.date_max);

  return (
    <li className="rounded-lg border border-muted/15 bg-surface p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-medium">{pattern.title ?? "(untitled pattern)"}</p>
        <p className="text-xs text-muted">
          {totalEntries} log entr{totalEntries === 1 ? "y" : "ies"}
          {dateMinShort && dateMaxShort && (
            <> from {dateMinShort} to {dateMaxShort}</>
          )}
        </p>
      </div>

      {isMedication && (taken > 0 || skipped > 0 || unknown > 0) && (
        <p className="mt-1 text-sm">
          {taken > 0 && <span><strong>{taken}</strong> taken</span>}
          {taken > 0 && (skipped > 0 || unknown > 0) && ", "}
          {skipped > 0 && <span><strong>{skipped}</strong> skipped</span>}
          {skipped > 0 && unknown > 0 && ", "}
          {unknown > 0 && (
            <span>
              <strong>{unknown}</strong> unknown
              <span className="ml-1 text-xs text-muted">(no adherence tag)</span>
            </span>
          )}
        </p>
      )}

      {isMedication && (
        <p className="mt-1 text-xs text-muted">
          {payload.active_days != null && (
            <>used on <strong className="text-ink">{payload.active_days}</strong> active day{payload.active_days === 1 ? "" : "s"}</>
          )}
          {payload.active_days != null && payload.entries_per_active_month != null && " · "}
          {payload.entries_per_active_month != null && (
            <>~{payload.entries_per_active_month} entries / active month</>
          )}
          {mostRecent && (payload.active_days != null || payload.entries_per_active_month != null) && " · "}
          {mostRecent && <>most recent {mostRecent}</>}
        </p>
      )}

      {isMedication && payload.dose_examples && payload.dose_examples.length > 0 && (
        <p className="mt-1 text-xs text-muted">
          Doses logged: {payload.dose_examples.join(" · ")}
        </p>
      )}

      {isMedication && payload.clustered_window && (
        <p className="mt-2 rounded-md bg-caution/10 px-2 py-1 text-xs text-caution">
          <strong>Clustered use:</strong>{" "}
          {Math.round(payload.clustered_window.share_of_active_days * 100)}% of
          active days fall between{" "}
          {fmtDate(payload.clustered_window.start)} and{" "}
          {fmtDate(payload.clustered_window.end)} — worth eyeballing before
          marking reviewed.
        </p>
      )}

      {!isMedication && pattern.summary_text && (
        <p className="mt-1 text-sm text-muted">{pattern.summary_text}</p>
      )}

      <p className="mt-2 text-xs text-muted/80">
        Accepting marks these entries as reviewed; they remain
        available for timelines and analysis.
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={acting}
          onClick={() => onPatch(pattern.id, "accepted", category)}
          className="rounded-md bg-accent px-3 py-1 text-xs text-surface hover:opacity-90 disabled:opacity-50"
        >
          {acting ? "…" : "Mark reviewed"}
        </button>
        <button
          type="button"
          disabled={acting}
          onClick={() => onPatch(pattern.id, "dismissed", category)}
          className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Dismiss (keep in review)
        </button>
      </div>
    </li>
  );
}
