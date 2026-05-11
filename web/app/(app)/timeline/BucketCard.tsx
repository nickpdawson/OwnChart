"use client";

import { useState } from "react";
import type {
  PeriodCluster,
  PeriodEvent,
  PeriodResponse,
  TimelineBucket,
} from "@/lib/api";

const SOURCE_TYPE_LABEL: Record<string, string> = {
  pdf: "PDF",
  fax_pdf: "Fax",
  ccda_xml: "CCDA",
  fhir_bundle: "FHIR",
  photo: "Photo",
  clinical_note: "Note",
  note: "Note",
  auto_export: "Auto Export",
  imaging_dmg: "Imaging",
  dicom: "DICOM",
};

function fmt(n: number): string {
  return n.toLocaleString();
}

function fmtMonthDay(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

function humanizeFactType(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Top N entries from a {key: count} map, sorted by count desc.
function topEntries(
  obj: Record<string, number>,
  n: number,
): { key: string; count: number }[] {
  return Object.entries(obj)
    .map(([key, count]) => ({ key, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, n);
}

export function BucketCard({ bucket }: { bucket: TimelineBucket }) {
  const [open, setOpen] = useState(false);
  const [period, setPeriod] = useState<PeriodResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Use the bucket's `start` to label the year. UTC fixes the off-by-
  // one when the user is in a negative-offset timezone and the
  // bucket's tz-aware midnight rolls back into the prior year.
  const year = new Date(bucket.start).getUTCFullYear();

  // Lane summaries — two top entries each. Clinical breakdown uses
  // humanized fact_type, wearable uses the representative label as-is
  // (already cased "Heart rate" etc.), source uses a short label map.
  const clinicalTop = topEntries(bucket.clinical.by_type, 3);
  const wearableTop = topEntries(bucket.wearable.by_metric, 3);
  const sourceTop = topEntries(bucket.source.by_type, 3);

  async function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (period !== null) return;
    setLoading(true);
    setError(null);
    try {
      const url =
        `/api/timeline/period?start=${encodeURIComponent(bucket.start)}` +
        `&end=${encodeURIComponent(bucket.end)}`;
      const r = await fetch(url, { credentials: "include" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setPeriod((await r.json()) as PeriodResponse);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  const totalCount =
    bucket.clinical.event_count +
    bucket.wearable.fact_count +
    bucket.source.document_count;

  // Strip the year prefix from the narrative summary since the
  // big-serif "2026" already does that job visually. Server returns
  // "2026: Eye surgery at Stanford…" — strip to "Eye surgery at
  // Stanford…" for the card.
  const narrative = bucket.narrative;
  const narrativeBody = narrative
    ? narrative.summary.replace(/^\d{4}(?:\s*[-–]\s*\d{4})?:\s*/, "")
    : null;

  return (
    <li
      id={`year-${year}`}
      className="rounded-xl border border-muted/15 bg-surface scroll-mt-24"
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full flex-col gap-2 px-5 py-4 text-left hover:bg-muted/5"
      >
        <div className="flex w-full items-baseline gap-4">
          <span className="font-serif text-2xl tabular-nums">{year}</span>
          <span
            className={
              "flex-1 font-serif text-base leading-snug " +
              (narrative?.kind === "care_meaningful"
                ? "text-ink"
                : narrative?.kind === "data_dense"
                  ? "text-muted"
                  : "italic text-muted")
            }
          >
            {narrativeBody ?? "OwnChart hasn't yet narrated this year."}
          </span>
          {totalCount > 0 && (
            <span className="text-xs text-muted">{open ? "▾" : "▸"}</span>
          )}
        </div>
        {/* Counts demoted to a small footer row — present for users
            who want the inventory, but no longer the headline. */}
        <span className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
          <Lane
            label="Clinical"
            count={bucket.clinical.event_count}
            top={clinicalTop}
            renderKey={humanizeFactType}
            color="text-ink"
          />
          <Lane
            label="Wearable"
            count={bucket.wearable.fact_count}
            top={wearableTop}
            renderKey={(k) => k}
            color="text-evidence"
          />
          <Lane
            label="Sources"
            count={bucket.source.document_count}
            top={sourceTop}
            renderKey={(k) => SOURCE_TYPE_LABEL[k] || k}
            color="text-accent"
          />
        </span>
      </button>
      {open && (
        <div className="border-t border-muted/10 px-5 py-4">
          {loading && <p className="text-xs text-muted">Loading the period…</p>}
          {error && (
            <p className="text-xs text-caution">Failed to load: {error}</p>
          )}
          {period && (
            <PeriodDrill
              period={period}
              windowStart={bucket.start}
              windowEnd={bucket.end}
            />
          )}
        </div>
      )}
    </li>
  );
}

function Lane({
  label,
  count,
  top,
  renderKey,
  color,
}: {
  label: string;
  count: number;
  top: { key: string; count: number }[];
  renderKey: (key: string) => string;
  color: string;
}) {
  if (count === 0) {
    return (
      <span className="text-muted/60">
        <span className="uppercase tracking-widest text-[10px]">{label}</span>
        <span className="ml-1.5">—</span>
      </span>
    );
  }
  return (
    <span className={color}>
      <span className="uppercase tracking-widest text-[10px] text-muted">
        {label}
      </span>
      <span className="ml-1.5 font-medium tabular-nums">{fmt(count)}</span>
      {top.length > 0 && (
        <span className="ml-1.5 text-xs text-muted">
          (
          {top
            .map((t) => `${renderKey(t.key)} ${fmt(t.count)}`)
            .join(" · ")}
          )
        </span>
      )}
    </span>
  );
}

function PeriodDrill({
  period,
  windowStart,
  windowEnd,
}: {
  period: PeriodResponse;
  windowStart: string;
  windowEnd: string;
}) {
  const wear = period.wearable_summary;
  const wearTop = topEntries(wear.by_metric, 12);

  return (
    <div className="grid gap-6">
      <Section title="Clinical events">
        {period.clinical_clusters.length === 0 ? (
          <p className="text-xs text-muted">
            No dated clinical events in this window.
          </p>
        ) : (
          <ul className="space-y-2">
            {period.clinical_clusters.map((c) => (
              <PeriodClusterCard
                key={c.cluster_id}
                cluster={c}
                windowStart={windowStart}
                windowEnd={windowEnd}
              />
            ))}
          </ul>
        )}
      </Section>

      <Section title="Wearable metrics">
        {wear.fact_count === 0 ? (
          <p className="text-xs text-muted">
            No wearable readings in this window.
          </p>
        ) : (
          <>
            <p className="text-xs text-muted">
              {fmt(wear.fact_count)} reading
              {wear.fact_count === 1 ? "" : "s"} across {wearTop.length}{" "}
              metric{wearTop.length === 1 ? "" : "s"}.
            </p>
            <ul className="mt-2 grid gap-1 sm:grid-cols-2">
              {wearTop.map((m) => (
                <li
                  key={m.key}
                  className="flex items-baseline justify-between gap-3 text-sm"
                >
                  <span>{m.key}</span>
                  <span className="tabular-nums text-muted">
                    {fmt(m.count)}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </Section>

      <Section title="Source documents">
        {period.sources.length === 0 ? (
          <p className="text-xs text-muted">
            No source documents dated in this window.
          </p>
        ) : (
          <ul className="divide-y divide-muted/10">
            {period.sources.map((s) => (
              <li key={s.id} className="py-2 text-sm">
                <a
                  href={`/sources/${s.id}`}
                  className="flex flex-wrap items-baseline justify-between gap-2"
                >
                  <span className="font-medium">
                    {s.source_label || s.original_filename || "(unlabeled)"}
                  </span>
                  <span className="text-xs text-muted">
                    {SOURCE_TYPE_LABEL[s.source_type] || s.source_type} ·{" "}
                    {fmtMonthDay(s.event_date)}
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-widest text-muted">
        {title}
      </h4>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function fmtYearShort(iso: string | null): string {
  if (!iso) return "?";
  try {
    return String(new Date(iso).getUTCFullYear());
  } catch {
    return "?";
  }
}

function clusterDateRange(c: PeriodCluster): string {
  if (!c.date_start_min && !c.date_start_max) return "no date";
  const a = fmtYearShort(c.date_start_min);
  const b = fmtYearShort(c.date_start_max);
  if (a === "?" && b === "?") return "no date";
  return a === b ? a : `${a}–${b}`;
}

function PeriodClusterCard({
  cluster,
  windowStart,
  windowEnd,
}: {
  cluster: PeriodCluster;
  windowStart: string;
  windowEnd: string;
}) {
  const [open, setOpen] = useState(false);
  const [facts, setFacts] = useState<PeriodEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (facts !== null) return;
    setLoading(true);
    setError(null);
    try {
      const url =
        `/api/timeline/period/cluster/facts` +
        `?start=${encodeURIComponent(windowStart)}` +
        `&end=${encodeURIComponent(windowEnd)}` +
        `&cluster_id=${encodeURIComponent(cluster.cluster_id)}`;
      const r = await fetch(url, { credentials: "include" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setFacts((await r.json()) as PeriodEvent[]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <li className="rounded-lg border border-muted/15 bg-bg/50">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full flex-wrap items-baseline gap-2 px-3 py-2 text-left text-sm hover:bg-muted/5"
      >
        <span className="text-[10px] uppercase tracking-widest text-muted">
          {humanizeFactType(cluster.fact_type)}
        </span>
        <span className="font-medium">{cluster.label}</span>
        <span className="text-xs text-muted">
          {clusterDateRange(cluster)}
          {" · "}
          {fmt(cluster.fact_count)}{" "}
          {cluster.fact_count === 1 ? "fact" : "facts"}
          {cluster.source_count > 0 && (
            <>
              {" · "}
              {cluster.source_count} source
              {cluster.source_count === 1 ? "" : "s"}
            </>
          )}
        </span>
        {cluster.needs_review_count > 0 && (
          <span className="rounded-md bg-caution/15 px-1.5 py-0.5 text-[10px] text-caution">
            {fmt(cluster.needs_review_count)} need review
          </span>
        )}
        <span className="ml-auto text-xs text-muted">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="border-t border-muted/10 px-3 py-2">
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
                {facts.map((e) => (
                  <li key={e.id} className="text-sm">
                    {/* docs/07 R3: clicking a fact opens the global
                        context sidesheet via ?fact= (relative path
                        keeps the user on /timeline). View-source is
                        now an action inside the sheet, not an
                        inline shortcut. */}
                    <a
                      href={`?fact=${encodeURIComponent(e.id)}`}
                      className="-mx-3 block rounded-md px-3 py-2 hover:bg-muted/5"
                    >
                      <span className="flex flex-wrap items-baseline gap-2">
                        <span className="text-xs tabular-nums text-muted">
                          {fmtMonthDay(e.date_start)}
                        </span>
                        <span className="font-medium">{e.display_label ?? e.label}</span>
                        {e.review_state !== "confirmed" &&
                          e.review_state !== "auto_confirmed" && (
                            <span className="text-xs text-caution">
                              · {e.review_state}
                            </span>
                          )}
                      </span>
                    </a>
                  </li>
                ))}
              </ul>
              {facts.length < cluster.fact_count && (
                <p className="mt-2 text-xs text-muted">
                  Showing {fmt(facts.length)} of {fmt(cluster.fact_count)}.
                  Drill into the dossier or Discover for more context.
                </p>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}
