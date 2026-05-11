// docs/07 R2 — the patient-meaningful lead on every source page.
// Replaces the file-inspector header (filename / MIME / SHA / 424
// facts) with what the source actually *added to your record*:
// one narrative paragraph + connected dossiers + the events worth
// noticing.

import Link from "next/link";
import type { SourceContributionSummary } from "@/lib/api";

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

function fmtNum(n: number): string {
  return n.toLocaleString();
}

// Tiny inline markdown: render **bold** spans inside the otherwise
// plain narrative paragraph. The backend marks the source name in
// bold; nothing else.
function MarkdownInline({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return (
    <>
      {parts.map((part, i) =>
        /^\*\*[^*]+\*\*$/.test(part) ? (
          <strong key={i} className="text-ink">
            {part.slice(2, -2)}
          </strong>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

export function ContributionLead({
  contribution,
}: {
  contribution: SourceContributionSummary;
}) {
  const {
    summary,
    total_facts,
    needs_review_count,
    fact_type_counts,
    date_min,
    date_max,
    top_events,
    dossier_linkages,
  } = contribution;

  // Order fact-type counts for the small footer line. Care-meaningful
  // types lead, "soft" types (observation/symptom) at the tail.
  const orderedTypes = [
    "procedure",
    "condition",
    "medication",
    "encounter",
    "lab_result",
    "imaging_study",
    "observation",
    "symptom",
    "life_context_event",
    "provider_relationship",
  ].filter((t) => fact_type_counts[t] > 0);

  return (
    <div className="mt-4">
      {/* Narrative paragraph — the headline of the page. */}
      <p className="max-w-2xl font-serif text-lg leading-relaxed text-ink">
        <MarkdownInline text={summary} />
      </p>

      {/* Most-relevant contributions: dossier badges + top events. */}
      {dossier_linkages.length > 0 && (
        <div className="mt-5">
          <p className="text-xs uppercase tracking-widest text-muted">
            Most relevant to
          </p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {dossier_linkages.slice(0, 6).map((l) => (
              <li key={l.slug}>
                <Link
                  href={`/dossier/${l.slug}` as const}
                  className="inline-flex items-baseline gap-1.5 rounded-md border border-accent/30 bg-accent/5 px-2.5 py-1 text-sm text-accent hover:bg-accent/10"
                >
                  <span>{l.name}</span>
                  <span className="text-xs tabular-nums opacity-70">
                    {fmtNum(l.fact_count)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}

      {top_events.length > 0 && (
        <div className="mt-5">
          <p className="text-xs uppercase tracking-widest text-muted">
            Events this source anchors
          </p>
          <ul className="mt-2 divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {top_events.map((e) => (
              <li key={e.id} className="flex flex-wrap items-baseline gap-2 px-3 py-2 text-sm">
                <span className="text-[10px] uppercase tracking-widest text-muted">
                  {e.fact_type.replace(/_/g, " ")}
                </span>
                <span className="font-mono text-xs tabular-nums text-muted">
                  {fmtDate(e.date_start)}
                </span>
                <span className="font-medium">{e.display_label ?? e.label}</span>
                {e.review_state === "needs_review" && (
                  <span className="text-xs text-caution">· needs review</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Quiet footer — counts available for users who want the
          inventory but never the headline. */}
      <p className="mt-5 text-xs text-muted">
        <span className="tabular-nums">{fmtNum(total_facts)}</span> fact
        {total_facts === 1 ? "" : "s"} extracted
        {date_min && date_max && (
          <>
            {" "}· spans{" "}
            <span className="tabular-nums">
              {new Date(date_min).getUTCFullYear()}
              {new Date(date_min).getUTCFullYear() !==
                new Date(date_max).getUTCFullYear() && (
                <>–{new Date(date_max).getUTCFullYear()}</>
              )}
            </span>
          </>
        )}
        {orderedTypes.length > 0 && (
          <>
            {" "}·{" "}
            {orderedTypes
              .slice(0, 5)
              .map((t) => `${fmtNum(fact_type_counts[t])} ${t.replace(/_/g, " ")}`)
              .join(" · ")}
          </>
        )}
        {needs_review_count > 0 && (
          <>
            {" "}·{" "}
            <span className="text-caution">
              {fmtNum(needs_review_count)} needs review
            </span>
          </>
        )}
      </p>
    </div>
  );
}
