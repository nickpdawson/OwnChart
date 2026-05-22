"use client";

import { useState } from "react";

// Minimal sign-out for recovery pages. Lives here (not in
// AppShell) because recovery routes intentionally don't render
// the app shell — they're terminal "you can't proceed" surfaces
// and the only outbound link is sign-out.

export function SignOutButton() {
  const [busy, setBusy] = useState(false);

  async function signOut() {
    setBusy(true);
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "include",
      });
    } catch {
      /* even on failure we route to /login; the cookie is httponly
         and the server has already cleared it on success */
    }
    window.location.assign("/login");
  }

  return (
    <button
      type="button"
      onClick={signOut}
      disabled={busy}
      className="rounded-md border border-muted/30 px-4 py-2 text-sm text-muted transition-colors hover:border-muted/60 hover:text-ink disabled:opacity-60"
    >
      {busy ? "Signing out…" : "Sign out"}
    </button>
  );
}
