"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { Route } from "next";
import { useEffect, useRef, useState } from "react";
import type { ActiveRecord, Membership } from "@/lib/api";
import { useFocusTrap } from "@/lib/useFocusTrap";
import { RecordSwitcher } from "./RecordSwitcher";

export type NavItem = {
  href: Route;
  label: string;
  children?: ReadonlyArray<{ href: Route; label: string }>;
};
type TopicLink = { id: string; slug: string; name: string };

export function AppShell({
  nav,
  secondaryNav,
  topics,
  activeRecord,
  memberships,
  children,
}: {
  nav: ReadonlyArray<NavItem>;
  secondaryNav: ReadonlyArray<NavItem>;
  topics: TopicLink[];
  activeRecord: ActiveRecord | null;
  memberships: Membership[];
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const drawerRef = useRef<HTMLElement | null>(null);

  // Close drawer on route change.
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  // Lock body scroll when drawer open on mobile.
  useEffect(() => {
    if (open) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.body.style.overflow = prev;
      };
    }
  }, [open]);

  // Keyboard: focus enters the drawer, stays in it, Escape closes it,
  // focus returns to the hamburger afterwards.
  useFocusTrap(open, drawerRef, () => setOpen(false));

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      {/* --- Mobile top bar -------------------------------------------------- */}
      <header className="flex items-center justify-between gap-3 border-b border-muted/15 bg-surface px-4 py-3 md:hidden">
        <div className="min-w-0">
          <Link href="/dashboard" className="font-serif text-lg" aria-label="OwnChart home">
            OwnChart
          </Link>
          {/* Whose record is open stays visible without opening the
              drawer — a caregiver switching between people needs this
              on every screen, not behind the menu. */}
          {activeRecord && (
            <p className="truncate text-xs text-muted" aria-live="polite">
              {activeRecord.display_name}
              {activeRecord.role !== "owner" ? ` · ${activeRecord.role}` : ""}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open navigation"
          aria-expanded={open}
          aria-controls="mobile-nav-drawer"
          className="-mr-2 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-muted hover:bg-bg hover:text-ink"
        >
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
            <path d="M3 5h16M3 11h16M3 17h16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      {/* --- Mobile drawer (overlay) ---------------------------------------- */}
      {open && (
        <div className="fixed inset-0 z-40 md:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setOpen(false)}
            className="absolute inset-0 bg-black/50"
            tabIndex={-1}
          />
          <aside
            id="mobile-nav-drawer"
            ref={drawerRef}
            role="dialog"
            aria-modal="true"
            aria-label="Navigation"
            className="absolute left-0 top-0 h-full w-[88%] max-w-[20rem] overflow-y-auto bg-surface px-5 py-6 shadow-2xl"
          >
            <div className="flex items-center justify-between">
              <Link href="/dashboard" className="font-serif text-xl">
                OwnChart
              </Link>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close navigation"
                className="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-md text-muted hover:bg-bg hover:text-ink"
              >
                <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
                  <path d="M5 5l12 12M17 5L5 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <RecordSwitcher active={activeRecord} memberships={memberships} />
            <NavList nav={nav} secondaryNav={secondaryNav} topics={topics} pathname={pathname} />
          </aside>
        </div>
      )}

      {/* --- Desktop pinned sidebar. Compact rail at tablet widths so the
             content keeps most of the viewport; full labels from lg. --- */}
      <aside className="hidden md:flex md:w-56 md:flex-col md:border-r md:border-muted/15 md:bg-surface md:px-4 md:py-8 lg:w-64 lg:px-5">
        <Link href="/dashboard" className="font-serif text-xl">
          OwnChart
        </Link>
        <RecordSwitcher active={activeRecord} memberships={memberships} />
        <NavList nav={nav} secondaryNav={secondaryNav} topics={topics} pathname={pathname} />
      </aside>

      {/* --- Main content --------------------------------------------------- */}
      <main className="flex-1 px-5 py-6 md:px-8 md:py-8 lg:px-10">
        {children}
      </main>
    </div>
  );
}

function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function NavLink({
  href,
  label,
  active,
  nested = false,
}: {
  href: Route;
  label: string;
  active: boolean;
  nested?: boolean;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={
        (nested ? "ml-3 px-3 py-2 text-sm " : "px-3 py-2.5 text-[15px] ") +
        "block rounded-md transition-colors " +
        (active ? "bg-bg font-medium text-ink" : "text-muted hover:bg-bg hover:text-ink")
      }
    >
      {label}
    </Link>
  );
}

function NavList({
  nav,
  secondaryNav,
  topics,
  pathname,
}: {
  nav: ReadonlyArray<NavItem>;
  secondaryNav: ReadonlyArray<NavItem>;
  topics: TopicLink[];
  pathname: string;
}) {
  return (
    <>
      <nav className="mt-8 flex flex-col gap-0.5" aria-label="Primary">
        {nav.map((n) => {
          const childActive = (n.children ?? []).some((c) => isActive(pathname, c.href));
          const active = isActive(pathname, n.href) || childActive;
          return (
            <div key={n.href}>
              <NavLink href={n.href} label={n.label} active={active && !childActive} />
              {n.children && n.children.length > 0 && (
                <div className="mt-0.5 flex flex-col gap-0.5">
                  {n.children.map((c) => (
                    <NavLink
                      key={c.href}
                      href={c.href}
                      label={c.label}
                      active={isActive(pathname, c.href)}
                      nested
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </nav>
      {topics.length > 0 && (
        <div className="mt-6 border-t border-muted/10 pt-4">
          <p className="px-3 text-xs uppercase tracking-widest text-muted">Your topics</p>
          <ul className="mt-2 flex flex-col gap-0.5">
            {topics.map((t) => {
              const target = `/dossier/${t.slug}` as Route;
              return (
                <li key={t.id}>
                  <NavLink href={target} label={t.name} active={isActive(pathname, target)} nested />
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {secondaryNav.length > 0 && (
        <nav className="mt-6 border-t border-muted/10 pt-4" aria-label="Secondary">
          <div className="flex flex-col gap-0.5">
            {secondaryNav.map((n) => (
              <NavLink key={n.href} href={n.href} label={n.label} active={isActive(pathname, n.href)} nested />
            ))}
          </div>
        </nav>
      )}
    </>
  );
}
