import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getSourceContributionSummary,
  getSourceDetail,
  getSourceReviewSummary,
  listFactsForSource,
  listSourceCandidates,
} from "@/lib/api";
import { ContributionLead } from "./ContributionLead";
import { ExtractFactsButton } from "./ExtractFactsButton";
import { MakeSenseButton } from "./MakeSenseButton";
import { PageGallery } from "./PageGallery";
import { ReviewSummary } from "./ReviewSummary";

type Params = { id: string };
type Search = { show?: string };

export const dynamic = "force-dynamic";

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return String(iso);
  }
}

export default async function SourceDetailPage({
  params,
  searchParams,
}: {
  params: Promise<Params>;
  searchParams: Promise<Search>;
}) {
  const { id } = await params;
  const { show } = await searchParams;
  const showSourceOnly = show === "source-only";
  let src;
  try {
    src = await getSourceDetail(id);
  } catch (e) {
    if ((e as Error).message.includes("404")) notFound();
    throw e;
  }
  const facts = await listFactsForSource(id, {
    includeSourceOnly: showSourceOnly,
  }).catch(() => []);
  const reviewSummary = await getSourceReviewSummary(id).catch(() => null);
  const contribution = await getSourceContributionSummary(id).catch(() => null);
  const candidates = await listSourceCandidates(id).catch(() => []);

  const meta = src.raw_metadata || {};
  const pageRenders = (meta as { page_renders?: { page: number }[] }).page_renders || [];
  const ocr = (meta as { ocr?: { ran?: boolean; total_words?: number; mean_confidence?: number | null } }).ocr;
  const isPdfish = src.source_type === "pdf" || src.source_type === "fax_pdf";

  const grouped = new Map<string, typeof facts>();
  for (const c of facts) {
    const arr = grouped.get(c.fact_type) ?? [];
    arr.push(c);
    grouped.set(c.fact_type, arr);
  }
  const groupOrder = ["procedure", "condition", "medication", "encounter", "symptom", "observation", "provider_relationship", "life_context_event"];

  return (
    <div className="max-w-5xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <Link href="/sources" className="hover:underline">Evidence vault</Link> · source
      </p>
      <h1 className="mt-2 font-serif text-2xl">
        {contribution?.source_name ||
          src.user_supplied_caption ||
          src.original_filename ||
          "(untitled)"}
      </h1>

      {contribution ? (
        <ContributionLead contribution={contribution} />
      ) : (
        <p className="mt-3 text-sm text-muted">
          OwnChart couldn&apos;t generate a contribution summary for this source.
        </p>
      )}

      <MakeSenseButton sourceId={src.id} initialCandidates={candidates} />

      {/* Technical metadata behind a disclosure per docs/07 R2: the
          source page leads with what this source *contributed*, not
          with file-inspector content. Provenance and integrity stay
          available — they're just no longer the headline. */}
      <details className="mt-4 rounded-lg border border-muted/15 bg-bg/50">
        <summary className="cursor-pointer px-4 py-2 text-xs uppercase tracking-widest text-muted hover:text-ink">
          Provenance and integrity
        </summary>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 px-4 pb-4 pt-2 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-xs uppercase tracking-widest text-muted">Type</dt>
            <dd>{src.source_type}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-widest text-muted">MIME</dt>
            <dd className="break-all">{src.mime_type || "—"}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-widest text-muted">Acquired</dt>
            <dd>{fmtDate(src.acquired_at)}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-widest text-muted">Captured</dt>
            <dd>{fmtDate(src.captured_at)}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-widest text-muted">Event date</dt>
            <dd>{fmtDate(src.user_supplied_event_date)}</dd>
          </div>
          <div className="col-span-2 sm:col-span-3">
            <dt className="text-xs uppercase tracking-widest text-muted">SHA-256</dt>
            <dd className="break-all font-mono text-xs">{src.hash}</dd>
          </div>
        </dl>
      </details>

      {src.has_gps && (
        <p className="mt-4 rounded-md border border-caution/30 bg-caution/10 p-3 text-sm">
          ⚠ This image carries EXIF GPS coordinates. The original is stored as-is; redaction is a V1.1 feature.
        </p>
      )}

      {isPdfish && (
        <section className="mt-8 rounded-xl border border-muted/15 bg-surface p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-serif text-xl">Extraction</h2>
              <p className="mt-1 text-sm text-muted">
                {ocr?.ran
                  ? `Local OCR ran on upload — ~${ocr.total_words ?? 0} words across ${pageRenders.length} page${pageRenders.length === 1 ? "" : "s"}`
                  : src.source_type === "pdf"
                    ? "PDF has a text layer; OCR not needed."
                    : "OCR has not run yet."}
                {ocr?.mean_confidence != null ? ` · mean confidence ~${Math.round(ocr.mean_confidence)}%` : ""}
              </p>
            </div>
            <ExtractFactsButton sourceId={src.id} />
          </div>
          <p className="mt-3 text-xs text-muted">
            Vision extraction sends each page image to Claude (consent-gated). Each page produces structured
            facts with their own audit row in <code>model_runs</code>. The system auto-classifies template
            noise as <em>deferred</em> (hidden) and machine-extracted facts as <em>needs_review</em>; you can
            correct or reject anything from any view.
          </p>
        </section>
      )}

      {pageRenders.length > 0 && (
        <section className="mt-8">
          <h2 className="font-serif text-xl">Pages ({pageRenders.length})</h2>
          <PageGallery sourceId={src.id} pageRenders={pageRenders} />
        </section>
      )}

      {reviewSummary && reviewSummary.total_facts > 0 && (
        <ReviewSummary sourceId={src.id} summary={reviewSummary} />
      )}

      {facts.length > 0 && (
        <section className="mt-10">
          <div className="flex flex-wrap items-baseline justify-between gap-3">
            <h2 className="font-serif text-xl">
              Facts from this source ({facts.length})
            </h2>
            {/* Q8 (2026-05-11): source_only facts are hidden by default;
                this toggle is the only surface that reveals them. */}
            <a
              href={
                showSourceOnly
                  ? `?`
                  : `?show=source-only`
              }
              className="text-xs text-muted underline-offset-4 hover:underline"
            >
              {showSourceOnly
                ? "Hide source-only facts"
                : "Show source-only facts"}
            </a>
          </div>
          <div className="mt-4 space-y-6">
            {groupOrder.map((key) => {
              const arr = grouped.get(key);
              if (!arr || arr.length === 0) return null;
              const niceLabel = key.replace(/_/g, " ");
              return (
                <div key={key}>
                  <p className="text-xs uppercase tracking-widest text-muted">
                    {niceLabel} ({arr.length})
                  </p>
                  <ul className="mt-2 divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
                    {arr.map((c) => {
                      const display = c.canonical_label || c.label;
                      const corrected = c.canonical_label && c.canonical_label !== c.label;
                      return (
                        <li key={c.id} className="px-4 py-3">
                          <div className="flex flex-wrap items-baseline gap-2">
                            <p className="font-medium">{display}</p>
                            {corrected && (
                              <span className="rounded-md bg-evidence/15 px-1.5 py-0.5 text-xs text-evidence">
                                corrected
                              </span>
                            )}
                            <span className="text-xs text-muted">
                              {fmtDate(c.canonical_date_start || c.date_start)}
                              {c.confidence !== null ? ` · conf ${c.confidence}` : ""}
                              {` · ${c.review_state}`}
                            </span>
                          </div>
                          {(c.canonical_description || c.description) && (
                            <p className="mt-1 text-sm text-muted">
                              {c.canonical_description || c.description}
                            </p>
                          )}
                          <p className="mt-0.5 text-xs text-muted">
                            via {c.extraction_method}
                          </p>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {src.exif_metadata && (
        <section className="mt-8">
          <h2 className="font-serif text-xl">EXIF</h2>
          <pre className="mt-3 max-h-80 overflow-auto rounded-xl border border-muted/15 bg-surface p-4 text-xs">
            {JSON.stringify(src.exif_metadata, null, 2)}
          </pre>
        </section>
      )}
    </div>
  );
}
