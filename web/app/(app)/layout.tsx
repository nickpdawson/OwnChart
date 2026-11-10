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
// Four destinations organised around patient tasks (Beta 2, audit
// 2026-11-10 item 8), replacing the twelve-entry list: Home, Ask,
// My health (Topics, Timeline), Records (Originals, Connections,
// Review). Chat and "Ask the records" were two doors to the same
// workflow; /ask now redirects to /chat. Discover is no longer a
// destination — its coverage observations belong under Records and
// its contextual ones on Home/Topics (route still answers). Routes
// are unchanged so saved links keep working.
//
// Audit log stays admin-only: the /api/audit/* routes 403 for other
// roles (P0 fix 2026-05-23).
type NavEntry = {
  href: Route;
  label: string;
  adminOnly?: boolean;
  children?: ReadonlyArray<{ href: Route; label: string }>;
};

const NAV: ReadonlyArray<NavEntry> = [
  { href: "/dashboard", label: "Home" },
  { href: "/chat", label: "Ask" },
  {
    href: "/topics",
    label: "My health",
    children: [
      { href: "/topics", label: "Topics" },
      { href: "/timeline", label: "Timeline" },
    ],
  },
  {
    href: "/sources",
    label: "Records",
    children: [
      { href: "/sources", label: "Originals" },
      { href: "/connectors", label: "Connections" },
      { href: "/review", label: "Review" },
    ],
  },
] as const;

const SECONDARY_NAV: ReadonlyArray<NavEntry> = [
  { href: "/audit", label: "Audit log", adminOnly: true },
  { href: "/settings", label: "Settings" },
  { href: "/admin", label: "Admin", adminOnly: true },
] as const;

export default async function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const me = await getMe();
  if (!me) redirect("/login");
  // No consent redirect here. Declining AI consent used to bounce the
  // user back to /consent from every page, so "Continue without LLM
  // features" was a loop. Records, corrections, organization and export
  // work without a model; the AI surfaces show an enable-AI notice and
  // the API refuses egress (llm/egress.py) if anything slips through.

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

  // Filter admin-only nav items unless the caller is the instance admin.
  const isAdmin = me.is_instance_admin === true;
  const visibleNav = NAV.filter((n) => !n.adminOnly || isAdmin);
  const visibleSecondary = SECONDARY_NAV.filter((n) => !n.adminOnly || isAdmin);

  return (
    <AppShell
      nav={visibleNav}
      secondaryNav={visibleSecondary}
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
