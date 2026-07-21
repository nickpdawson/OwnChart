import Link from "next/link";

// Post-deletion confirmation (P0-3). Reached after DELETE /api/account
// clears the session cookie, so it must render without auth — hence the
// (auth) group, which has no signed-in gate.

export default function AccountDeletedPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      <h1 className="font-serif text-3xl">Your account is deleted</h1>
      <p className="mt-4 text-sm text-muted">
        Your account and the data you solely owned have been permanently
        erased from this instance. Records you shared with another owner stay
        with them.
      </p>
      <p className="mt-6 text-sm text-muted">
        Thanks for trying OwnChart. You can create a new account any time.
      </p>
      <Link
        href="/login"
        className="mt-8 rounded-lg bg-accent px-4 py-2 text-center text-surface"
      >
        Back to sign in
      </Link>
    </main>
  );
}
