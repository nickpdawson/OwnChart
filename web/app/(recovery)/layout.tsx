import { redirect } from "next/navigation";
import { getMe } from "@/lib/api";

// Recovery routes — terminal pages the AppLayout falls back to when
// the user's session is valid but the multi-tenant resolution fails.
//
// This group exists OUTSIDE the (app) group on purpose: it does not
// re-trigger the (app) layout's "redirect if no memberships /
// no active record" rules, so the user doesn't get stuck in a loop.
// Each page does its own minimal gating.

export default async function RecoveryLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const me = await getMe();
  if (!me) redirect("/login");
  if (!me.phi_consent_granted) redirect("/consent");
  return (
    <div className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center px-6 py-12">
      {children}
    </div>
  );
}
