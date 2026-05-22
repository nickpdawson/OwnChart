import {
  listCalendarSources,
  listGoogleCredentials,
  probeGoogleCalendarConfigured,
  type CalendarSourceOut,
  type GoogleConfiguredStatus,
  type GoogleCredentialOut,
} from "@/lib/api";
import { CalendarSettingsClient } from "./CalendarSettingsClient";

export const dynamic = "force-dynamic";

// Beta 1 Section A — Calendar settings surface. Three lanes:
//   - iOS EventKit sources (existing FU-CAL-WEB-SETTINGS-UI).
//   - Google Calendar connect + multi-calendar picker.
//   - ICS placeholder (still operator-not-configured).
//
// Google config probe: a 503 from /connect-start means the
// operator hasn't set the three env vars. We render the
// placeholder in that case rather than a broken-feeling button.

type SearchParams = { google_bind?: string };

export default async function CalendarSettingsPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  let sources: CalendarSourceOut[] = [];
  let credentials: GoogleCredentialOut[] = [];
  let googleConfigured: GoogleConfiguredStatus = {
    configured: false,
    detail: "Unknown — server did not respond.",
  };
  let loadError: string | null = null;
  try {
    [sources, googleConfigured, credentials] = await Promise.all([
      listCalendarSources(),
      probeGoogleCalendarConfigured(),
      listGoogleCredentials().catch(() => []),
    ]);
  } catch (e) {
    loadError = (e as Error).message;
  }

  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <a href="/settings" className="hover:text-fg">
          Settings
        </a>{" "}
        / Calendar
      </p>
      <h1 className="mt-2 font-serif text-3xl">Calendar sources</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Calendars OwnChart is currently reading for this record.
        Privacy mode controls what gets stored when a calendar
        syncs; the AI exposure toggle is a separate decision that
        controls what Ask can see, independent of storage.
      </p>

      {params.google_bind === "ok" && (
        <p className="mt-4 rounded-md border border-evidence/30 bg-evidence/10 p-3 text-sm text-evidence">
          Google calendars connected. The first sync runs in the
          background — refresh in a minute or two to see event
          counts.
        </p>
      )}

      <CalendarSettingsClient
        initialSources={sources}
        googleConfigured={googleConfigured}
        googleCredentials={credentials}
        loadError={loadError}
      />
    </div>
  );
}
