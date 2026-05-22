"use client";

import { useState } from "react";
import type { Membership } from "@/lib/api";
import { SignOutButton } from "../SignOutButton";

// Recovery-page picker. Same POST shape as the sidebar RecordSwitcher
// but lives outside the (app) shell because the user has no
// resolvable active record yet, so the shell can't render.
//
// On success we route to /dashboard with a full reload so server
// components on every page re-fetch with the new cookie.

function roleLabel(role: Membership["role"]): string {
  if (role === "owner") return "Owner";
  if (role === "caregiver") return "Caregiver";
  return "Viewer";
}

export function PickRecordClient({
  memberships,
}: {
  memberships: Membership[];
}) {
  const [picking, setPicking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function pick(id: string) {
    if (picking) return;
    setPicking(id);
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
      // Full reload so the layout's redirect logic re-evaluates
      // with the new cookie and we land on /dashboard cleanly.
      window.location.assign("/dashboard");
    } catch (e) {
      setError((e as Error).message);
      setPicking(null);
    }
  }

  return (
    <div className="mt-6">
      <ul className="space-y-2">
        {memberships.map((m) => {
          const busy = picking === m.person_record_id;
          return (
            <li key={m.person_record_id}>
              <button
                type="button"
                disabled={picking !== null}
                onClick={() => pick(m.person_record_id)}
                className="flex w-full items-center justify-between gap-3 rounded-md border border-muted/15 bg-surface px-4 py-3 text-left transition-colors hover:border-muted/30 disabled:opacity-60"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium text-ink">
                    {m.display_name}
                  </p>
                  <p className="truncate text-sm text-muted">
                    {roleLabel(m.role)}
                    {m.is_self && " · You"}
                  </p>
                </div>
                <span className="shrink-0 text-sm text-muted">
                  {busy ? "Loading…" : "Open →"}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {error && (
        <p
          role="alert"
          className="mt-4 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning"
        >
          {error}
        </p>
      )}
      <div className="mt-8 border-t border-muted/15 pt-6">
        <SignOutButton />
      </div>
    </div>
  );
}
