import Link from "next/link";
import {
  getDashboardStats,
  getDiscover,
  getHomeAiPartner,
  getMe,
  getNotableEvents,
  getTimeline,
  type DiscoverItem,
  type HomeAiPartner,
  type NotableEvent,
  type SignalStrength,
  type TimelineBucket,
} from "@/lib/api";

export const dynamic = "force-dynamic";

// Per docs/06 §50: home should not lead with raw-source telemetry
// ("Auto Export push 2026-05-09 18:42 UTC"). The three modules below
// are docs/06 §54/§67/§80 — Life Overview, What's Worth Noticing,
// Continue Researching — kept lightweight by deliberate V1 design
// (no charts, no zoom controls, no LLM).

function fmtNum(n: number): string {
  return n.toLocaleString();
}

const STRENGTH_BADGE: Record<SignalStrength, string> = {
  strong: "bg-accent/15 text-accent",
  moderate: "bg-evidence/15 text-evidence",
  needs_review: "bg-caution/15 text-caution",
};

const STRENGTH_LABEL: Record<SignalStrength, string> = {
  strong: "Strong",
  moderate: "Moderate",
  needs_review: "Review",
};

export default async function DashboardPage() {
  // Fetch home modules in parallel. A failure on any one shouldn't
  // blank the whole home — render what we can, surface the error
  // inline.
  const [statsR, timelineR, discoverR, meR, notableR, partnerR] =
    await Promise.allSettled([
      getDashboardStats(),
      getTimeline({ grain: "year" }),
      getDiscover(3),
      getMe(),
      getNotableEvents(6),
      getHomeAiPartner(),
    ]);

  const stats = statsR.status === "fulfilled" ? statsR.value : null;
  const timeline = timelineR.status === "fulfilled" ? timelineR.value : null;
  const discover = discoverR.status === "fulfilled" ? discoverR.value : null;
  const me = meR.status === "fulfilled" ? meR.value : null;
  const notable = notableR.status === "fulfilled" ? notableR.value : [];
  const partner: HomeAiPartner | null =
    partnerR.status === "fulfilled" ? partnerR.value : null;
  const recentEpisodes = partner?.recent_episodes ?? [];

  // Personalized greeting is intentionally simple — the prior
  // regex-based first-name guess produced bad output for emails
  // whose local-part isn't a real first name. A proper fix is a
  // User.display_name column the user sets in settings; until then,
  // drop the name and let the corpus sentence below do the
  // personalization work ("Your record spans 30 years...").
  // Touch `me` so the unused-var lint stays quiet; keeps the fetch
  // wired for a future display_name lookup.
  void me;

  const isEmpty =
    stats !== null &&
    stats.source_count === 0 &&
    stats.facts.total === 0;

  if (isEmpty) {
    return (
      <div className="max-w-4xl">
        <p className="text-sm uppercase tracking-widest text-muted">Home</p>
        <h1 className="mt-2 font-serif text-3xl">Your living record — empty</h1>
        <p className="mt-4 max-w-2xl text-muted">
          Once you ingest your first source — a fax PDF, a CCDA, an Apple
          Health export, or a connected EHR — a global timeline will appear
          here, alongside Discover items as the system finds patterns.
        </p>
        <div className="mt-6 flex flex-wrap gap-3 text-sm">
          <Link
            href="/sources"
            className="rounded-md border border-muted/30 px-3 py-1.5 hover:bg-muted/5"
          >
            Open evidence vault →
          </Link>
          <Link
            href="/connectors"
            className="rounded-md border border-muted/30 px-3 py-1.5 hover:bg-muted/5"
          >
            Connect an EHR →
          </Link>
        </div>
      </div>
    );
  }

  // Home lead is anchored on the top significance-ranked event (Q
  // 2026-05-11): the R1 year-bucket narratives are not yet good enough
  // to drive the headline. Use them only as a quiet "data assembled"
  // footer when no major events exist.
  const recentBuckets = timeline?.buckets ?? [];
  const topNotable = notable[0] ?? null;

  // Suggested questions are deterministic and shaped by what data
  // exists — we never invent a topic the user doesn't have.
  const suggestedQuestions = buildSuggestedQuestions({
    notable,
    topicNames: (stats?.topics ?? []).map((t) => t.name),
  });

  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">Home</p>
      <h1 className="mt-2 font-serif text-3xl">Your living record</h1>
      {topNotable ? (
        <p className="mt-3 max-w-2xl font-serif text-lg leading-relaxed text-muted">
          The most recent major{" "}
          {topNotable.fact_type.replace(/_/g, " ")} on your record is{" "}
          <a
            href={`?fact=${encodeURIComponent(topNotable.id)}`}
            className="text-ink underline-offset-4 hover:underline"
          >
            {topNotable.display_label ?? topNotable.label}
          </a>{" "}
          on{" "}
          {new Date(topNotable.date_start).toLocaleDateString(undefined, {
            year: "numeric",
            month: "long",
            day: "numeric",
          })}
          .
        </p>
      ) : recentBuckets.length > 0 ? (
        <p className="mt-3 max-w-2xl font-serif text-lg italic leading-relaxed text-muted">
          Data assembled, but no major events surfaced yet. Mark a fact
          as a major event from its Fact Context sheet — Home will lead
          with whatever you promote.
        </p>
      ) : (
        <p className="mt-3 max-w-2xl font-serif text-lg italic leading-relaxed text-muted">
          Once you ingest a source, your record will appear here.
        </p>
      )}

      {/* --- Continue exploring ----------------------------------------- */}
      {stats && stats.topics.length > 0 && (
        <section className="mt-10">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="font-serif text-xl">Continue exploring</h2>
            <Link
              href="/topics"
              className="text-sm text-muted underline-offset-4 hover:underline"
            >
              All dossiers →
            </Link>
          </div>
          <p className="mt-1 text-sm text-muted">
            Topics with evidence in your record. Each dossier collects facts,
            sources, and timeline context for one part of your health.
          </p>
          <ul className="mt-3 grid gap-3 sm:grid-cols-2">
            {stats.topics.slice(0, 6).map((t) => (
              <li key={t.id}>
                <Link
                  href={`/dossier/${t.slug}` as const}
                  className="block rounded-xl border border-muted/15 bg-surface p-4 hover:border-accent/40"
                >
                  <h3 className="font-serif text-base">{t.name}</h3>
                  {t.description && (
                    <p className="mt-1 line-clamp-2 text-sm text-muted">
                      {t.description}
                    </p>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* --- Saved episodes ---------------------------------------------- */}
      {recentEpisodes.length > 0 && (
        <section className="mt-10">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="font-serif text-xl">Your episodes</h2>
            <Link
              href="/chat"
              className="text-sm text-muted underline-offset-4 hover:underline"
            >
              Start a new thread →
            </Link>
          </div>
          <p className="mt-1 text-sm text-muted">
            Life moments you&apos;ve saved from chat threads. Each one
            keeps its members, narrative, and follow-up questions.
          </p>
          <ul className="mt-3 grid gap-2 sm:grid-cols-2">
            {recentEpisodes.map((e) => {
              const when = e.date_start
                ? new Date(e.date_start).toLocaleDateString(undefined, {
                    year: "numeric",
                    month: "short",
                    day: "numeric",
                  })
                : null;
              return (
                <li key={e.id}>
                  <Link
                    href={`/episode/${e.id}` as never}
                    className="block rounded-xl border border-accent/30 bg-accent/5 p-4 hover:border-accent/60"
                  >
                    <div className="flex flex-wrap items-baseline gap-x-2">
                      <span className="text-[10px] uppercase tracking-widest text-accent">
                        {e.kind.replace(/_/g, " ")}
                      </span>
                      {when && (
                        <span className="font-mono text-xs tabular-nums text-muted">
                          {when}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 font-serif text-base text-ink">
                      {e.title}
                    </p>
                    {e.summary && (
                      <p className="mt-1 line-clamp-2 text-sm text-muted">
                        {e.summary}
                      </p>
                    )}
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {/* --- Newly assembled --------------------------------------------- */}
      {notable.length > 0 && (
        <section className="mt-10">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="font-serif text-xl">Newly assembled</h2>
            <Link
              href="/timeline"
              className="text-sm text-muted underline-offset-4 hover:underline"
            >
              Open timeline →
            </Link>
          </div>
          <p className="mt-1 text-sm text-muted">
            Recent dated clinical events OwnChart has connected to your
            record. Tap to jump to that year on the timeline.
          </p>
          <NotableMoments events={notable} />
        </section>
      )}

      {/* --- Life Overview ------------------------------------------------ */}
      <section className="mt-10">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="font-serif text-xl">Life overview</h2>
          <Link
            href="/timeline"
            className="text-sm text-muted underline-offset-4 hover:underline"
          >
            Open timeline →
          </Link>
        </div>
        <LifeOverview timeline={timeline} />
      </section>

      {/* --- What's worth noticing --------------------------------------- */}
      <section className="mt-10">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="font-serif text-xl">What&apos;s worth noticing</h2>
          <Link
            href="/discover"
            className="text-sm text-muted underline-offset-4 hover:underline"
          >
            Open Discover →
          </Link>
        </div>
        <WorthNoticing items={discover?.items ?? []} />
      </section>

      {/* --- Questions to ask -------------------------------------------- */}
      {suggestedQuestions.length > 0 && (
        <section className="mt-10">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="font-serif text-xl">Questions to ask</h2>
            <Link
              href="/ask"
              className="text-sm text-muted underline-offset-4 hover:underline"
            >
              Open Ask →
            </Link>
          </div>
          <p className="mt-1 text-sm text-muted">
            Things your record can already answer with citations.
          </p>
          <ul className="mt-3 flex flex-wrap gap-2">
            {suggestedQuestions.map((q) => (
              <li key={q}>
                {/* Plain <a> — typed-routes rejects runtime query
                    strings; /ask is the static route, ?q= is a hint
                    the page reads on mount. */}
                <a
                  href={`/ask?q=${encodeURIComponent(q)}`}
                  className="inline-block rounded-md border border-muted/30 bg-surface px-3 py-1.5 text-sm hover:border-accent/40"
                >
                  {q}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}

    </div>
  );
}

// Suggested questions are deterministic — we only suggest things the
// user's record can actually answer with citations. Per docs/04
// research-partner voice + product/design team 2026-05-10: never
// invent a topic the user doesn't have.
function buildSuggestedQuestions({
  notable,
  topicNames,
}: {
  notable: NotableEvent[];
  topicNames: string[];
}): string[] {
  const out: string[] = [];
  // 1. Most-recent dated clinical event → "What happened around X?"
  const lead = notable[0];
  if (lead) {
    const d = new Date(lead.date_start);
    const mo = d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    out.push(`What happened around my ${mo} ${lead.fact_type}?`);
  }
  // 2. Top dossier — "Tell me the story of X."
  const topic = topicNames[0];
  if (topic) {
    out.push(`Tell me the story of my ${topic.toLowerCase()}.`);
  }
  // 3. Always useful, never wrong.
  out.push("What medications am I currently taking?");
  out.push("What needs my review right now?");
  return out.slice(0, 4);
}

function LifeOverview({ timeline }: { timeline: Awaited<ReturnType<typeof getTimeline>> | null }) {
  if (!timeline) {
    return (
      <p className="mt-3 text-sm text-caution">
        Couldn&apos;t load the timeline preview. Try the Timeline tab directly.
      </p>
    );
  }
  if (timeline.buckets.length === 0) {
    return (
      <p className="mt-3 text-sm text-muted">
        No dated events yet. As facts get dates attached, the year strip below
        will fill in.
      </p>
    );
  }

  // Show the strip oldest → newest left-to-right (reading order). On
  // narrow viewports it scrolls horizontally; the buckets stay
  // uniform-width so the lane indicators line up across the strip.
  const buckets = timeline.buckets;

  let totalClinical = 0;
  let totalWearable = 0;
  let totalSources = 0;
  for (const b of buckets) {
    totalClinical += b.clinical.event_count;
    totalWearable += b.wearable.fact_count;
    totalSources += b.source.document_count;
  }

  return (
    <>
      <p className="mt-2 text-sm text-muted">
        <span className="text-ink tabular-nums">{fmtNum(buckets.length)}</span>{" "}
        year{buckets.length === 1 ? "" : "s"} ·{" "}
        <span className="text-ink tabular-nums">{fmtNum(totalClinical)}</span>{" "}
        clinical ·{" "}
        <span className="text-ink tabular-nums">{fmtNum(totalWearable)}</span>{" "}
        wearable ·{" "}
        <span className="text-ink tabular-nums">{fmtNum(totalSources)}</span>{" "}
        sources.
      </p>
      <div className="mt-4 -mx-1 overflow-x-auto pb-2">
        <ul className="flex min-w-full gap-1 px-1">
          {buckets.map((b) => (
            <YearCell key={b.start} bucket={b} />
          ))}
        </ul>
      </div>
      {/* Lane legend — fixes UXUI-proposal §H1: color-only had no
          meaning before. Three small squares match the LaneBar palette
          so the strip's bars are decodable at a glance. */}
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-sm bg-ink" /> clinical
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-sm bg-evidence" /> wearable
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-sm bg-accent" /> sources
        </span>
        <span className="ml-auto text-muted/70">
          Tap a year to open it on the timeline.
        </span>
      </div>
    </>
  );
}

function NotableMoments({ events }: { events: NotableEvent[] }) {
  return (
    <ul className="mt-3 grid gap-2 sm:grid-cols-2">
      {events.map((e) => {
        const d = new Date(e.date_start);
        const display = d.toLocaleDateString(undefined, {
          year: "numeric",
          month: "short",
          day: "numeric",
        });
        return (
          <li key={e.id}>
            {/* docs/07 R3: clicking a moment opens the fact-context
                sidesheet via ?fact= rather than jumping to the
                /timeline year anchor. The sheet's actions include
                navigation to source / dossier / ask, so the user
                doesn't lose this view to learn more. */}
            <a
              href={`?fact=${encodeURIComponent(e.id)}`}
              className="block rounded-xl border border-muted/15 bg-surface p-4 hover:border-accent/40"
            >
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-[10px] uppercase tracking-widest text-muted">
                  {e.fact_type.replace(/_/g, " ")}
                </span>
                <span className="font-mono text-xs tabular-nums text-muted">
                  {display}
                </span>
              </div>
              <p className="mt-1 font-serif text-base text-ink">{e.display_label ?? e.label}</p>
              {e.source_label && (
                <p className="mt-1 text-xs text-muted">
                  from {e.source_label}
                </p>
              )}
            </a>
          </li>
        );
      })}
    </ul>
  );
}

function YearCell({ bucket }: { bucket: TimelineBucket }) {
  const year = new Date(bucket.start).getUTCFullYear();
  // Lane intensity: square-rooted so a year with 30k wearable readings
  // doesn't render the same dot weight as a year with 1 — but a year
  // with 1 still looks distinguishable from 0. Capped to keep the
  // visual range bounded.
  const intensity = (n: number, cap: number) =>
    n === 0 ? 0 : Math.min(1, Math.sqrt(n) / Math.sqrt(cap));
  const cIn = intensity(bucket.clinical.event_count, 50);
  const wIn = intensity(bucket.wearable.fact_count, 50_000);
  const sIn = intensity(bucket.source.document_count, 30);
  const total =
    bucket.clinical.event_count +
    bucket.wearable.fact_count +
    bucket.source.document_count;

  return (
    <li className="min-w-[3.25rem] flex-1">
      <a
        href={`/timeline#year-${year}`}
        className="block rounded-md border border-muted/15 bg-surface p-2 text-center hover:border-accent/40"
        title={
          `${year}\n` +
          `${fmtNum(bucket.clinical.event_count)} clinical events\n` +
          `${fmtNum(bucket.wearable.fact_count)} wearable readings\n` +
          `${fmtNum(bucket.source.document_count)} source documents`
        }
      >
        <span className="block font-mono text-xs tabular-nums text-muted">
          {String(year).slice(-2)}
        </span>
        <span className="mt-1 flex items-end justify-center gap-0.5">
          <LaneBar intensity={cIn} colorClass="bg-ink" />
          <LaneBar intensity={wIn} colorClass="bg-evidence" />
          <LaneBar intensity={sIn} colorClass="bg-accent" />
        </span>
        <span
          className={
            "mt-1 block text-[10px] tabular-nums " +
            (total > 0 ? "text-muted" : "text-muted/40")
          }
        >
          {total > 0 ? fmtNum(total) : "—"}
        </span>
      </a>
    </li>
  );
}

function LaneBar({
  intensity,
  colorClass,
}: {
  intensity: number;
  colorClass: string;
}) {
  // Vertical bar 14px max. Intensity 0 still renders a faint baseline
  // so the three lanes are visually consistent across cells.
  const heightPx = Math.max(2, Math.round(intensity * 14));
  const opacity = intensity === 0 ? 0.15 : 0.4 + intensity * 0.6;
  return (
    <span
      aria-hidden="true"
      className={`inline-block w-1 rounded-sm ${colorClass}`}
      style={{ height: `${heightPx}px`, opacity }}
    />
  );
}

function WorthNoticing({ items }: { items: DiscoverItem[] }) {
  if (items.length === 0) {
    return (
      <p className="mt-3 text-sm text-muted">
        Nothing surfaced yet. Discovery items appear once there&apos;s enough
        history to find dense periods, gaps, or review backlogs.
      </p>
    );
  }
  return (
    <ul className="mt-3 space-y-3">
      {items.map((item) => (
        <li
          key={item.id}
          className="rounded-xl border border-muted/15 bg-surface p-4"
        >
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="font-serif text-base">{item.title}</h3>
            <span
              className={
                "rounded-md px-2 py-0.5 text-[10px] font-medium " +
                STRENGTH_BADGE[item.signal_strength]
              }
            >
              {STRENGTH_LABEL[item.signal_strength]}
            </span>
          </div>
          <p className="mt-1 text-sm text-muted">{item.why_surfaced}</p>
          {item.action_href && (
            <a
              href={item.action_href}
              className="mt-2 inline-block text-sm text-accent underline-offset-4 hover:underline"
            >
              {item.suggested_action}
            </a>
          )}
        </li>
      ))}
    </ul>
  );
}
