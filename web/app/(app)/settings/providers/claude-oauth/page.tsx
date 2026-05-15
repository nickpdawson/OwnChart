import Link from "next/link";

// The (app) layout fetches /api/auth/me at request time, which means
// every page under it has to be dynamic — prerendering this static
// page would force the layout to fetch the api at build time and fail.
export const dynamic = "force-dynamic";

// "Sign in with Claude" status page.
//
// This isn't on OwnChart's roadmap — it's blocked upstream. Anthropic
// has not exposed a consumer OAuth flow for API access. claude.ai is
// a separate product with separate billing from console.anthropic.com.
//
// Copy here is deliberately not "coming soon." That implies an
// OwnChart-side timeline. The honest framing: "Not currently
// supported by Anthropic." If Anthropic exposes it later, the
// wiring is in place.

export default function ClaudeOAuthPage() {
  return (
    <div className="max-w-2xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <Link href="/settings/providers" className="hover:underline">
          AI providers
        </Link>{" "}
        · Sign in with Claude
      </p>
      <h1 className="mt-2 font-serif text-3xl">
        Not currently supported by Anthropic
      </h1>

      <div className="mt-6 space-y-5 text-sm leading-relaxed">
        <p>
          OwnChart supports Anthropic <strong>API keys</strong> today.
          A &quot;Sign in with Claude&quot; flow that uses your{" "}
          <a
            href="https://claude.ai"
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent underline-offset-4 hover:underline"
          >
            claude.ai
          </a>{" "}
          subscription for API calls would require Anthropic to expose
          consumer OAuth for API access. They haven&apos;t. claude.ai
          and console.anthropic.com are separate products with
          separate accounts and separate billing.
        </p>
        <p>
          What works today, in two steps:
        </p>
        <ol className="ml-5 list-decimal space-y-2">
          <li>
            Make an API key at{" "}
            <a
              href="https://console.anthropic.com"
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline-offset-4 hover:underline"
            >
              console.anthropic.com
            </a>
            . The Console account is separate from your claude.ai
            account, has its own billing, and uses per-token pricing
            instead of a flat monthly fee.
          </li>
          <li>
            Drop the key into{" "}
            <Link
              href="/settings/providers"
              className="text-accent underline-offset-4 hover:underline"
            >
              Settings → AI providers
            </Link>
            . OwnChart bills your console balance, never the
            deployment&apos;s shared key.
          </li>
        </ol>

        <div className="rounded-xl border border-muted/15 bg-bg/40 p-4 text-xs text-muted">
          <p className="font-medium text-ink">If Anthropic ships consumer OAuth</p>
          <p className="mt-1">
            The OwnChart side of this is already wired. The
            <code className="mx-1 rounded bg-muted/10 px-1 py-0.5">
              llm_provider_credentials
            </code>
            table already supports an
            <code className="mx-1 rounded bg-muted/10 px-1 py-0.5">
              auth_kind = &quot;oauth&quot;
            </code>
            row with access/refresh tokens. The day Anthropic exposes
            it, this page flips from explainer to sign-in flow.
          </p>
        </div>

        <Link
          href="/settings/providers"
          className="inline-block rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5"
        >
          ← Back to AI providers
        </Link>
      </div>
    </div>
  );
}
