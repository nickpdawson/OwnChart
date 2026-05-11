import { getTimeline } from "@/lib/api";
import { BucketCard } from "./BucketCard";
import { LifeStrip } from "./LifeStrip";

export const dynamic = "force-dynamic";

function fmtNum(n: number): string {
  return n.toLocaleString();
}

export default async function TimelinePage() {
  let tl;
  try {
    tl = await getTimeline({ grain: "year" });
  } catch (e) {
    return (
      <div className="max-w-4xl">
        <p className="text-sm uppercase tracking-widest text-muted">Timeline</p>
        <h1 className="mt-2 font-serif text-3xl">Couldn&apos;t load timeline</h1>
        <p className="mt-2 text-sm text-caution">{(e as Error).message}</p>
      </div>
    );
  }

  // Newest year first in the vertical card list — usually the
  // most-actionable scroll position. The horizontal LifeStrip uses
  // the natural oldest→newest order (reading direction).
  const buckets = [...tl.buckets].reverse();
  const stripBuckets = tl.buckets;

  // Roll-up totals across all buckets, used in the page header so the
  // user gets a sense of the corpus at a glance before drilling in.
  let totalClinical = 0;
  let totalWearable = 0;
  let totalSources = 0;
  for (const b of buckets) {
    totalClinical += b.clinical.event_count;
    totalWearable += b.wearable.fact_count;
    totalSources += b.source.document_count;
  }

  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">Timeline</p>
      <h1 className="mt-2 font-serif text-3xl">Global timeline</h1>
      {buckets.length === 0 ? (
        <p className="mt-3 text-muted">
          No dated events yet. Once facts have dates attached, the global
          timeline fills in automatically — clinical events, wearable density,
          and source documents in the same view.
        </p>
      ) : (
        <>
          <p className="mt-3 max-w-2xl font-serif text-lg leading-relaxed text-muted">
            <span className="text-ink tabular-nums">{fmtNum(buckets.length)}</span>{" "}
            year{buckets.length === 1 ? "" : "s"} on the record ·{" "}
            <span className="text-ink tabular-nums">{fmtNum(totalClinical)}</span>{" "}
            clinical event{totalClinical === 1 ? "" : "s"} ·{" "}
            <span className="text-ink tabular-nums">{fmtNum(totalWearable)}</span>{" "}
            wearable reading{totalWearable === 1 ? "" : "s"} ·{" "}
            <span className="text-ink tabular-nums">{fmtNum(totalSources)}</span>{" "}
            source document{totalSources === 1 ? "" : "s"}.
          </p>
          <LifeStrip buckets={stripBuckets} />
          <p className="mt-3 text-xs text-muted">
            Tap a year on the strip to jump · tap a card to see events,
            metrics, and sources in that window.
          </p>
          <ul className="mt-6 space-y-3">
            {buckets.map((b) => (
              <BucketCard key={b.start} bucket={b} />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
