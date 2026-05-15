import Link from "next/link";

// The (app) layout fetches /api/auth/me at request time, which means
// every page under it has to be dynamic — prerendering this static
// page would force the layout to fetch the api at build time and fail.
export const dynamic = "force-dynamic";

// "Sign in with Claude" stub.
//
// As of 2026-05-15, Anthropic does NOT expose a consumer OAuth flow
// that exchanges a claude.ai login for an API-callable token. Their
// API requires a console.anthropic.com API key (BYOK).
//
// This page exists so the link from /settings/providers goes
// somewhere honest — it explains the gap rather than leaving a
// dead button. When Anthropic ships consumer OAuth, this page
// becomes the OAuth start endpoint and we wire the callback into
// /api/connectors/callback (or a sibling).

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
        Coming soon — when Anthropic exposes it
      </h1>

      <div className="mt-6 space-y-5 text-sm leading-relaxed">
        <p>
          OwnChart will let you log in with your{" "}
          <a
            href="https://claude.ai"
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent underline-offset-4 hover:underline"
          >
            claude.ai
          </a>{" "}
          account and use your Pro / Max subscription instead of
          paying per-token for an API key. The UI is here, the
          wiring is here. What&apos;s missing is the upstream:
          Anthropic doesn&apos;t yet expose a consumer OAuth flow
          that exchanges a claude.ai session for an API-callable
          token.
        </p>
        <p>
          Until they do, the realistic path is:
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
            . That account is separate from your claude.ai
            account, has its own billing, and ships per-token
            pricing rather than a flat monthly fee.
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
          <p className="font-medium text-ink">Why we&apos;re honest about this</p>
          <p className="mt-1">
            A button that lies (&quot;Sign in with Claude&quot; that
            then asks for an API key) erodes trust. A button that
            tells you what&apos;s actually possible — and links to the
            real path — is the same UX with less surprise. The
            wiring is here so the day Anthropic ships consumer
            OAuth, this page becomes the start of the flow.
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
