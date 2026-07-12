import { redirect } from "next/navigation";
import { getMe } from "@/lib/api";
import { AdminTabs } from "./AdminTabs";

// Batch C — instance-admin section. The (app) layout already loaded
// /me and filtered the "Admin" nav link to admins; this is the
// second, authoritative client-side gate (the backend is the real
// one — every /api/admin/* route 403s a non-admin). A non-admin who
// hand-types /admin is bounced to their dashboard.
export const dynamic = "force-dynamic";

export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const me = await getMe();
  if (!me) redirect("/login");
  if (!me.is_instance_admin) redirect("/dashboard");

  return (
    <div className="max-w-6xl">
      <p className="text-sm uppercase tracking-widest text-muted">Instance admin</p>
      <h1 className="mt-2 font-serif text-3xl">Manage this OwnChart</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Everyone with an account on this instance, what they can access,
        and what the AI has cost this month. Actions here are audited.
      </p>
      <AdminTabs />
      <div className="mt-6">{children}</div>
    </div>
  );
}
