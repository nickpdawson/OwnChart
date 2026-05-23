"use client";

// docs/07 R3 — the patient-meaningful fact-context sidesheet.
//
// Mounted globally in (app)/layout.tsx. Reads ?fact=<id> from the
// URL: when set, fetches /api/facts/{id}/context and renders an
// overlay sheet. Closing clears the param (so the browser back
// button closes it). Deep-link: any surface can link to
// ?fact=<id> on the current page, and the same surface URL
// without ?fact= shows the underlying content.
//
// The point per the product/design team: clicking a fact never
// dumps the user into the Evidence Vault file-inspector. It opens
// here, explains what the fact is + what it's connected to + what
// the user can do next, with Source as one action among several.

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
// Type-only imports — `import type` keeps the client bundle from
// pulling api.ts's server-only `next/headers` dependency.
import type { FactContext, FactContextAction } from "@/lib/api";

// Significance taxonomy is duplicated here as a client-safe constant.
// The server-side source of truth is api/ownchart/canonical/significance.py
// (taxonomy is also declared in web/lib/api.ts for type checking, but
// importing the const from there would force a server module into
// the client bundle).
type Significance =
  | "major_event"
  | "major_diagnosis"
  | "major_procedure"
  | "major_medication"
  | "major_activity_lifestyle"
  | "background"
  | "source_only";

const SIGNIFICANCE_CHOICES: readonly Significance[] = [
  "major_event",
  "major_diagnosis",
  "major_procedure",
  "major_medication",
  "major_activity_lifestyle",
  "background",
  "source_only",
] as const;

const SIGNIFICANCE_LABEL: Record<string, string> = {
  major_event: "Major event",
  major_diagnosis: "Major diagnosis",
  major_procedure: "Major procedure",
  major_medication: "Major medication",
  major_activity_lifestyle: "Major activity / lifestyle",
  background: "Background",
  source_only: "Source-only",
};

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

