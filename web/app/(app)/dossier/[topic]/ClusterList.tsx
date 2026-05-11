"use client";

import { useState } from "react";
import type { FactCluster, FactReadout } from "@/lib/api";

type Props = {
  slug: string;
  clusters: FactCluster[];
};

const TYPE_GROUPS: { key: string; label: string }[] = [
  { key: "procedure", label: "Procedures" },
  { key: "condition", label: "Conditions" },
  { key: "encounter", label: "Encounters" },
  { key: "medication", label: "Medications" },
  { key: "symptom", label: "Symptoms" },
  { key: "observation", label: "Observations" },
  { key: "finding", label: "Findings" },
  { key: "lab_result", label: "Lab results" },
  { key: "imaging_study", label: "Imaging" },
  { key: "provider_relationship", label: "Providers" },
  { key: "life_context_event", label: "Life context" },
  { key: "inferred_relationship", label: "Inferred relationships" },
];

// Fallback label for fact_types not in TYPE_GROUPS — keeps clusters
// visible if a new fact_type is introduced server-side without a UI
// registration. snake_case → "Snake case".
function humanizeFactType(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
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

function fmtYear(iso: string | null): string {
  if (!iso) return "?";
  try {
    return String(new Date(iso).getFullYear());
  } catch {
    return "?";
  }
}

function dateRangeLabel(c: FactCluster): string {
  if (!c.date_start_min && !c.date_start_max) return "no date";
  if (!c.date_start_min || !c.date_start_max) {
    return fmtDate(c.date_start_min || c.date_start_max);
  }
  const a = fmtYear(c.date_start_min);
  const b = fmtYear(c.date_start_max);
  return a === b ? a : `${a}–${b}`;
}

export function ClusterList({ slug, clusters }: Props) {
  const grouped = new Map<string, FactCluster[]>();
  for (const c of clusters) {
    const arr = grouped.get(c.fact_type) ?? [];
    arr.push(c);
    grouped.set(c.fact_type, arr);
  }

  if (clusters.length === 0) {
    return (
      <p className="mt-3 text-muted">
        No matching facts yet. Ingest a CCDA, fax, or note that mentions this topic.
      </p>
    );
  }

  // Render TYPE_GROUPS in their preferred editorial order, then any
  // remaining fact_types in alphabetical order. Without the second pass
  // an unknown fact_type would silently disappear from the UI even
  // though its clusters are counted in the dossier header.
  const knownKeys = new Set(TYPE_GROUPS.map((g) => g.key));
  const unknownGroups = Array.from(grouped.entries())
    .filter(([k]) => !knownKeys.has(k))
    .map(([k, arr]) => ({ key: k, label: humanizeFactType(k), arr }))
    .sort((a, b) => a.label.localeCompare(b.label));

  return (
    <div className="mt-4 space-y-8">
      {TYPE_GROUPS.map(({ key, label }) => {
        const arr = grouped.get(key);
        if (!arr || arr.length === 0) return null;
        return <FactTypeSection key={key} slug={slug} label={label} arr={arr} />;
      })}
      {unknownGroups.map(({ key, label, arr }) => (
        <FactTypeSection key={key} slug={slug} label={label} arr={arr} />
      ))}
    </div>
  );
}

function FactTypeSection({
  slug,
  label,
  arr,
}: {
  slug: string;
  label: string;
  arr: FactCluster[];
}) {
  const total = arr.reduce((n, c) => n + c.fact_count, 0);
  return (
    <div>
      <h3 className="text-sm font-semibold uppercase tracking-widest text-muted">
        {label} ({total})
      </h3>
      <ul className="mt-2 space-y-2">
        {arr.map((c) => (
          <ClusterCard key={c.cluster_id} slug={slug} cluster={c} />
        ))}
      </ul>
    </div>
  );
}

// Initial fetch size when a cluster is opened; matches the default
// `limit` on GET /clusters/{id}/facts. The "Show more" button bumps to
// MAX_FETCH (the endpoint's hard cap). For clusters larger than that,
// the rest of the series belongs in Discover / the per-metric layer.
const INITIAL_FETCH = 500;
const MAX_FETCH = 2000;

function ClusterCard({ slug, cluster }: { slug: string; cluster: FactCluster }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [facts, setFacts] = useState<FactReadout[] | null>(null);
  const [fetchedLimit, setFetchedLimit] = useState(0);
  const [error, setError] = useState<string | null>(null);

  async function load(nextLimit: number) {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(
        `/api/topics/${encodeURIComponent(slug)}/clusters/${encodeURIComponent(
          cluster.cluster_id,
        )}/facts?limit=${nextLimit}`,
        { credentials: "include" },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setFacts((await r.json()) as FactReadout[]);
      setFetchedLimit(nextLimit);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (facts !== null) return;
    await load(INITIAL_FETCH);
  }

  return (
    <li className="rounded-xl border border-muted/15 bg-surface">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full flex-wrap items-baseline gap-2 px-4 py-3 text-left hover:bg-muted/5"
      >
        <span className="font-medium">{cluster.label}</span>
        <span className="text-xs text-muted">
          {dateRangeLabel(cluster)}
          {" · "}
          {cluster.fact_count} {cluster.fact_count === 1 ? "fact" : "facts"}
          {cluster.source_count > 0 && (
            <>
              {" · "}
              {cluster.source_count} source
              {cluster.source_count === 1 ? "" : "s"}
            </>
          )}
        </span>
        {cluster.needs_review_count > 0 && (
          <span className="rounded-md bg-caution/15 px-1.5 py-0.5 text-xs text-caution">
            {cluster.needs_review_count} need review
          </span>
        )}
        <span className="ml-auto text-xs text-muted">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="border-t border-muted/10 px-4 py-3">
          {loading && <p className="text-xs text-muted">Loading evidence…</p>}
          {error && (
            <p className="text-xs text-caution">Failed to load: {error}</p>
          )}
          {facts && facts.length === 0 && (
            <p className="text-xs text-muted">No facts in this cluster.</p>
          )}
          {facts && facts.length > 0 && (
            <>
            <ul className="divide-y divide-muted/10">
              {facts.map((c) => {
                // Display precedence: user-canonical label > LLM
                // display_label (R5) > original label. Original is
                // always preserved as source-of-truth.
                const display = c.canonical_label || c.display_label || c.label;
                const corrected =
                  c.canonical_label && c.canonical_label !== c.label;
                const sourceHref = c.source_id
                  ? c.source_page
                    ? `/sources/${c.source_id}#page-${c.source_page}`
                    : c.source_anchor_id
                      ? `/sources/${c.source_id}#anchor-${c.source_anchor_id}`
                      : `/sources/${c.source_id}`
                  : null;
                return (
                  <li
                    id={`fact-${c.id}`}
                    key={c.id}
                    className="py-3 scroll-mt-24 first:pt-0 last:pb-0"
                  >
                    <div className="flex flex-wrap items-baseline gap-2">
                      <p className="font-medium">{display}</p>
                      {corrected && (
                        <span className="rounded-md bg-evidence/15 px-1.5 py-0.5 text-xs text-evidence">
                          corrected
                        </span>
                      )}
                      <span className="text-xs text-muted">
                        {fmtDate(c.canonical_date_start || c.date_start)}
                        {c.confidence !== null
                          ? ` · conf ${c.confidence}`
                          : ""}
                        {` · ${c.review_state}`}
                      </span>
                    </div>
                    {(c.canonical_description || c.description) && (
                      <p className="mt-1 text-sm text-muted">
                        {c.canonical_description || c.description}
                      </p>
                    )}
                    {c.source_anchor_excerpt && (
                      <blockquote className="mt-2 border-l-2 border-muted/30 pl-3 text-xs italic text-muted">
                        “{c.source_anchor_excerpt}”
                      </blockquote>
                    )}
                    <p className="mt-0.5 text-xs text-muted">
                      via {c.extraction_method}
                      {corrected ? " · canonicalized by you" : ""}
                      {sourceHref && (
                        <>
                          {" · "}
                          <a
                            href={sourceHref}
                            className="underline-offset-4 hover:underline"
                          >
                            view source
                            {c.source_page
                              ? ` (page ${c.source_page})`
                              : c.source_anchor_section_path
                                ? ` (${c.source_anchor_section_path})`
                                : ""}
                          </a>
                        </>
                      )}
                    </p>
                  </li>
                );
              })}
            </ul>
            {facts.length < cluster.fact_count && (
              <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-muted">
                <span>
                  Showing {facts.length.toLocaleString()} of{" "}
                  {cluster.fact_count.toLocaleString()}.
                </span>
                {fetchedLimit < MAX_FETCH ? (
                  <button
                    type="button"
                    onClick={() => load(MAX_FETCH)}
                    disabled={loading}
                    className="rounded-md border border-muted/30 px-2 py-1 hover:bg-muted/5 disabled:opacity-50"
                  >
                    {loading ? "Loading…" : `Show next batch (up to ${MAX_FETCH.toLocaleString()})`}
                  </button>
                ) : (
                  <span className="italic">
                    The full series will live in Discover (coming).
                  </span>
                )}
              </div>
            )}
            </>
          )}
        </div>
      )}
    </li>
  );
}
