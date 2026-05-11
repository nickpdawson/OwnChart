"use client";

import type { TimelineBucket } from "@/lib/api";

// docs/07 §270-285 + UXUI_proposals.md M1 / §T1: the Pictal-inspired
// horizontal life journey. One vertical rod per year, height
// proportional to event density (sqrt-scaled so a 30k-reading year
// doesn't crush a 3-event year). Clicking a rod scroll-anchors the
// matching BucketCard into view. Sticky at the top of /timeline so
// the strip persists as the user scrolls through year cards.

function totalCount(b: TimelineBucket): number {
  return (
    b.clinical.event_count +
    b.wearable.fact_count +
    b.source.document_count
  );
}

function fmt(n: number): string {
  return n.toLocaleString();
}

export function LifeStrip({ buckets }: { buckets: TimelineBucket[] }) {
  if (buckets.length === 0) return null;

  const max = Math.max(...buckets.map(totalCount), 1);
  const firstYear = new Date(buckets[0].start).getUTCFullYear();
  const lastYear = new Date(buckets[buckets.length - 1].start).getUTCFullYear();

  return (
    <nav
      aria-label="Life strip — click a year to jump"
      className="sticky top-0 z-20 -mx-5 mt-6 overflow-x-auto border-y border-muted/15 bg-bg/85 px-5 py-3 backdrop-blur md:-mx-10 md:px-10"
    >
      <ul className="flex min-w-full items-end gap-[3px]">
        {buckets.map((b) => {
          const year = new Date(b.start).getUTCFullYear();
          const total = totalCount(b);
          // Sqrt-scaled height so the high-density years don't crush
          // the rest. Minimum 6px so every year shows a visible mark
          // even when empty (calm, not invisible).
          const h = Math.max(6, Math.round(Math.sqrt(total / max) * 64));
          // Label every 5th year + the first and last so the strip
          // reads like a Pictal life-journey ruler with clear bookends.
          const isLabeled =
            year % 5 === 0 || year === firstYear || year === lastYear;
          return (
            <li key={b.start} className="flex flex-col items-center">
              <a
                href={`#year-${year}`}
                className={
                  "block w-[6px] rounded-sm transition-colors " +
                  (total > 0
                    ? "bg-ink/70 hover:bg-accent"
                    : "bg-muted/20 hover:bg-muted/40")
                }
                style={{ height: `${h}px` }}
                title={`${year}: ${fmt(total)} event${total === 1 ? "" : "s"}`}
                aria-label={`${year}: ${fmt(total)} events`}
              />
              {isLabeled && (
                <span className="mt-1 font-mono text-[9px] tabular-nums text-muted">
                  &apos;{String(year).slice(-2)}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
