"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { Route } from "next";
import { useEffect, useState } from "react";

type NavItem = { href: Route; label: string };
type TopicLink = { id: string; slug: string; name: string };

export function AppShell({
  nav,
  topics,
  children,
}: {
  nav: ReadonlyArray<NavItem>;
  topics: TopicLink[];
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();

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

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      {/* --- Mobile top bar -------------------------------------------------- */}
      <header className="flex items-center justify-between border-b border-muted/15 bg-surface px-4 py-3 md:hidden">
        <Link href="/dashboard" className="font-serif text-lg" aria-label="OwnChart home">
          OwnChart
        </Link>
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open navigation"
          aria-expanded={open}
          className="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-md text-muted hover:bg-bg hover:text-ink"
        >
          {/* simple hamburger */}
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
            <path d="M3 5h16M3 11h16M3 17h16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      {/* --- Mobile drawer (overlay) ---------------------------------------- */}
      {open && (
        <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-modal="true">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setOpen(false)}
            className="absolute inset-0 bg-black/50"
          />
          <aside className="absolute left-0 top-0 h-full w-[88%] max-w-[20rem] overflow-y-auto bg-surface px-5 py-6 shadow-2xl">
            <div className="flex items-center justify-between">
              <Link href="/dashboard" className="font-serif text-xl">
                OwnChart
              </Link>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-md text-muted hover:bg-bg hover:text-ink"
              >
                <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
                  <path d="M5 5l12 12M17 5L5 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <NavList nav={nav} topics={topics} pathname={pathname} compact={false} />
          </aside>
        </div>
      )}

      {/* --- Desktop pinned sidebar ----------------------------------------- */}
      <aside className="hidden md:flex md:w-64 md:flex-col md:border-r md:border-muted/15 md:bg-surface md:px-5 md:py-8">
        <Link href="/dashboard" className="font-serif text-xl">
          OwnChart
        </Link>
        <NavList nav={nav} topics={topics} pathname={pathname} compact={false} />
      </aside>

      {/* --- Main content --------------------------------------------------- */}
      <main className="flex-1 px-5 py-6 md:px-10 md:py-8">
        {children}
      </main>
    </div>
  );
}

function NavList({
  nav,
  topics,
  pathname,
  compact: _compact,
}: {
  nav: ReadonlyArray<NavItem>;
  topics: TopicLink[];
  pathname: string;
  compact: boolean;
}) {
  return (
    <>
      <nav className="mt-8 flex flex-col gap-0.5">
        {nav.map((n) => {
          const active = pathname === n.href || pathname.startsWith(`${n.href}/`);
          return (
            <Link
              key={n.href}
              href={n.href}
              aria-current={active ? "page" : undefined}
              className={
                "rounded-md px-3 py-2.5 text-sm transition-colors " +
                (active
                  ? "bg-bg font-medium text-ink"
                  : "text-muted hover:bg-bg hover:text-ink")
              }
            >
              {n.label}
            </Link>
          );
        })}
      </nav>
      {topics.length > 0 && (
        <div className="mt-8 border-t border-muted/10 pt-4">
          <p className="px-3 text-xs uppercase tracking-widest text-muted">Your topics</p>
          <ul className="mt-2 flex flex-col gap-0.5">
            {topics.map((t) => {
              const target = `/dossier/${t.slug}` as const;
              const active = pathname === target || pathname.startsWith(`${target}/`);
              return (
                <li key={t.id}>
                  <Link
                    href={target}
                    aria-current={active ? "page" : undefined}
                    className={
                      "block rounded-md px-3 py-2 text-sm transition-colors " +
                      (active
                        ? "bg-bg font-medium text-ink"
                        : "text-muted hover:bg-bg hover:text-ink")
                    }
                  >
                    {t.name}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </>
  );
}
