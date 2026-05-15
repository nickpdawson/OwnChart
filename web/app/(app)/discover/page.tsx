import type { DiscoverItem, SignalStrength } from "@/lib/api";
import { getDiscover } from "@/lib/api";

export const dynamic = "force-dynamic";

// Badge classes by signal_strength. The vocabulary matches what the
// eventual statistical / AI-narrated items will use, so the color
// contract doesn't change when the engine behind the feed evolves.
//
// strong       → accent (positive surface, "this is real")
// moderate     → evidence (slightly muted, "looks like something")
// needs_review → caution (attention required, not assertion)
const STRENGTH_BADGE: Record<SignalStrength, string> = {
  strong: "bg-accent/15 text-accent",
  moderate: "bg-evidence/15 text-evidence",
  needs_review: "bg-caution/15 text-caution",
};

const STRENGTH_LABEL: Record<SignalStrength, string> = {
  strong: "Strong signal",
  moderate: "Moderate signal",
  needs_review: "Needs review",
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

export default async function DiscoverPage() {
  let feed;
  try {
    feed = await getDiscover();
  } catch (e) {
    return (
      <div className="max-w-4xl">
        <p className="text-sm uppercase tracking-widest text-muted">Discover</p>
        <h1 className="mt-2 font-serif text-3xl">Couldn&apos;t load Discover</h1>
        <p className="mt-2 text-sm text-caution">{(e as Error).message}</p>
      </div>
    );
  }

  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">Discover</p>
      <h1 className="mt-2 font-serif text-3xl">What&apos;s worth noticing</h1>
      <p className="mt-2 max-w-2xl text-muted">
        Deterministic observations across your records — dense periods, long
        gaps, review backlogs. Statistical correlations and AI-narrated items
        will appear in this same feed once they land.
      </p>

      {feed.items.length === 0 ? (
        <p className="mt-8 text-muted">
          Nothing surfaced yet. Once you have at least three years of dated
          events, dense periods and gaps will start to appear here.
        </p>
      ) : (
        <ul className="mt-6 space-y-4">
          {feed.items.map((item) => (
            <DiscoverCard key={item.id} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
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
  dense_period: "Dense period",
  long_gap: "Gap",
  unreviewed_high_count: "Review",
};


function DiscoverCard({ item }: { item: DiscoverItem }) {
  const window = evidenceWindowLabel(item);
  const borderClass = TYPE_BORDER[item.type] ?? "border-muted/15";
  const eyebrow = TYPE_EYEBROW[item.type] ?? item.type;

  // For connected_episode, build a secondary "Ask what it means"
  // affordance — the action_href points at the fact-context sheet
  // anchor, and the Ask link prefills a contextual question. Both
  // are non-destructive and route the user to the existing surfaces.
  const askHref =
    item.type === "connected_episode"
      ? `/ask?q=${encodeURIComponent(`What happened in ${item.title}?`)}`
      : null;

  return (
    <li className={"rounded-xl border bg-surface p-5 " + borderClass}>
      <p className="text-[10px] uppercase tracking-widest text-muted">
        {eyebrow}
      </p>
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
      {window && (
        <p className="mt-1 text-xs text-muted">Window: {window}</p>
      )}
      <div className="mt-3 flex flex-wrap items-baseline gap-3 text-sm">
        <span className="text-muted">{item.suggested_action}</span>
        {item.action_href && (
          // Plain <a>: hrefs are deterministic V1 strings
          // (/timeline#year-YYYY, ?fact=…) — Link's typed-routes
          // rejects arbitrary strings; no perf gain from prefetch.
          <a
            href={item.action_href}
            className="text-accent underline-offset-4 hover:underline"
          >
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
