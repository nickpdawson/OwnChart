"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

// Batch C — management actions on the user-detail page. Same same-origin
// POSTs as the users table; on success we refresh the server component so
// the badges/stats re-render from the source of truth.

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

export function UserDetailActions({
  userId,
  email,
  disabled,
  isAdmin,
  isSelf,
}: {
  userId: string;
  email: string;
  disabled: boolean;
  isAdmin: boolean;
  isSelf: boolean;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(
    action: "disable" | "enable" | "make-admin" | "revoke-admin",
    confirmMsg: string | null,
  ) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    setBusy(true);
    setError(null);
    const res = await postAction(`/api/admin/users/${userId}/${action}`);
    setBusy(false);
    if (!res.ok) {
      setError(res.error ?? "Action failed.");
      return;
    }
    router.refresh();
  }

  return (
    <section className="rounded-xl border border-muted/15 bg-surface p-4">
      <div className="flex flex-wrap gap-2">
        {disabled ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => run("enable", null)}
            className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5 disabled:opacity-50"
          >
            Enable account
          </button>
        ) : (
          <button
            type="button"
            disabled={busy || isSelf}
            title={isSelf ? "You can't disable your own account" : undefined}
            onClick={() =>
              run(
                "disable",
                `Disable ${email}? Their login and every signed-in device stop working until you re-enable them.`,
              )
            }
            className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:border-warning/60 hover:text-warning disabled:opacity-40"
          >
            Disable account
          </button>
        )}
        {isAdmin ? (
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              run("revoke-admin", `Revoke instance-admin from ${email}?`)
            }
            className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5 disabled:opacity-50"
          >
            Revoke admin
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              run(
                "make-admin",
                `Grant instance-admin to ${email}? They'll be able to manage every account.`,
              )
            }
            className="rounded-md border border-accent/30 px-3 py-1.5 text-sm text-accent hover:bg-accent/10 disabled:opacity-50"
          >
            Make admin
          </button>
        )}
      </div>
      {error && (
        <p
          role="alert"
          className="mt-3 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning"
        >
          {error}
        </p>
      )}
    </section>
  );
}
