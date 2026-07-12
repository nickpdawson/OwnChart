"use client";

import Link from "next/link";
import { useState } from "react";
import type { AdminUserRow } from "@/lib/api";

// Batch C — the users table + management actions. Every mutation posts
// to same-origin `/api/admin/*` (the backend is the real gate) and then
// patches the local row so the table reflects the change without a full
// reload. Destructive actions confirm first.

function fmtUsd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function fmtDate(iso: string): string {
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

async function postAction(path: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await fetch(path, { method: "POST", credentials: "include" });
    if (!r.ok) {
      let msg = `Failed (${r.status}).`;
      try {
        const body = await r.json();
        if (body?.detail?.message) msg = body.detail.message;
        else if (typeof body?.detail === "string") msg = body.detail;
      } catch {
        /* default */
      }
      return { ok: false, error: msg };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
}

export function UsersClient({
  initial,
  total,
  selfId,
}: {
  initial: AdminUserRow[];
  total: number;
  selfId: string | null;
}) {
  const [users, setUsers] = useState<AdminUserRow[]>(initial);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function patchRow(id: string, patch: Partial<AdminUserRow>) {
    setUsers((prev) => prev.map((u) => (u.id === id ? { ...u, ...patch } : u)));
  }

  async function run(
    id: string,
    action: "disable" | "enable" | "make-admin" | "revoke-admin",
    confirmMsg: string | null,
    patch: Partial<AdminUserRow>,
  ) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    setBusyId(id);
    setError(null);
    const res = await postAction(`/api/admin/users/${id}/${action}`);
    setBusyId(null);
    if (!res.ok) {
      setError(res.error ?? "Action failed.");
      return;
    }
    patchRow(id, patch);
  }

  return (
    <div className="space-y-4">
      {error && (
        <p
          role="alert"
          className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning"
        >
          {error}
        </p>
      )}

      <p className="text-xs text-muted">
        {total} {total === 1 ? "account" : "accounts"} on this instance.
      </p>

      <div className="overflow-x-auto rounded-xl border border-muted/15 bg-surface">
        <table className="w-full min-w-[860px] text-sm">
          <thead className="border-b border-muted/15 text-left text-xs uppercase tracking-widest text-muted">
            <tr>
              <th className="px-3 py-2">Account</th>
              <th className="px-3 py-2">Records</th>
              <th className="px-3 py-2">Agreement</th>
              <th className="px-3 py-2 text-right">Spend (mo)</th>
              <th className="px-3 py-2">Joined</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-muted/10">
            {users.map((u) => {
              const isSelf = u.id === selfId;
              const busy = busyId === u.id;
              return (
                <tr key={u.id} className={u.disabled ? "bg-warning/5" : ""}>
                  <td className="px-3 py-2">
                    <Link
                      href={`/admin/users/${u.id}`}
                      className="font-medium text-ink hover:text-accent"
                    >
                      {u.email}
                    </Link>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {u.is_instance_admin && (
                        <span className="rounded bg-accent/10 px-1.5 py-0.5 text-xs text-accent">
                          admin
                        </span>
                      )}
                      {u.disabled && (
                        <span className="rounded bg-warning/10 px-1.5 py-0.5 text-xs text-warning">
                          disabled
                        </span>
                      )}
                      {!u.email_verified && (
                        <span className="rounded bg-muted/10 px-1.5 py-0.5 text-xs text-muted">
                          unverified
                        </span>
                      )}
                      {isSelf && (
                        <span className="rounded bg-muted/10 px-1.5 py-0.5 text-xs text-muted">
                          you
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2 tabular-nums text-muted">
                    {u.membership_count}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted">
                    {u.latest_agreement_version ? (
                      <>
                        <span className="text-ink">
                          {u.latest_agreement_version}
                        </span>
                        {u.latest_agreement_accepted_at && (
                          <div>{fmtDate(u.latest_agreement_accepted_at)}</div>
                        )}
                      </>
                    ) : (
                      <span className="text-muted/60">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {fmtUsd(u.month_to_date_spend_usd)}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap text-xs text-muted">
                    {fmtDate(u.created_at)}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap justify-end gap-1.5">
                      {u.disabled ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            run(u.id, "enable", null, { disabled: false })
                          }
                          className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
                        >
                          Enable
                        </button>
                      ) : (
                        <button
                          type="button"
                          disabled={busy || isSelf}
                          title={isSelf ? "You can't disable your own account" : undefined}
                          onClick={() =>
                            run(
                              u.id,
                              "disable",
                              `Disable ${u.email}? Their login and every signed-in device stop working until you re-enable them.`,
                              { disabled: true },
                            )
                          }
                          className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:border-warning/60 hover:text-warning disabled:opacity-40"
                        >
                          Disable
                        </button>
                      )}
                      {u.is_instance_admin ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            run(
                              u.id,
                              "revoke-admin",
                              `Revoke instance-admin from ${u.email}?`,
                              { is_instance_admin: false },
                            )
                          }
                          className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
                        >
                          Revoke admin
                        </button>
                      ) : (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            run(
                              u.id,
                              "make-admin",
                              `Grant instance-admin to ${u.email}? They'll be able to manage every account.`,
                              { is_instance_admin: true },
                            )
                          }
                          className="rounded-md border border-accent/30 px-2 py-1 text-xs text-accent hover:bg-accent/10 disabled:opacity-50"
                        >
                          Make admin
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
