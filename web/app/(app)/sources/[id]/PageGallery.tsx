"use client";

import { useEffect, useState } from "react";

type AnchorReadout = {
  id: string;
  anchor_type: string;
  page_number: number | null;
  section_path: string | null;
  text_excerpt: string | null;
};

export function PageGallery({
  sourceId,
  pageRenders,
}: {
  sourceId: string;
  pageRenders: { page: number }[];
}) {
  const [openPage, setOpenPage] = useState<number | null>(null);
  const [highlight, setHighlight] = useState<{ kind: "page" | "anchor"; key: string } | null>(null);
  const [anchors, setAnchors] = useState<AnchorReadout[]>([]);
  const [anchorsLoaded, setAnchorsLoaded] = useState(false);

  // Pull all evidence anchors for this source so we can show the
  // supporting text excerpts alongside each page.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/sources/${sourceId}/anchors`, {
          credentials: "include",
          cache: "no-store",
        });
        if (!cancelled && r.ok) {
          const list = (await r.json()) as AnchorReadout[];
          setAnchors(list);
        }
      } catch {
        /* ignore — page render still works without excerpts */
      } finally {
        if (!cancelled) setAnchorsLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sourceId]);

  // Honor the URL hash on mount: #page-N or #anchor-{id}. Scrolls the
  // matching card into view and flashes a temporary highlight so the
  // user sees what they were sent here for. CAIHL "why do you think
  // that?" deep-link landing.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const hash = window.location.hash || "";
    if (!hash) return;
    const m = hash.match(/^#(page|anchor)-(.+)$/);
    if (!m) return;
    const kind = m[1] as "page" | "anchor";
    const key = m[2];
    // Wait for the layout to settle before scrolling.
    const t = setTimeout(() => {
      const el = document.getElementById(hash.slice(1));
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        setHighlight({ kind, key });
        // Drop the highlight after a few seconds so the page returns to
        // its calm baseline.
        setTimeout(() => setHighlight(null), 3500);
      }
    }, 150);
    return () => clearTimeout(t);
  }, [anchorsLoaded]);

  // Group anchors by page for easy per-page rendering.
  const anchorsByPage = new Map<number, AnchorReadout[]>();
  for (const a of anchors) {
    if (a.page_number == null) continue;
    const arr = anchorsByPage.get(a.page_number) ?? [];
    arr.push(a);
    anchorsByPage.set(a.page_number, arr);
  }

  return (
    <>
      <ul className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
        {pageRenders.map((p) => {
          const anchorsHere = anchorsByPage.get(p.page) ?? [];
          const isHighlighted = highlight?.kind === "page" && highlight.key === String(p.page);
          return (
            <li
              key={p.page}
              id={`page-${p.page}`}
              className={`scroll-mt-24 rounded-xl border bg-surface p-3 transition-colors ${
                isHighlighted
                  ? "border-accent/70 ring-2 ring-accent/40"
                  : "border-muted/15"
              }`}
            >
              <button
                type="button"
                onClick={() => setOpenPage(p.page)}
                className="block w-full text-left"
                aria-label={`Enlarge page ${p.page}`}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={`/api/sources/${sourceId}/page/${p.page}`}
                  alt={`Page ${p.page}`}
                  className="aspect-[3/4] w-full rounded object-contain bg-bg"
                  loading="lazy"
                />
                <p className="mt-2 px-1 text-xs uppercase tracking-widest text-muted">page {p.page}</p>
              </button>
              {/* Per-page anchor excerpts — the actual quote that
                  grounds each fact extracted from this page. */}
              {anchorsHere.length > 0 && (
                <ul className="mt-2 space-y-2">
                  {anchorsHere.map((a) => {
                    const anchorHi = highlight?.kind === "anchor" && highlight.key === a.id;
                    return (
                      <li
                        key={a.id}
                        id={`anchor-${a.id}`}
                        className={`scroll-mt-24 rounded-md border-l-2 px-2 py-1.5 text-xs transition-colors ${
                          anchorHi
                            ? "border-accent bg-accent/10 text-ink"
                            : "border-muted/30 text-muted"
                        }`}
                      >
                        {a.text_excerpt ? (
                          <span className="italic">“{a.text_excerpt.slice(0, 280)}{a.text_excerpt.length > 280 ? "…" : ""}”</span>
                        ) : (
                          <span>(no excerpt captured for this anchor)</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
      </ul>

      {/* For non-paged anchors (CCDA sections, FHIR resource refs,
          image_full anchors) — list at the bottom so deep-link by id
          still works. */}
      {anchors.some((a) => a.page_number == null) && (
        <section className="mt-6">
          <p className="text-xs uppercase tracking-widest text-muted">Other anchors</p>
          <ul className="mt-2 space-y-2">
            {anchors
              .filter((a) => a.page_number == null)
              .map((a) => {
                const anchorHi = highlight?.kind === "anchor" && highlight.key === a.id;
                return (
                  <li
                    key={a.id}
                    id={`anchor-${a.id}`}
                    className={`scroll-mt-24 rounded-md border bg-surface p-3 text-xs transition-colors ${
                      anchorHi ? "border-accent ring-2 ring-accent/40" : "border-muted/15"
                    }`}
                  >
                    <p className="font-mono text-muted">
                      {a.anchor_type}
                      {a.section_path ? ` · ${a.section_path}` : ""}
                    </p>
                    {a.text_excerpt && (
                      <p className="mt-1 italic">
                        “{a.text_excerpt.slice(0, 480)}{a.text_excerpt.length > 480 ? "…" : ""}”
                      </p>
                    )}
                  </li>
                );
              })}
          </ul>
        </section>
      )}

      {openPage !== null && (
        <div
          role="dialog"
          aria-modal="true"
          onClick={() => setOpenPage(null)}
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/70 p-4"
        >
          <button
            type="button"
            onClick={() => setOpenPage(null)}
            aria-label="Close"
            className="absolute right-4 top-4 rounded-md bg-surface px-3 py-1.5 text-sm"
          >
            Close
          </button>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={`/api/sources/${sourceId}/page/${openPage}`}
            alt={`Page ${openPage} (full)`}
            className="max-h-full max-w-full rounded-md bg-white"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </>
  );
}
