import Link from "next/link";

// Shown after registering on a verification-required instance (e.g. via an
// invite): the account exists but is dormant until the emailed link is
// opened. Static segment — wins over the sibling [token] dynamic route.

export default function VerifyEmailSentPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      <h1 className="font-serif text-3xl">Check your email</h1>
      <p className="mt-4 text-sm text-muted">
        Your account was created. We&rsquo;ve sent a link to verify your email
        address — open it to finish setting up and unlock access. The link
        expires after a while.
      </p>
      <p className="mt-6 text-sm text-muted">
        Didn&rsquo;t get it? Check spam, or sign in to have a new link sent.
      </p>
      <Link
        href="/login"
        className="mt-8 rounded-lg bg-accent px-4 py-2 text-center text-surface"
      >
        Go to sign in
      </Link>
    </main>
  );
}
