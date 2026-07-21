"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const r = await fetch(`/api/auth/login`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, password }),
        credentials: "include",
      });
      if (!r.ok) {
        if (r.status === 401) {
          setError("Invalid credentials.");
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
      <h1 className="font-serif text-3xl">OwnChart</h1>
      <p className="mt-2 text-sm text-muted">
        Patient-owned longitudinal health intelligence.
      </p>

      <form onSubmit={submit} className="mt-10 flex flex-col gap-3">
        <label className="text-sm">
          Email
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
            autoComplete="current-password"
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
          {busy ? "…" : "Sign in"}
        </button>
      </form>

      <p className="mt-8 text-sm text-muted">
        Setting up this server?{" "}
        <Link href="/setup" className="underline-offset-4 hover:underline">
          Create the owner account
        </Link>
        .
      </p>
    </main>
  );
}
