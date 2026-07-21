"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

type State = "verifying" | "verified" | "invalid" | "error";

/**
 * Fires the verify-email call once on mount. The endpoint is idempotent, so
 * an already-verified account also returns 200 → we show the same "verified"
 * state. A bad/expired token returns 400 → "invalid".
 */
export function VerifyEmailClient({ token }: { token: string }) {
  const [state, setState] = useState<State>("verifying");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/auth/verify-email", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ token }),
        });
        if (cancelled) return;
        if (r.ok) {
          setState("verified");
        } else if (r.status === 400) {
          setState("invalid");
        } else {
          setState("error");
        }
      } catch {
        if (!cancelled) setState("error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      {state === "verifying" && (
        <>
          <h1 className="font-serif text-3xl">Verifying your email…</h1>
          <p className="mt-4 text-sm text-muted">One moment.</p>
        </>
      )}

      {state === "verified" && (
        <>
          <h1 className="font-serif text-3xl">Email verified</h1>
          <p className="mt-4 text-sm text-muted">
            Your email address is confirmed. You can sign in now.
          </p>
          <Link
            href="/login"
            className="mt-8 rounded-lg bg-accent px-4 py-2 text-center text-surface"
          >
            Sign in
          </Link>
        </>
      )}

      {state === "invalid" && (
        <>
          <h1 className="font-serif text-3xl">Link invalid or expired</h1>
          <p className="mt-4 text-sm text-muted">
            This verification link is invalid or has expired. Sign in to have
            a fresh one sent, or ask the person who invited you to reissue the
            invitation.
          </p>
          <Link
            href="/login"
            className="mt-8 rounded-lg bg-accent px-4 py-2 text-center text-surface"
          >
            Go to sign in
          </Link>
        </>
      )}

      {state === "error" && (
        <>
          <h1 className="font-serif text-3xl">Something went wrong</h1>
          <p className="mt-4 text-sm text-muted">
            We couldn&rsquo;t verify your email just now. Refresh to try again,
            or head to sign in.
          </p>
          <Link
            href="/login"
            className="mt-8 rounded-lg bg-accent px-4 py-2 text-center text-surface"
          >
            Go to sign in
          </Link>
        </>
      )}
    </main>
  );
}
