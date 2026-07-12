"use client";

import { useState } from "react";
import type { AdminInvite } from "@/lib/api";

// Batch C — instance-wide invitation oversight. Revoke posts to the
// admin endpoint (POST /api/admin/invitations/{id}/revoke), which can
// revoke ANY invite on the instance (the settings/records DELETE path is
// scoped to creator/owner). On success we patch the row to "revoked".

function statusClass(s: AdminInvite["status"]): string {
  if (s === "active") return "border-evidence/30 bg-evidence/10 text-evidence";
  if (s === "accepted") return "border-muted/30 bg-bg/40 text-muted";
  return "border-muted/15 bg-bg/30 text-muted";
}

function statusLabel(s: AdminInvite["status"]): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString();
  } catch {
    return iso;
  }
}

export function AdminInvitationsClient({
  initial,
}: {
  initial: AdminInvite[];
}) {
  const [invites, setInvites] = useState<AdminInvite[]>(initial);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function revoke(inv: AdminInvite) {
    if (
      !confirm(
        `Revoke the invite to ${inv.invited_email}? They won't be able to use it.`,
      )
    ) {
      return;
    }
    setBusyId(inv.id);
    setError(null);
    try {
      const r = await fetch(`/api/admin/invitations/${inv.id}/revoke`, {
        method: "POST",
        credentials: "include",
      });
      if (!r.ok) {
        let msg = `Revoke failed (${r.status}).`;
        try {
          const body = await r.json();
          if (body?.detail?.message) msg = body.detail.message;
        } catch {
          /* default */
        }
        setError(msg);
        return;
      }
      const updated = (await r.json()) as AdminInvite;
      setInvites((prev) =>
        prev.map((i) => (i.id === inv.id ? updated : i)),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  if (invites.length === 0) {
    return (
      <p className="mt-3 text-sm text-muted">
        No invitations have been issued on this instance yet.
      </p>
    );
  }

  return (
    <div className="mt-3 space-y-2">
      {error && (
        <p
          role="alert"
          className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning"
        >
          {error}
        </p>
      )}
      <ul className="space-y-2">
        {invites.map((inv) => (
          <li
            key={inv.id}
            className="flex items-start justify-between gap-3 rounded-md border border-muted/15 bg-surface p-3"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-ink">
                {inv.invited_email}
              </p>
              <p className="mt-0.5 text-xs text-muted">
                {inv.target_kind === "new_record"
                  ? "Own new record"
                  : "Joins existing record"}
                {" · "}
                {inv.role}
                {" · expires "}
                {fmtDate(inv.expires_at)}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span
                className={
                  "rounded-md border px-2 py-1 text-xs " +
                  statusClass(inv.status)
                }
              >
                {statusLabel(inv.status)}
              </span>
              {inv.status === "active" && (
                <button
                  type="button"
                  disabled={busyId === inv.id}
                  onClick={() => revoke(inv)}
                  className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:border-warning/60 hover:text-warning disabled:opacity-50"
                >
                  Revoke
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
