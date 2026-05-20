import { listCalendarSources, type CalendarSourceOut } from "@/lib/api";
import { CalendarSettingsClient } from "./CalendarSettingsClient";

export const dynamic = "force-dynamic";

// FU-CAL-WEB-SETTINGS-UI — web settings/status surface for calendar
// sources. Wires only against existing backend endpoints
// (GET/PATCH/DELETE /api/calendar/sources). Google/ICS are
// rendered as "not configured by operator" placeholders until the
// FU-CAL-GOOGLE-OAUTH / FU-CAL-ICS-ADAPTER adapters land.

export default async function CalendarSettingsPage() {
  let sources: CalendarSourceOut[] = [];
  let loadError: string | null = null;
  try {
    sources = await listCalendarSources();
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

      <CalendarSettingsClient
        initialSources={sources}
        loadError={loadError}
      />
    </div>
  );
}
