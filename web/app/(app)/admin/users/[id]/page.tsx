import Link from "next/link";
import { notFound } from "next/navigation";
import { getAdminUserDetail, getMe } from "@/lib/api";
import { UserDetailActions } from "./UserDetailActions";

type Params = { id: string };

export const dynamic = "force-dynamic";

function fmtUsd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default async function AdminUserDetailPage({
  params,
}: {
  params: Promise<Params>;
}) {
  const { id } = await params;
  const [me, user] = await Promise.all([getMe(), getAdminUserDetail(id)]);
  if (!user) notFound();

  const isSelf = user.id === (me?.id ?? null);

  return (
    <div className="max-w-3xl space-y-6">
      <p className="text-sm text-muted">
        <Link href="/admin/users" className="hover:text-ink">
          ← All users
        </Link>
      </p>

      <div>
        <h2 className="font-serif text-2xl">{user.email}</h2>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {user.is_instance_admin && (
            <span className="rounded bg-accent/10 px-1.5 py-0.5 text-xs text-accent">
              instance admin
            </span>
          )}
          {user.disabled && (
            <span className="rounded bg-warning/10 px-1.5 py-0.5 text-xs text-warning">
              disabled
            </span>
          )}
          <span className="rounded bg-muted/10 px-1.5 py-0.5 text-xs text-muted">
            {user.email_verified ? "email verified" : "email unverified"}
          </span>
          {isSelf && (
            <span className="rounded bg-muted/10 px-1.5 py-0.5 text-xs text-muted">
              this is you
            </span>
          )}
        </div>
      </div>

      <UserDetailActions
        userId={user.id}
        email={user.email}
        disabled={user.disabled}
        isAdmin={user.is_instance_admin}
        isSelf={isSelf}
      />

      <section className="grid gap-3 sm:grid-cols-3">
        <Stat label="Joined" value={fmtDate(user.created_at)} />
        <Stat
          label="Active devices"
          value={user.device_token_count.toLocaleString()}
        />
        <Stat
          label="Spend (this month)"
          value={fmtUsd(user.month_to_date_spend_usd)}
        />
      </section>

      <section>
        <h3 className="text-xs uppercase tracking-widest text-muted">
          Agreement
        </h3>
        {user.latest_agreement_version ? (
          <p className="mt-2 text-sm">
            <span className="text-ink">{user.latest_agreement_version}</span>{" "}
            <span className="text-muted">
              accepted {fmtDate(user.latest_agreement_accepted_at)}
            </span>
          </p>
        ) : (
          <p className="mt-2 text-sm text-muted">No agreement recorded.</p>
        )}
      </section>

      <section>
        <h3 className="text-xs uppercase tracking-widest text-muted">
          Records ({user.membership_count} active)
        </h3>
        {user.memberships.length === 0 ? (
          <p className="mt-2 text-sm text-muted">No memberships.</p>
        ) : (
          <ul className="mt-2 divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {user.memberships.map((m) => (
              <li
                key={m.person_record_id}
                className="flex items-center justify-between px-4 py-2 text-sm"
              >
                <span className={m.revoked ? "text-muted line-through" : "text-ink"}>
                  {m.display_name}
                </span>
                <span className="flex items-center gap-2 text-xs">
                  <span className="text-muted">{m.role}</span>
                  {m.revoked && (
                    <span className="rounded bg-muted/10 px-1.5 py-0.5 text-muted">
                      revoked
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3 className="text-xs uppercase tracking-widest text-muted">
          AI usage this month
        </h3>
        {user.per_purpose.length === 0 ? (
          <p className="mt-2 text-sm text-muted">
            No model runs this month.
          </p>
        ) : (
          <div className="mt-2 grid gap-4 sm:grid-cols-2">
            <Breakdown title="By purpose" items={user.per_purpose} />
            <Breakdown title="By model" items={user.per_model} />
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-muted/15 bg-surface p-4">
      <p className="text-xs uppercase tracking-widest text-muted">{label}</p>
      <p className="mt-1 font-serif text-xl tabular-nums">{value}</p>
    </div>
  );
}

function Breakdown({
  title,
  items,
}: {
  title: string;
  items: { key: string; runs: number; estimated_usd_cost: number }[];
}) {
  return (
    <div className="rounded-xl border border-muted/15 bg-surface">
      <p className="border-b border-muted/10 px-4 py-2 text-xs uppercase tracking-widest text-muted">
        {title}
      </p>
      <ul className="divide-y divide-muted/10">
        {items.map((it) => (
          <li
            key={it.key}
            className="flex items-baseline justify-between px-4 py-2 text-sm"
          >
            <span className="font-mono text-xs">{it.key}</span>
            <span className="flex items-baseline gap-3 text-xs text-muted">
              <span>{it.runs} runs</span>
              <span className="font-medium text-ink">
                {fmtUsd(it.estimated_usd_cost)}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
