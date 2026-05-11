"use client";

import { useState } from "react";
import type { SourceReviewSummary } from "@/lib/api";

// docs/07 §453-468: the source-level review callout. Replaces the
// "412 individual review items" cliff with one summary plus three
// bulk decisions. Provider/contact cleanup is the highest-volume
// case, so the primary surfaced action is "keep all as source-only."

async function bulkUpdate(
  sourceId: string,
  factType: string,
  assertionType: "annotate" | "reject",
  newReviewState: string | null,
): Promise<{ updated: number }> {
  // Resolve the relevant fact IDs for this source × fact_type, then
  // POST to /api/facts/bulk. The route already exists from #54.
  const idsRes = await fetch(
    `/api/facts?source_id=${encodeURIComponent(sourceId)}` +
      `&fact_type=${encodeURIComponent(factType)}` +
      `&review_state=needs_review&limit=500`,
    { credentials: "include" },
  );
  if (!idsRes.ok) throw new Error(`fact lookup failed: ${idsRes.status}`);
  type FactRow = { id: string };
  const facts = (await idsRes.json()) as FactRow[];
  const ids = facts.map((f) => f.id);
  if (ids.length === 0) return { updated: 0 };
  const r = await fetch("/api/facts/bulk", {
    method: "POST",
    headers: { "content-type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      fact_ids: ids,
      assertion_type: assertionType,
      new_review_state: newReviewState,
      reason: `source-level: ${factType} → ${newReviewState ?? assertionType}`,
    }),
  });
  if (!r.ok) throw new Error(`bulk update failed: HTTP ${r.status}`);
  return (await r.json()) as { updated: number };
}

function fmt(n: number): string {
  return n.toLocaleString();
}

export function ReviewSummary({
  sourceId,
  summary,
}: {
  sourceId: string;
  summary: SourceReviewSummary;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  async function run(
    factType: string,
    assertionType: "annotate" | "reject",
    newReviewState: string | null,
    label: string,
  ) {
    setBusy(true);
    setError(null);
    setToast(null);
    try {
      const { updated } = await bulkUpdate(
        sourceId,
        factType,
        assertionType,
        newReviewState,
      );
      setToast(`${label}: ${fmt(updated)} fact${updated === 1 ? "" : "s"} updated.`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const {
    total_facts,
    needs_review_count,
    timeline_relevant_needs_review,
    provider_contact_needs_review,
    confirmed_count,
    deferred_or_resolved_count,
  } = summary;

  // Nothing to triage at the source level — render a quiet
  // confirmation, not the action toolbar.
  if (needs_review_count === 0) {
    return (
      <section className="mt-10 rounded-xl border border-muted/15 bg-bg/40 p-5">
        <h2 className="font-serif text-xl">This source is fully reviewed</h2>
        <p className="mt-1 text-sm text-muted">
          {fmt(total_facts)} fact{total_facts === 1 ? "" : "s"} ·{" "}
          <span className="tabular-nums">{fmt(confirmed_count)}</span>{" "}
          confirmed, <span className="tabular-nums">{fmt(deferred_or_resolved_count)}</span>{" "}
          resolved. Nothing pending.
        </p>
      </section>
    );
  }

  return (
    <section className="mt-10 rounded-xl border border-caution/30 bg-caution/5 p-5">
      <h2 className="font-serif text-xl">Review at the source level</h2>
      <p className="mt-2 font-serif text-base leading-relaxed text-ink">
        This source produced{" "}
        <span className="tabular-nums">{fmt(total_facts)}</span> extracted
        fact{total_facts === 1 ? "" : "s"}.{" "}
        <span className="tabular-nums">{fmt(timeline_relevant_needs_review)}</span>{" "}
        appear timeline-relevant.{" "}
        <span className="tabular-nums">{fmt(provider_contact_needs_review)}</span>{" "}
        are provider / contact / source-context details.{" "}
        <span className="tabular-nums">{fmt(confirmed_count)}</span> are already
        confirmed.
      </p>
      <p className="mt-2 text-sm text-muted">
        Clearing the source-context items in bulk keeps them searchable from
        this page but stops them from cluttering the timeline, dossiers, and
        Review Inbox.
      </p>

      <div className="mt-4 flex flex-wrap gap-2 text-sm">
        {timeline_relevant_needs_review > 0 && (
          <a
            href={`/review?source_id=${encodeURIComponent(sourceId)}`}
            className="rounded-md bg-accent px-3 py-1.5 text-xs text-surface hover:opacity-90"
          >
            Review {fmt(timeline_relevant_needs_review)} timeline-relevant
          </a>
        )}
        {provider_contact_needs_review > 0 && (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                run(
                  "provider_relationship",
                  "annotate",
                  "source_only",
                  "Kept as source-only",
                )
              }
              className="rounded-md border border-evidence/40 px-3 py-1.5 text-xs text-evidence hover:bg-evidence/10 disabled:opacity-50"
            >
              Keep {fmt(provider_contact_needs_review)} provider/contact items
              as source-only
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                run(
                  "provider_relationship",
                  "annotate",
                  "deferred",
                  "Deferred",
                )
              }
              className="rounded-md border border-muted/30 px-3 py-1.5 text-xs hover:bg-muted/5 disabled:opacity-50"
            >
              Defer them
            </button>
          </>
        )}
      </div>
      {toast && (
        <p className="mt-3 text-xs text-accent">{toast}</p>
      )}
      {error && (
        <p className="mt-3 text-xs text-caution">Failed: {error}</p>
      )}
    </section>
  );
}
