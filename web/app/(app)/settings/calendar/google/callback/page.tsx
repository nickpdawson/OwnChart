import Link from "next/link";
import { exchangeGoogleCallback } from "@/lib/api";
import { GoogleCalendarPickerClient } from "./GoogleCalendarPickerClient";

export const dynamic = "force-dynamic";

// Beta 1 Section A — landing page for Google's OAuth redirect.
//
// Operator must set OWNCHART_GOOGLE_CALENDAR_REDIRECT_URI to:
//     https://<your-host>/settings/calendar/google/callback
// (NOT /api/calendar/google/callback). The backend's
// /api/calendar/google/callback endpoint remains a JSON API; we
// call it server-side from here with the code + state we
// received from Google, then render the picker.
//
// Failure modes surfaced explicitly:
//   - User denied consent → Google sends ?error=access_denied.
//   - OAuth state expired or malformed → 400 from backend.
//   - Operator config missing → 503 from backend (rare; the
//     connect-start would have refused earlier).

type SearchParams = {
  code?: string;
  state?: string;
  error?: string;
  error_description?: string;
};

export default async function GoogleCallbackPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;

  // Google's error path — user clicked "Cancel" or revoked
  // consent on the OAuth consent screen.
  if (params.error) {
    return (
      <div className="max-w-2xl">
        <p className="text-sm uppercase tracking-widest text-muted">
          <Link href="/settings" className="hover:text-fg">
            Settings
          </Link>{" "}
          /{" "}
          <Link href="/settings/calendar" className="hover:text-fg">
            Calendar
          </Link>{" "}
          / Google connect
        </p>
        <h1 className="mt-2 font-serif text-3xl">Google didn&apos;t connect</h1>
        <p className="mt-3 text-muted">
          The Google sign-in page returned an error and no calendars
          were connected.
        </p>
        <dl className="mt-6 grid grid-cols-[8rem_1fr] gap-x-6 gap-y-2 rounded-xl border border-muted/15 bg-surface p-4 text-sm">
          <dt className="text-muted">Code</dt>
          <dd className="font-mono text-xs">{params.error}</dd>
          {params.error_description && (
            <>
              <dt className="text-muted">Detail</dt>
              <dd>{params.error_description}</dd>
            </>
          )}
        </dl>
        <Link
          href="/settings/calendar"
          className="mt-6 inline-block rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/10"
        >
          ← Back to Calendar settings
        </Link>
      </div>
    );
  }

  // Missing the parameters Google should have sent — typically a
  // user landing here without going through the connect flow.
  if (!params.code || !params.state) {
    return (
      <div className="max-w-2xl">
        <p className="text-sm uppercase tracking-widest text-muted">
          <Link href="/settings" className="hover:text-fg">
            Settings
          </Link>{" "}
          /{" "}
          <Link href="/settings/calendar" className="hover:text-fg">
            Calendar
          </Link>{" "}
          / Google connect
        </p>
        <h1 className="mt-2 font-serif text-3xl">Nothing to do here</h1>
        <p className="mt-3 text-muted">
          This page handles the Google sign-in redirect. Start the
          flow from the Calendar settings page.
        </p>
        <Link
          href="/settings/calendar"
          className="mt-6 inline-block rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/10"
        >
          ← Go to Calendar settings
        </Link>
      </div>
    );
  }

  // Exchange the code + state for a credential + calendar picker.
  let exchange;
  try {
    exchange = await exchangeGoogleCallback(params.code, params.state);
  } catch (e) {
    return (
      <div className="max-w-2xl">
        <p className="text-sm uppercase tracking-widest text-muted">
          <Link href="/settings" className="hover:text-fg">
            Settings
          </Link>{" "}
          /{" "}
          <Link href="/settings/calendar" className="hover:text-fg">
            Calendar
          </Link>{" "}
          / Google connect
        </p>
        <h1 className="mt-2 font-serif text-3xl">Connection failed</h1>
        <p className="mt-3 text-muted">
          The Google OAuth handshake didn&apos;t complete. This usually
          means the sign-in link was already used, expired, or the
          operator&apos;s Google Cloud configuration changed since the
          flow started.
        </p>
        <pre className="mt-4 overflow-x-auto rounded-md border border-muted/15 bg-surface p-3 text-xs text-muted">
          {(e as Error).message}
        </pre>
        <Link
          href="/settings/calendar"
          className="mt-6 inline-block rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/10"
        >
          ← Try again from Calendar settings
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <Link href="/settings" className="hover:text-fg">
          Settings
        </Link>{" "}
        /{" "}
        <Link href="/settings/calendar" className="hover:text-fg">
          Calendar
        </Link>{" "}
        / Google connect
      </p>
      <h1 className="mt-2 font-serif text-3xl">Pick calendars to sync</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Connected as{" "}
        <span className="font-mono text-sm text-fg">
          {exchange.google_account_email}
        </span>
        . Each calendar you bind syncs into OwnChart on its own
        privacy mode and AI-exposure toggle. You can disconnect a
        calendar at any time from the Calendar settings page.
      </p>

      <GoogleCalendarPickerClient
        credentialId={exchange.credential_id}
        accountEmail={exchange.google_account_email}
        calendars={exchange.calendars}
      />
    </div>
  );
}
