"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

/**
 * First-owner bootstrap (P0-1 release review).
 *
 * Creating the instance owner requires the one-time **setup token** minted
 * at API startup — logged once to the api container log and written to
 * `data/setup_token`. This page is useless without that token, so linking
 * to it from the login page is not an attack surface: the register call it
 * makes is gated server-side by the token + a Postgres advisory lock.
 */
export default function SetupPage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const r = await fetch(`/api/auth/register`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, password, setup_token: token.trim() }),
        credentials: "include",
      });
      if (!r.ok) {
        let code: string | undefined;
        try {
          const body = await r.json();
          code =
            typeof body?.detail === "object" ? body.detail?.code : undefined;
        } catch {
          /* non-JSON body */
        }
        if (code === "setup_token_required" || code === "setup_token_invalid") {
          setError(
            "That setup token isn’t valid. Find it in the API startup log " +
              "(docker compose logs api | grep setup_token) or in " +
              "data/setup_token, then try again.",
          );
        } else if (r.status === 403) {
          setError(
            "This server already has an owner. Use Sign in instead.",
          );
        } else {
          setError("Something went wrong.");
        }
        return;
      }
      const me = await r.json();
      router.replace(me.phi_consent_granted ? "/dashboard" : "/consent");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      <h1 className="font-serif text-3xl">Set up OwnChart</h1>
      <p className="mt-2 text-sm text-muted">
        Create the owner account for this instance. This runs once, on a
        fresh install.
      </p>

      <form onSubmit={submit} className="mt-10 flex flex-col gap-3">
        <label className="text-sm">
          Setup token
          <input
            type="text"
            required
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="mt-1 w-full rounded-lg border border-muted/30 bg-surface px-3 py-2 font-mono text-xs"
          />
          <span className="mt-1 block text-xs text-muted">
            From the API startup log (<code>docker compose logs api | grep
            setup_token</code>) or <code>data/setup_token</code>.
          </span>
        </label>
        <label className="text-sm">
          Owner email
          <input
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded-lg border border-muted/30 bg-surface px-3 py-2"
          />
        </label>
        <label className="text-sm">
          Password
          <input
            type="password"
            required
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-lg border border-muted/30 bg-surface px-3 py-2"
          />
        </label>

        {error && <p className="text-sm text-caution">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="mt-2 rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50"
        >
          {busy ? "…" : "Create owner account"}
        </button>
      </form>

      <p className="mt-8 text-sm text-muted">
        Already have an account?{" "}
        <Link href="/login" className="underline-offset-4 hover:underline">
          Sign in
        </Link>
        .
      </p>
    </main>
  );
}
