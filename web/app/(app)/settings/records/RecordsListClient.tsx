"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type { Membership } from "@/lib/api";

// Management view of the user's memberships. Reuses the same
// POST /api/auth/set-active-record endpoint as the sidebar
// switcher, but renders a full card per record + role label +
// "Switch" action so the user can audit who they have access
// to and switch from one place.

function roleLabel(role: Membership["role"]): string {
  if (role === "owner") return "Owner";
  if (role === "caregiver") return "Caregiver";
  return "Viewer";
}

function roleBlurb(role: Membership["role"]): string {
  if (role === "owner")
    return "You can read, add, edit, and manage who has access.";
  if (role === "caregiver") return "You can read and add data.";
  return "You can read this record but cannot edit.";
}

export function RecordsListClient({
  memberships,
  activeRecordId,
}: {
  memberships: Membership[];
  activeRecordId: string | null;
}) {
  const router = useRouter();
  const [switching, setSwitching] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function switchTo(id: string) {
    if (switching) return;
    if (id === activeRecordId) return;
    setSwitching(id);
    setError(null);
    try {
      const r = await fetch("/api/auth/set-active-record", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ person_record_id: id }),
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try {
          const body = await r.json();
          const detail = body?.detail;
          if (typeof detail === "object" && detail?.message) {
            msg = detail.message;
          }
        } catch {
          /* fall through */
        }
        throw new Error(msg);
      }
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSwitching(null);
    }
  }

  if (memberships.length === 0) {
    return (
      <div className="mt-8 rounded-md border border-muted/15 bg-surface p-6 text-sm text-muted">
        You don&apos;t have access to any records right now. Contact your
        instance admin to be added.
      </div>
    );
  }

  return (
    <div className="mt-8 space-y-3">
      {memberships.map((m) => {
        const isActive = m.person_record_id === activeRecordId;
        const isSwitching = switching === m.person_record_id;
        return (
          <div
            key={m.person_record_id}
            className={
              "flex items-start justify-between gap-4 rounded-md border bg-surface p-4 " +
              (isActive ? "border-evidence/40" : "border-muted/15")
            }
          >
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium text-ink">{m.display_name}</p>
              <p className="mt-0.5 text-sm text-muted">
                {roleLabel(m.role)}
                {m.is_self && " · This is your record"}
              </p>
              <p className="mt-1 text-xs text-muted">{roleBlurb(m.role)}</p>
            </div>
            <div className="shrink-0">
              {isActive ? (
                <span
                  aria-label="Currently active"
                  className="rounded-md border border-evidence/30 bg-evidence/10 px-3 py-1.5 text-xs font-medium text-evidence"
                >
                  Active
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => switchTo(m.person_record_id)}
                  disabled={isSwitching || switching !== null}
                  className="rounded-md border border-muted/30 px-3 py-1.5 text-sm transition-colors hover:border-muted/60 hover:text-ink disabled:opacity-60"
                >
                  {isSwitching ? "Switching…" : "Switch to"}
                </button>
              )}
            </div>
          </div>
        );
      })}
      {error && (
        <p
          role="alert"
          className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning"
        >
          {error}
        </p>
      )}
    </div>
  );
}
