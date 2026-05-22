"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { ActiveRecord, Membership } from "@/lib/api";

// Beta 1 Section B — Multi-tenant UI. The switcher anchors the
// sidebar above nav. It always shows which record is active and
// in what role; clicking opens a dropdown listing every record
// the user is a member of, so the user can switch without
// leaving the page.
//
// The switch POSTs to /api/auth/set-active-record, which (a)
// validates the user has a non-revoked membership on the target
// and (b) re-signs the session cookie so subsequent server-side
// fetches see the new active record. A router.refresh() then
// re-runs server components so the rest of the app picks up
// the change without a full reload.
//
// `me.memberships` may be empty if the user lost all memberships
// since the page loaded; the layout normally redirects in that
// case, but the component renders a "no records" stub defensively.

type Props = {
  active: ActiveRecord | null;
  memberships: Membership[];
};

function roleLabel(role: Membership["role"]): string {
  // PM A-5: the role label is informational. It shows the user
  // what privileges they have on the active record but does not
  // gate UI here — the backend gates writes via require_role.
  if (role === "owner") return "Owner";
  if (role === "caregiver") return "Caregiver";
  return "Viewer";
}

export function RecordSwitcher({ active, memberships }: Props) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [switching, setSwitching] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const handle = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, [open]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const handle = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", handle);
    return () => document.removeEventListener("keydown", handle);
  }, [open]);

  async function switchTo(personRecordId: string) {
    if (switching) return;
    if (active && active.id === personRecordId) {
      setOpen(false);
      return;
    }
    setSwitching(personRecordId);
    setError(null);
    try {
      const r = await fetch("/api/auth/set-active-record", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ person_record_id: personRecordId }),
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try {
          const body = await r.json();
          const detail = body?.detail;
          if (typeof detail === "object" && detail?.code) {
            msg =
              detail.code === "record_access_revoked"
                ? "That record is no longer available."
                : detail.code === "record_not_found"
                  ? "That record could not be found."
                  : detail.message || msg;
          } else if (typeof detail === "string") {
            msg = detail;
          }
        } catch {
          /* fall through with default msg */
        }
        throw new Error(msg);
      }
      setOpen(false);
      // Reload server components so the new cookie's active record
      // takes effect everywhere (timeline, dossier, ask, etc.).
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSwitching(null);
    }
  }

  if (memberships.length === 0) {
    // Defensive — layout normally redirects to /no-records.
    return (
      <div className="mt-6 rounded-md border border-muted/15 px-3 py-2 text-xs text-muted">
        No records available.
      </div>
    );
  }

  // If the user has exactly one record AND it's active, render a
  // simple label (no dropdown). Caregiver/owner of one record
  // is the common-case alpha shape; the dropdown is for multi-record.
  const onlyOne = memberships.length === 1;

  return (
    <div ref={containerRef} className="relative mt-6">
      <button
        type="button"
        onClick={() => !onlyOne && setOpen((p) => !p)}
        aria-haspopup={onlyOne ? undefined : "listbox"}
        aria-expanded={onlyOne ? undefined : open}
        disabled={onlyOne}
        className={
          "flex w-full items-center justify-between gap-2 rounded-md border border-muted/15 bg-bg px-3 py-2 text-left text-sm transition-colors " +
          (onlyOne ? "" : "hover:border-muted/30 hover:bg-bg/80")
        }
      >
        <div className="min-w-0 flex-1">
          <p className="truncate text-xs uppercase tracking-widest text-muted">
            Viewing
          </p>
          <p className="truncate font-medium text-ink">
            {active?.display_name ?? "Pick a record"}
          </p>
          {active && (
            <p className="truncate text-xs text-muted">
              {roleLabel(active.role)}
            </p>
          )}
        </div>
        {!onlyOne && (
          <svg
            width="14"
            height="14"
            viewBox="0 0 14 14"
            aria-hidden="true"
            className={"shrink-0 text-muted transition-transform " + (open ? "rotate-180" : "")}
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
        )}
      </button>

      {open && !onlyOne && (
        <div
          role="listbox"
          aria-label="Switch active record"
          className="absolute left-0 right-0 top-full z-50 mt-1 max-h-80 overflow-y-auto rounded-md border border-muted/15 bg-surface py-1 shadow-lg"
        >
          {memberships.map((m) => {
            const isActive = active?.id === m.person_record_id;
            const isSwitching = switching === m.person_record_id;
            return (
              <button
                key={m.person_record_id}
                type="button"
                role="option"
                aria-selected={isActive}
                disabled={isSwitching}
                onClick={() => switchTo(m.person_record_id)}
                className={
                  "flex w-full items-start justify-between gap-3 px-3 py-2 text-left text-sm transition-colors " +
                  (isActive
                    ? "bg-bg font-medium text-ink"
                    : "text-ink hover:bg-bg")
                }
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate">{m.display_name}</p>
                  <p className="truncate text-xs text-muted">
                    {roleLabel(m.role)}
                    {m.is_self && " · You"}
                  </p>
                </div>
                {isActive && (
                  <span aria-hidden="true" className="shrink-0 text-evidence">
                    ✓
                  </span>
                )}
                {isSwitching && (
                  <span className="shrink-0 text-xs text-muted">…</span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {error && (
        <p
          role="alert"
          className="mt-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning"
        >
          {error}
        </p>
      )}
    </div>
  );
}
