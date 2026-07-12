import Link from "next/link";
import { getAdminUsage } from "@/lib/api";

export const dynamic = "force-dynamic";

function fmtUsd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

export default async function AdminUsagePage() {
  const usage = await getAdminUsage();
  const max = Math.max(
    0,
    ...usage.per_user.map((u) => u.month_to_date_spend_usd),
  );

  return (
    <div className="space-y-6">
      <section className="rounded-xl border border-evidence/30 bg-evidence/5 p-4">
        <p className="text-xs uppercase tracking-widest text-evidence">
          Instance AI spend · this month
        </p>
        <p className="mt-1 font-serif text-3xl tabular-nums">
          {fmtUsd(usage.total_estimated_usd_cost)}
        </p>
        <p className="mt-1 text-xs text-muted">
          Since {fmtDate(usage.month_start)}. Resets {fmtDate(usage.resets_at)}.
          Estimated from public per-token pricing over every user&apos;s
          model runs.
        </p>
      </section>

      <section>
        <h2 className="font-serif text-lg">By user</h2>
        {usage.per_user.length === 0 ? (
          <p className="mt-2 text-sm text-muted">No users yet.</p>
        ) : (
          <ul className="mt-3 space-y-1.5">
            {usage.per_user.map((u) => {
              const pct = max > 0 ? (u.month_to_date_spend_usd / max) * 100 : 0;
              return (
                <li
                  key={u.user_id}
                  className="rounded-md border border-muted/15 bg-surface px-4 py-2.5"
                >
                  <div className="flex items-baseline justify-between gap-3">
                    <Link
                      href={`/admin/users/${u.user_id}`}
                      className="truncate text-sm text-ink hover:text-accent"
                    >
                      {u.email}
                    </Link>
                    <span className="shrink-0 tabular-nums text-sm font-medium">
                      {fmtUsd(u.month_to_date_spend_usd)}
                    </span>
                  </div>
                  <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-bg">
                    <div
                      className="h-full rounded-full bg-accent/60"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <p className="text-xs text-muted">
        Each user&apos;s own itemized rows (with CSV export) live under{" "}
        <Link href="/settings/providers/usage" className="underline hover:text-ink">
          Settings · AI providers · Usage
        </Link>
        .
      </p>
    </div>
  );
}
