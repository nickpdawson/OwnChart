"use client";

import type { FactReadout } from "@/lib/api";

const COLOR_BY_TYPE: Record<string, string> = {
  procedure: "rgb(var(--oc-accent))",
  condition: "rgb(var(--oc-evidence))",
  medication: "rgb(var(--oc-caution))",
  encounter: "rgb(var(--oc-muted))",
  symptom: "rgb(var(--oc-caution))",
  observation: "rgb(var(--oc-muted))",
  life_context_event: "rgb(var(--oc-accent))",
  inferred_relationship: "rgb(var(--oc-muted))",
};

export function Timeline({ facts }: { facts: FactReadout[] }) {
  const dated = facts
    .map((c) => ({ c, t: c.canonical_date_start || c.date_start }))
    .filter((x): x is { c: FactReadout; t: string } => Boolean(x.t));

  if (dated.length < 2) {
    return (
      <p className="mt-3 text-sm text-muted">
        Need at least two dated facts to render a timeline. Got {dated.length}.
      </p>
    );
  }

  const times = dated.map((d) => +new Date(d.t));
  const min = Math.min(...times);
  const max = Math.max(...times);
  const span = Math.max(max - min, 86400000); // avoid div/0 if all same day

  const yearOf = (ms: number) => new Date(ms).getFullYear();
  const minYear = yearOf(min);
  const maxYear = yearOf(max);
  const yearTicks: number[] = [];
  const tickStep = Math.max(1, Math.ceil((maxYear - minYear) / 8));
  for (let y = minYear; y <= maxYear; y += tickStep) yearTicks.push(y);

  return (
    <div className="mt-3 rounded-xl border border-muted/15 bg-surface p-6">
      <div className="relative h-32 w-full">
        <div className="absolute left-0 right-0 top-1/2 h-px bg-muted/30" />
        {yearTicks.map((y) => {
          const ms = +new Date(`${y}-01-01T00:00:00Z`);
          const left = ((ms - min) / span) * 100;
          if (left < 0 || left > 100) return null;
          return (
            <div key={y} className="absolute" style={{ left: `${left}%`, top: "50%" }}>
              <div className="h-2 w-px bg-muted/40 -translate-x-1/2" />
              <div className="mt-2 -translate-x-1/2 text-xs text-muted">{y}</div>
            </div>
          );
        })}
        {dated.map(({ c, t }, i) => {
          const left = ((+new Date(t) - min) / span) * 100;
          const color = COLOR_BY_TYPE[c.fact_type] || "rgb(var(--oc-muted))";
          const stacked = (i % 2) === 0 ? "-top-1" : "top-2";
          return (
            <div
              key={c.id}
              title={`${c.canonical_label || c.label} — ${new Date(t).toLocaleDateString()}`}
              className={`absolute ${stacked} -translate-x-1/2`}
              style={{ left: `${left}%` }}
            >
              <div
                className="h-3 w-3 rounded-full ring-2 ring-surface"
                style={{ background: color }}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
