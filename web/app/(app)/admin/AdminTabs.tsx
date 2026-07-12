"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { Route } from "next";

const TABS: ReadonlyArray<{ href: Route; label: string }> = [
  { href: "/admin", label: "Overview" },
  { href: "/admin/users", label: "Users" },
  { href: "/admin/usage", label: "Usage" },
  { href: "/admin/invitations", label: "Invitations" },
] as const;

export function AdminTabs() {
  const pathname = usePathname();
  return (
    <nav className="mt-6 flex flex-wrap gap-1 border-b border-muted/15">
      {TABS.map((t) => {
        // "/admin" is only active on the exact overview path; the others
        // match their subtree so /admin/users/[id] keeps "Users" lit.
        const active =
          t.href === "/admin"
            ? pathname === "/admin"
            : pathname === t.href || pathname.startsWith(`${t.href}/`);
        return (
          <Link
            key={t.href}
            href={t.href}
            aria-current={active ? "page" : undefined}
            className={
              "rounded-t-md px-3 py-2 text-sm transition-colors " +
              (active
                ? "border-b-2 border-accent font-medium text-ink"
                : "text-muted hover:text-ink")
            }
          >
            {t.label}
          </Link>
        );
      })}
    </nav>
  );
}
