"use client";

import { useState } from "react";
import Link from "next/link";

/**
 * Set-a-new-password form (P0-3). POSTs {token, new_password} to the reset
 * endpoint. An invalid/expired token gets a calm error and a route back to
 * request a fresh link; success prompts the user to sign in (the reset also
 * bumps the session version server-side, killing other live sessions).
 */
export function ResetPasswordClient({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Pick a password at least 8 characters long.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      const r = await fetch("/api/auth/password/reset", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token, new_password: password }),
      });
      if (r.ok) {
        setDone(true);
        return;
      }
      let code: string | undefined;
      let message: string | undefined;
      try {
        const body = await r.json();
        if (typeof body?.detail === "object") {
          code = body.detail?.code;
          message = body.detail?.message;
        }
      } catch {
        /* non-JSON */
      }
      if (r.status === 429) {
        setError("Too many attempts. Wait a moment and try again.");
      } else if (code === "invalid_or_expired_token") {
        setExpired(true);
      } else if (code === "password_unchanged") {
        setError("New password must differ from your current one.");
      } else if (code === "weak_password") {
        setError("New password must be at least 8 characters.");
      } else {
        setError(message || "Something went wrong. Try again.");
      }
    } catch {
      setError("Something went wrong. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
        <h1 className="font-serif text-3xl">Password updated</h1>
        <p className="mt-4 text-sm text-muted">
          Your password has been changed. Any other sessions were signed out.
          Sign in with your new password.
        </p>
        <Link
          href="/login"
          className="mt-8 rounded-lg bg-accent px-4 py-2 text-center text-surface"
        >
          Sign in
        </Link>
      </main>
    );
  }

  if (expired) {
    return (
      <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
        <h1 className="font-serif text-3xl">Link expired</h1>
        <p className="mt-4 text-sm text-muted">
          This password reset link is invalid or has expired. Reset links are
          single-use and time-limited — request a fresh one.
        </p>
        <Link
          href="/forgot-password"
          className="mt-8 rounded-lg bg-accent px-4 py-2 text-center text-surface"
        >
          Request a new link
        </Link>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      <h1 className="font-serif text-3xl">Choose a new password</h1>
      <p className="mt-2 text-sm text-muted">
        Pick something at least 8 characters long.
      </p>

      <form onSubmit={submit} className="mt-10 flex flex-col gap-3">
        <label className="text-sm">
          New password
          <input
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-lg border border-muted/30 bg-surface px-3 py-2"
          />
        </label>
        <label className="text-sm">
          Confirm new password
          <input
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            className="mt-1 w-full rounded-lg border border-muted/30 bg-surface px-3 py-2"
          />
        </label>

        {error && <p className="text-sm text-caution">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="mt-2 rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50"
        >
          {busy ? "…" : "Update password"}
        </button>
      </form>

      <p className="mt-8 text-sm text-muted">
        <Link href="/login" className="underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      </p>
    </main>
  );
}
