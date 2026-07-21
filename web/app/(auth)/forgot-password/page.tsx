"use client";

import { useState } from "react";
import Link from "next/link";

/**
 * Forgot-password request (P0-3 recovery slice).
 *
 * Posts the email to the always-200 anti-enumeration endpoint. The UI
 * mirrors the backend: it NEVER reveals whether an account exists — the
 * same neutral "if that address has an account, we've sent a link"
 * confirmation shows regardless of the response. Rate-limited server-side.
 */
export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const r = await fetch("/api/auth/password/reset-request", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email: email.trim() }),
      });
      if (r.status === 429) {
        setError("Too many attempts. Wait a moment and try again.");
        return;
      }
      // Any 2xx (and even most non-2xx) collapses to the same neutral
      // confirmation — we must not become an existence oracle.
      setSent(true);
    } catch {
      setError("Something went wrong. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      <h1 className="font-serif text-3xl">Reset your password</h1>

      {sent ? (
        <>
          <p className="mt-4 text-sm text-muted">
            If that address has an account, we&rsquo;ve sent a link to reset
            your password. It expires after a short while — check your inbox
            (and spam).
          </p>
          <p className="mt-8 text-sm text-muted">
            <Link href="/login" className="underline-offset-4 hover:underline">
              Back to sign in
            </Link>
          </p>
        </>
      ) : (
        <>
          <p className="mt-2 text-sm text-muted">
            Enter your email and we&rsquo;ll send you a link to set a new
            password.
          </p>

          <form onSubmit={submit} className="mt-10 flex flex-col gap-3">
            <label className="text-sm">
              Email
              <input
                type="email"
                required
                autoComplete="email"
                autoCapitalize="none"
                spellCheck={false}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="mt-1 w-full rounded-lg border border-muted/30 bg-surface px-3 py-2"
              />
            </label>

            {error && <p className="text-sm text-caution">{error}</p>}

            <button
              type="submit"
              disabled={busy}
              className="mt-2 rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50"
            >
              {busy ? "…" : "Send reset link"}
            </button>
          </form>

          <p className="mt-8 text-sm text-muted">
            Remembered it?{" "}
            <Link href="/login" className="underline-offset-4 hover:underline">
              Sign in
            </Link>
            .
          </p>
        </>
      )}
    </main>
  );
}
