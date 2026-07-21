"use client";

import { useState } from "react";

// Invite acceptance form. POSTs to /api/auth/register with the
// invite_token.
//
// P0-4: the invited email is MASKED in the preview, so the invitee types
// their own address to confirm ownership. The server binds the invite to
// that email (case-insensitive) and rejects a mismatch — typing the wrong
// address can't grant access, and the masked hint lets a legitimate
// invitee recognize which address is expected.

type Props = {
  token: string;
  maskedEmail: string;
};

export function InviteAcceptClient({ token, maskedEmail }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!email.trim()) {
      setError("Enter the email this invite was sent to.");
      return;
    }
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
      const r = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          email: email.trim(),
          password,
          invite_token: token,
        }),
      });
      if (!r.ok) {
        let msg = `Registration failed (${r.status}).`;
        try {
          const body = await r.json();
          const detail = body?.detail;
          if (typeof detail === "object" && detail?.code) {
            if (detail.code === "invite_email_mismatch") {
              msg =
                "That email doesn't match the one this invite was sent to. Check the address and try again.";
            } else if (detail.code === "invitation_unavailable") {
              msg =
                "This invite is no longer available. It may have expired or already been used.";
            } else if (detail.code === "invite_required") {
              msg = "An invite is required to register on this instance.";
            } else if (detail.message) {
              msg = detail.message;
            }
          } else if (typeof detail === "string") {
            msg = detail;
          }
        } catch {
          /* fall through with default */
        }
        throw new Error(msg);
      }
      const me = await r.json();
      // On a verification-required instance the server does NOT sign us in —
      // it sends a verification email and `email_verified` is false. Route
      // to a "check your email" screen instead of the app.
      if (me?.email_verified === false) {
        window.location.assign("/verify-email/sent");
        return;
      }
      // Otherwise the session cookie is set; the layout redirect routes us
      // to /consent or /dashboard depending on PHI consent state.
      window.location.assign("/consent");
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="mt-6 space-y-4">
      <div>
        <label
          className="block text-xs uppercase tracking-widest text-muted"
          htmlFor="invite-email"
        >
          Your email
        </label>
        <input
          id="invite-email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          autoComplete="email"
          autoCapitalize="none"
          spellCheck={false}
          placeholder={maskedEmail}
          className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2 text-sm"
        />
        <span className="mt-1 block text-xs text-muted">
          Confirm the address this invite was sent to ({maskedEmail}).
        </span>
      </div>
      <div>
        <label
          className="block text-xs uppercase tracking-widest text-muted"
          htmlFor="invite-password"
        >
          Password
        </label>
        <input
          id="invite-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          minLength={8}
          autoComplete="new-password"
          className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2 text-sm"
        />
      </div>
      <div>
        <label
          className="block text-xs uppercase tracking-widest text-muted"
          htmlFor="invite-password-confirm"
        >
          Confirm password
        </label>
        <input
          id="invite-password-confirm"
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          required
          minLength={8}
          autoComplete="new-password"
          className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2 text-sm"
        />
      </div>
      {error && (
        <p
          role="alert"
          className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning"
        >
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={busy}
        className="rounded-md border border-accent/40 bg-accent/10 px-4 py-2 text-sm font-medium text-accent transition-colors hover:bg-accent/20 disabled:opacity-60"
      >
        {busy ? "Creating account…" : "Create account & accept invite"}
      </button>
    </form>
  );
}
