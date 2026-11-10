import type { DiscoverItem, SignalStrength } from "@/lib/api";

// Badge classes by signal_strength. The vocabulary matches what the
// eventual statistical / AI-narrated items will use, so the color
// contract doesn't change when the engine behind the feed evolves.
//
// strong       → accent (positive surface, "this is real")
// moderate     → evidence (slightly muted, "looks like something")
// needs_review → caution (attention required, not assertion)
export const STRENGTH_BADGE: Record<SignalStrength, string> = {
  strong: "bg-accent/15 text-accent",
  moderate: "bg-evidence/15 text-evidence",
  needs_review: "bg-caution/15 text-caution",
};

// Labels say what is known, not how confident a heuristic feels. A
// count of dated records is coverage, not clinical significance
// (audit 2026-11-10, item 10; "significance over fact-count").
export const STRENGTH_LABEL: Record<SignalStrength, string> = {
  strong: "Many dated records",
  moderate: "More than usual",
  needs_review: "Needs a look",
};

function fmtMonthYear(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
    });
  } catch {
    return iso;
  }
}

function evidenceWindowLabel(item: DiscoverItem): string | null {
  if (!item.evidence_window) return null;
  const a = fmtMonthYear(item.evidence_window.start);
  const b = fmtMonthYear(item.evidence_window.end);
  return a === b ? a : `${a} → ${b}`;
}

// docs/07 R4 — type-specific visual treatment. connected_episode
// is the "OwnChart thinks these are parts of one event, not
// separate moments" insight; it earns a different border accent +
// secondary action ("Ask what it means") than the count-based
// dense_period / long_gap items.
const TYPE_BORDER: Record<string, string> = {
  connected_episode: "border-accent/40",
  dense_period: "border-muted/15",
  long_gap: "border-muted/15",
  unreviewed_high_count: "border-caution/30",
};

const TYPE_EYEBROW: Record<string, string> = {
  connected_episode: "Event",
  dense_period: "Many records",
  long_gap: "Gap in records",
  unreviewed_high_count: "Review",
};

export function DiscoverCard({ item }: { item: DiscoverItem }) {
  const window = evidenceWindowLabel(item);
  const borderClass = TYPE_BORDER[item.type] ?? "border-muted/15";
  const eyebrow = TYPE_EYEBROW[item.type] ?? item.type;

  // For connected_episode, build a secondary "Ask what it means"
  // affordance — the action_href points at the fact-context sheet
  // anchor, and the Ask link prefills a contextual question. Both
  // are non-destructive and route the user to the existing surfaces.
  const askHref =
    item.type === "connected_episode"
      ? `/chat?q=${encodeURIComponent(`What happened in ${item.title}?`)}`
      : null;

  return (
    <li className={"rounded-xl border bg-surface p-5 " + borderClass}>
      <p className="text-xs uppercase tracking-widest text-muted">{eyebrow}</p>
      <div className="mt-1 flex flex-wrap items-baseline justify-between gap-3">
        <h3 className="font-serif text-lg">{item.title}</h3>
        <span
          className={
            "rounded-md px-2 py-0.5 text-xs font-medium " +
            STRENGTH_BADGE[item.signal_strength]
          }
        >
          {STRENGTH_LABEL[item.signal_strength]}
        </span>
      </div>
      <p className="mt-2 text-sm text-muted">{item.why_surfaced}</p>
      {window && <p className="mt-1 text-xs text-muted">Window: {window}</p>}
      <div className="mt-3 flex flex-wrap items-baseline gap-3 text-sm">
        <span className="text-muted">{item.suggested_action}</span>
        {item.action_href && (
          // Plain <a>: hrefs are deterministic V1 strings
          // (/timeline#year-YYYY, ?fact=…) — Link's typed-routes
          // rejects arbitrary strings; no perf gain from prefetch.
          <a href={item.action_href} className="text-accent underline-offset-4 hover:underline">
            Open →
          </a>
        )}
        {askHref && (
          <a
            href={askHref}
            className="text-muted underline-offset-4 hover:text-ink hover:underline"
          >
            Ask what it means →
          </a>
        )}
      </div>
    </li>
  );
}
