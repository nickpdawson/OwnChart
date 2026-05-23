"use client";

import { useState } from "react";
import type { UndatedHistoryGroup } from "@/lib/api";

// Section C Phase 1. Surfaces facts whose source carried no explicit
// occurrence date (typical case: a FHIR Condition with only a
// `recordedDate`, which we deliberately do NOT promote to an event
// date). Before Phase 1 these were silently dropped from Timeline;
// now they appear in this collapsed-by-default group.
//
// Clicking a row opens the existing fact-detail surface where the
// user can correct the date via the existing UserAssertion flow.

export function UndatedHistoryCard({ group }: { group: UndatedHistoryGroup }) {
  const [open, setOpen] = useState(false);
  if (group.total === 0) return null;
  return (
    <section className="mt-8 rounded-md border border-muted/15 bg-surface/60">
      <button
        type="button"
        onClick={() => setOpen((p) => !p)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left hover:bg-surface"
      >
        <div className="min-w-0">
          <p className="font-medium text-ink">Undated history</p>
          <p className="mt-0.5 text-xs text-muted">
            {group.total} fact{group.total === 1 ? "" : "s"} from your
            records whose source didn&rsquo;t carry an explicit
            occurrence date. Open to review and add a date if you know
            when each happened.
          </p>
        </div>
        <svg
          width="14"
          height="14"
          viewBox="0 0 14 14"
          aria-hidden="true"
          className={
            "shrink-0 text-muted transition-transform " +
            (open ? "rotate-180" : "")
          }
        >
          <path
            d="M3 5l4 4 4-4"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            fill="none"
          />
        </svg>
      </button>
      {open && (
        <ul className="divide-y divide-muted/10 border-t border-muted/15">
          {group.items.map((it) => (
            <li key={it.fact_id}>
              <a
                href={`/?fact=${it.fact_id}`}
                className="flex items-baseline justify-between gap-3 px-4 py-2.5 text-sm hover:bg-bg/40"
              >
                <span className="min-w-0 flex-1 truncate text-ink">
                  {it.display_label || it.label}
                </span>
                <span className="shrink-0 text-xs text-muted">
                  {it.fact_type}
                  {it.source_label && (
                    <span className="ml-2">· {it.source_label}</span>
                  )}
                </span>
              </a>
            </li>
          ))}
          {group.total > group.items.length && (
            <li className="px-4 py-2.5 text-xs text-muted">
              + {group.total - group.items.length} more …
            </li>
          )}
        </ul>
      )}
    </section>
  );
}