function humanizeFactType(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function FactContextSheet() {
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();

  const factId = searchParams?.get("fact") ?? null;
  const [data, setData] = useState<FactContext | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!factId) {
      setData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      setData(null);
      try {
        const r = await fetch(`/api/facts/${encodeURIComponent(factId)}/context`, {
          credentials: "include",
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const out = (await r.json()) as FactContext;
        if (!cancelled) setData(out);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [factId]);

  // Focus the close button when the sheet opens (a11y) and trap
  // Escape to close.
  useEffect(() => {
    if (!factId) return;
    closeRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") close();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [factId]);

  function close() {
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    params.delete("fact");
    const qs = params.toString();
    // window.history (not router.replace) — typed-routes rejects
    // runtime path strings, and we only need URL state cleanup
    // here, not a Next-side navigation.
    const newUrl = qs ? `${pathname}?${qs}` : pathname;
    window.history.replaceState(null, "", newUrl);
    // Trigger re-render: force a popstate-style hook re-run so the
    // sheet unmounts.
    window.dispatchEvent(new PopStateEvent("popstate"));
  }

  async function applyAction(action: FactContextAction) {
    if (!data) return;
    if (action.href) {
      // action.href is a deterministic V1 route string from the
      // backend; bypass typed-routes by treating it as a plain URL.
      window.location.assign(action.href);
      return;
    }
    // In-place actions: confirm / edit / source_only. Edit triggers
    // a navigate to /review for V1 (no inline editor inside the
    // sheet yet); confirm and source_only do an immediate PATCH.
    setActing(true);
    setError(null);
    try {
      if (action.kind === "edit") {
        router.push("/review");
        return;
      }
      const body: Record<string, unknown> = {};
      if (action.kind === "confirm") {
        body.assertion_type = "confirm";
      } else if (action.kind === "source_only") {
        body.assertion_type = "annotate";
        body.new_review_state = "source_only";
      } else {
        return;
      }
      const r = await fetch(`/api/facts/${data.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      close();
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setActing(false);
    }
  }

  if (!factId) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="fact-context-title"
      className="fixed inset-0 z-50 flex"
    >
      {/* Scrim. Click closes. */}
      <button
        type="button"
        aria-label="Close fact context"
        onClick={close}
        className="absolute inset-0 bg-black/40"
      />
      {/* Sidesheet pane. */}
      <aside className="relative ml-auto h-full w-full max-w-xl overflow-y-auto bg-bg shadow-2xl">
        <div className="sticky top-0 z-10 flex items-baseline justify-between gap-3 border-b border-muted/15 bg-bg/95 px-6 py-4 backdrop-blur">
          <p className="text-[10px] uppercase tracking-widest text-muted">
            Fact context
          </p>
          <button
            ref={closeRef}
            type="button"
            onClick={close}
            className="rounded-md px-2 py-1 text-sm text-muted hover:bg-muted/10 hover:text-ink"
          >
            Close ✕
          </button>
        </div>

        <div className="px-6 py-5">
          {loading && <p className="text-sm text-muted">Loading context…</p>}
          {error && (
            <p className="text-sm text-caution">Failed to load: {error}</p>
          )}
          {data && (
            <FactContextBody
              data={data}
              acting={acting}
              apply={applyAction}
              onSignificanceChanged={(next) =>
                setData((prev) =>
                  prev
                    ? {
                        ...prev,
                        significance: next.significance,
                        significance_source: next.significance_source,
                      }
                    : prev,
                )
              }
            />
          )}
        </div>
      </aside>
    </div>
  );
}

function FactContextBody({
  data,
  acting,
  apply,
  onSignificanceChanged,
}: {
  data: FactContext;
  acting: boolean;
  apply: (a: FactContextAction) => void;
  onSignificanceChanged: (next: {
    significance: string;
    significance_source: string;
  }) => void;
}) {
  return (
    <>
      <p className="text-[10px] uppercase tracking-widest text-muted">
        {humanizeFactType(data.fact_type)}
      </p>
      <h2 id="fact-context-title" className="mt-1 font-serif text-2xl text-ink">
        {data.display_label ?? data.label}
      </h2>
      {data.display_label && data.display_label !== data.label && (
        // R5: when the patient-readable label differs from the raw
        // SNOMED/CPT source, surface the original quietly so the
        // user can verify provenance without losing the readable lead.
        <p className="mt-0.5 font-mono text-xs text-muted">
          original: {data.label}
        </p>
      )}
      <p className="mt-2 font-serif text-base leading-relaxed text-muted">
        {data.what_this_is}
      </p>

      <DateProvenanceLine
        factId={data.id}
        dateProvenance={data.date_provenance ?? null}
        dateStart={data.date_start}
        historicalStatus={data.historical_status ?? null}
      />

      {data.why_needs_review_text && (
        <p className="mt-3 rounded-lg border border-caution/30 bg-caution/5 p-3 text-sm italic text-caution">
          {data.why_needs_review_text}
        </p>
      )}

      <SignificanceControls
        factId={data.id}
        significance={data.significance}
        significanceSource={data.significance_source}
        onChanged={onSignificanceChanged}
      />

      {/* Action row — primary affordances per docs/07 R3. */}
      <div className="mt-5 flex flex-wrap gap-2">
        {data.suggested_actions.map((a) => (
          <button
            key={a.kind}
            type="button"
            disabled={acting}
            onClick={() => apply(a)}
            className={
              "rounded-md px-3 py-1.5 text-sm disabled:opacity-50 " +
              (a.kind === "confirm"
                ? "bg-accent text-surface hover:opacity-90"
                : a.kind === "source_only"
                  ? "border border-evidence/40 text-evidence hover:bg-evidence/10"
                  : "border border-muted/30 hover:bg-muted/5")
            }
          >
            {a.label}
          </button>
        ))}
      </div>

      {/* Episode hint when present. */}
      {data.episode && (
        <section className="mt-7">
          <p className="text-xs uppercase tracking-widest text-muted">
            Part of
          </p>
          <p className="mt-1 font-serif text-base">{data.episode.title}</p>
          {data.episode.date_start && (
            <p className="mt-0.5 text-xs text-muted">
              {fmtDate(data.episode.date_start)} ·{" "}
              <span className="tabular-nums">{data.episode.fact_count}</span>{" "}
              related facts
            </p>
          )}
        </section>
      )}

      {/* Related facts. Clicking a related fact swaps the sheet to it.
          Source_only facts already filtered server-side; what remains
          is ranked by significance descending. */}
      {data.related_facts.length > 0 && (
        <section className="mt-7">
          <p className="text-xs uppercase tracking-widest text-muted">
            Related ({data.related_facts.length})
          </p>
          <ul className="mt-2 divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {data.related_facts.slice(0, 8).map((r) => (
              <li key={r.id} className="px-3 py-2 text-sm">
                <RelatedFactLink id={r.id} fact_type={r.fact_type} label={r.label} display_label={r.display_label} date_start={r.date_start} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Also recorded by (Q7, 2026-05-11). Driven by equivalence_key —
          surfaces cross-source verification without polluting the main
          related list. */}
      {data.also_recorded_by && data.also_recorded_by.length > 0 && (
        <section className="mt-7">
          <p className="text-xs uppercase tracking-widest text-muted">
            Also recorded by
          </p>
          <ul className="mt-2 space-y-1 text-sm">
            {data.also_recorded_by.map((p) => (
              <li
                key={p.fact_id}
                className="flex flex-wrap items-baseline gap-2"
              >
                {p.source_id ? (
                  <a
                    href={`/sources/${p.source_id}`}
                    className="text-accent underline-offset-4 hover:underline"
                  >
                    {p.source_name ?? "another source"}
                  </a>
                ) : (
                  <span>{p.source_name ?? "another source"}</span>
                )}
                <span className="text-xs text-muted">
                  via {p.extraction_method.replace(/_/g, " ")}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Matching dossiers — quick jump. */}
      {data.matching_dossiers.length > 0 && (
        <section className="mt-7">
          <p className="text-xs uppercase tracking-widest text-muted">
            Appears in
          </p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {data.matching_dossiers.map((d) => (
              <li key={d.slug}>
                <a
                  href={`/dossier/${d.slug}`}
                  className="inline-block rounded-md border border-accent/30 bg-accent/5 px-2.5 py-1 text-sm text-accent hover:bg-accent/10"
                >
                  {d.name}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Source attribution + evidence footer. Always last —
          evidence is on demand, not the headline. */}
      {data.source.source_name && (
        <section className="mt-7">
          <p className="text-xs uppercase tracking-widest text-muted">
            Evidence
          </p>
          <p className="mt-1 text-sm">
            From{" "}
            {data.source.source_id ? (
              <a
                href={`/sources/${data.source.source_id}${
                  data.source.source_page
                    ? `#page-${data.source.source_page}`
                    : ""
                }`}
                className="text-accent underline-offset-4 hover:underline"
              >
                {data.source.source_name}
              </a>
            ) : (
              <span>{data.source.source_name}</span>
            )}
            {data.source.source_page && (
              <span className="text-muted"> · page {data.source.source_page}</span>
            )}
          </p>
        </section>
      )}

      {/* Quiet provenance footer for the inspector-minded — review
          state, extraction method, confidence — never the headline. */}
      <p className="mt-7 text-xs text-muted">
        {data.review_state.replace(/_/g, " ")} · via{" "}
        {data.extraction_method.replace(/_/g, " ")}
        {data.confidence != null && (
          <> · confidence <span className="tabular-nums">{data.confidence}</span></>
        )}
      </p>
    </>
  );
}

function RelatedFactLink({
  id,
  fact_type,
  label,
  display_label,
  date_start,
}: {
  id: string;
  fact_type: string;
  label: string;
  display_label?: string | null;
  date_start: string | null;
}) {
  const searchParams = useSearchParams();
  const pathname = usePathname();
  // Swap the sheet to this related fact without losing the current
  // page — same URL, new ?fact=. Stack-friendly via browser back.
  function swap() {
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    params.set("fact", id);
    window.history.pushState(null, "", `${pathname}?${params.toString()}`);
    // Trigger Next's nav listener so the sheet refetches.
    window.dispatchEvent(new PopStateEvent("popstate"));
  }
  return (
    <button
      type="button"
      onClick={swap}
      className="flex w-full flex-wrap items-baseline gap-2 text-left hover:opacity-80"
    >
      <span className="text-[10px] uppercase tracking-widest text-muted">
        {humanizeFactType(fact_type)}
      </span>
      <span className="font-medium">{display_label ?? label}</span>
      <span className="ml-auto text-xs tabular-nums text-muted">
        {fmtDate(date_start)}
      </span>
    </button>
  );
}

// Significance controls (2026-05-11) — user can promote / demote any
// fact. Submit writes through PATCH /api/facts/{id}/significance with
// significance_source='user', overriding heuristic + LLM.
function SignificanceControls({
  factId,
  significance,
  significanceSource,
  onChanged,
}: {
  factId: string;
  significance: string | null;
  significanceSource: string | null;
  onChanged: (next: { significance: string; significance_source: string }) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function set(next: Significance) {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(
        `/api/facts/${encodeURIComponent(factId)}/significance`,
        {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ significance: next }),
        },
      );
      if (!r.ok) throw new Error(await r.text());
      const out = (await r.json()) as {
        significance: string;
        significance_source: string;
      };
      onChanged(out);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const current = significance ?? "background";
  return (
    <section className="mt-5 rounded-lg border border-muted/15 bg-bg/40 p-3">
      <p className="text-[10px] uppercase tracking-widest text-muted">
        Significance{" "}
        {significanceSource && (
          <span className="ml-1 text-muted/80">· set by {significanceSource}</span>
        )}
      </p>
      <p className="mt-1 text-sm">
        <span className="font-medium text-ink">
          {SIGNIFICANCE_LABEL[current] ?? current}
        </span>
      </p>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {SIGNIFICANCE_CHOICES.map((s) => (
          <button
            key={s}
            type="button"
            disabled={busy || s === current}
            onClick={() => set(s)}
            className={
              "rounded-md px-2 py-0.5 text-xs disabled:opacity-50 " +
              (s === current
                ? "bg-accent/15 text-accent"
                : "border border-muted/30 hover:bg-muted/5")
            }
          >
            {SIGNIFICANCE_LABEL[s] ?? s}
          </button>
        ))}
      </div>
      {error && (
        <p className="mt-2 text-xs text-caution">Couldn&apos;t update: {error}</p>
      )}
    </section>
  );
}


// Section C Phase 1 — surfaces the date provenance line on the
// fact-context sidesheet. One short sentence per case, plus an
// always-available "Correct this date" link that drops into the
// existing UserAssertion correction surface (no new endpoint).
//
// Cases:
//   NULL date            → "Date unknown — mentioned in this record"
//   explicit             → no badge (the date itself is sufficient)
//   encounter_proximate  → "Documented during this visit"
//   issued_approximate   → "Approximate (report-issued date)"
//   user_canonical       → "You confirmed this date"
//
// historical_status renders a small low-contrast pill next to the
// label per PM confirmation #4. Resolved/inactive/remission stay
// retrievable; we don't grey out the row.

function DateProvenanceLine({
  factId,
  dateProvenance,
  dateStart,
  historicalStatus,
}: {
  factId: string;
  dateProvenance: string | null;
  dateStart: string | null;
  historicalStatus: string | null;
}) {
  const correctUrl = `/?fact=${factId}#correct-date`;
  let copy: string | null = null;
  if (dateStart === null) {
    copy = "Date unknown — mentioned in this record. Add a date if you remember when.";
  } else if (dateProvenance === "encounter_proximate") {
    copy = "Date inherited from the linked visit — not an explicit occurrence date.";
  } else if (dateProvenance === "issued_approximate") {
    copy = "Approximate — date is the report-issued timestamp.";
  } else if (dateProvenance === "user_canonical") {
    copy = "You confirmed this date.";
  }
  if (!copy && !historicalStatus) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      {copy && (
        <p className="rounded-md border border-muted/15 bg-bg/40 px-3 py-1.5 text-xs text-muted">
          {copy}{" "}
          <a
            href={correctUrl}
            className="underline decoration-dotted underline-offset-2 hover:text-ink"
          >
            Correct this date
          </a>
        </p>
      )}
      {historicalStatus && (
        <span className="rounded-md border border-muted/20 bg-bg/40 px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted">
          {historicalStatus}
        </span>
      )}
    </div>
  );
}
