import Link from "next/link";
import { getAdminOverview } from "@/lib/api";

export const dynamic = "force-dynamic";

function fmtUsd(n: number): string {
  if (n < 0.01 && n > 0) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

export default async function AdminOverviewPage() {
  const o = await getAdminOverview();

  const stats: { label: string; value: string; note?: string }[] = [
    { label: "Users", value: o.user_count.toLocaleString() },
    { label: "Instance admins", value: o.admin_count.toLocaleString() },
    {
      label: "Disabled",
      value: o.disabled_user_count.toLocaleString(),
      note: "accounts frozen",
    },
    { label: "Records", value: o.record_count.toLocaleString() },
    {
      label: "Active memberships",
      value: o.active_membership_count.toLocaleString(),
    },
    {
      label: "Pending invites",
      value: o.pending_invitation_count.toLocaleString(),
    },
    {
      label: "AI spend (this month)",
      value: fmtUsd(o.month_to_date_spend_usd),
      note: "across all users",
    },
  ];

  return (
    <div className="space-y-6">
      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {stats.map((s) => (
          <div
            key={s.label}
            className="rounded-xl border border-muted/15 bg-surface p-4"
          >
            <p className="text-xs uppercase tracking-widest text-muted">
              {s.label}
            </p>
            <p className="mt-1 font-serif text-2xl tabular-nums">{s.value}</p>
            {s.note && <p className="mt-0.5 text-xs text-muted">{s.note}</p>}
          </div>
        ))}
      </section>

      <section className="flex flex-wrap gap-2">
        <Link
          href="/admin/users"
          className="rounded-md border border-accent/40 bg-accent/10 px-3 py-1.5 text-sm text-accent hover:bg-accent/20"
        >
          Manage users
        </Link>
        <Link
          href="/admin/usage"
          className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5"
        >
          Instance usage
        </Link>
        <Link
          href="/admin/invitations"
          className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5"
        >
          Invitations
        </Link>
      </section>
    </div>
  );
}
