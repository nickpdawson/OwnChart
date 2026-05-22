import { redirect } from "next/navigation";
import type { Route } from "next";
import { getInstanceInfo, getMe, listTopics } from "@/lib/api";
import { AppShell } from "./AppShell";
import { DemoBanner } from "./DemoBanner";
import { FactContextSheet } from "./FactContextSheet";

// The shell stays user-neutral: nothing here references any specific
// provider or topic. "Your topics" below the static nav lists whatever
// the user has actually created.
//
// Order reflects docs/06's IA: Timeline + Discover are the patient-
// facing heart of the product, alongside Dossiers and Ask. Evidence
// Vault, Connections, Review, and Audit are support systems and
// drop below a small visual divider. Dossiers are *lenses* over the
// global timeline, not the marquee — they sit after Discover.
const NAV: ReadonlyArray<{ href: Route; label: string }> = [
  { href: "/dashboard", label: "Home" },
  { href: "/chat", label: "Chat" },
  { href: "/timeline", label: "Timeline" },
  { href: "/discover", label: "Discover" },
  { href: "/topics", label: "Dossiers" },
  { href: "/ask", label: "Ask the records" },
  { href: "/sources", label: "Evidence vault" },
  { href: "/connectors", label: "EHR connections" },
  { href: "/review", label: "Review inbox" },
  { href: "/audit", label: "Audit log" },
  { href: "/settings", label: "Settings" },
] as const;

export default async function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const me = await getMe();
  if (!me) redirect("/login");
  if (!me.phi_consent_granted) redirect("/consent");

  // Beta 1 Section B (M02) — recovery routing for the multi-tenant
  // surfaces. The PM A-5 contract is that the session stays valid
  // in both of these cases; the client routes the user somewhere
  // they can recover, instead of crashing into a 403 wall.
  //
  //   - Zero memberships  → /no-records (terminal "contact admin").
  //   - Memberships > 0 but no resolved active record (their session
  //     pin pointed at a now-revoked record) → /pick-record so they
  //     can explicitly choose a different record.
  const memberships = me.memberships ?? [];
  if (memberships.length === 0) redirect("/no-records");
  if (!me.active_record) redirect("/pick-record");

  const [topics, instance] = await Promise.all([
    listTopics().catch(() => []),
    getInstanceInfo().catch(() => null),
  ]);
  const topicLinks = topics.map((t) => ({ id: t.id, slug: t.slug, name: t.name }));

  return (
    <AppShell
      nav={NAV}
      topics={topicLinks}
      activeRecord={me.active_record}
      memberships={memberships}
    >
      {instance?.demo_mode && <DemoBanner />}
      {children}
      {/* docs/07 R3 — global fact-context sidesheet. Reads ?fact=<id>
          from the URL on any (app) route and overlays. Closing
          clears the param, so the back button closes it. */}
      <FactContextSheet />
    </AppShell>
  );
}
