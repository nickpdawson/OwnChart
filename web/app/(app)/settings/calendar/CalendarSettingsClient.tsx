"use client";

import { useState } from "react";
import type {
  CalendarPrivacyMode,
  CalendarSourceOut,
  CalendarSyncStatus,
} from "@/lib/api";

type Props = {
  initialSources: CalendarSourceOut[];
  loadError: string | null;
};

const PRIVACY_CHOICES: { value: CalendarPrivacyMode; label: string; blurb: string }[] = [
  {
    value: "full_details",
    label: "Full details",
    blurb:
      "Title, location, notes, and attendee count are stored on this device.",
  },
  {
    value: "title_and_time",
    label: "Title and time",
    blurb:
      "Title and time are stored. Location, notes, and attendees are dropped at ingest.",
  },
  {
    value: "busy_only",
    label: "Busy only",
    blurb:
      "Only start, end, and all-day flag are stored. Title, location, notes, attendees are dropped.",
  },
];

function adapterLabel(adapter: string): string {
  if (adapter === "ios_eventkit") return "iOS EventKit";
  if (adapter === "google_calendar") return "Google Calendar";
  if (adapter === "ics") return "ICS feed";
  return adapter;
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function syncStatusLabel(
  status: CalendarSyncStatus | null,
  ts: string | null,
): string {
  if (!ts || !status) return "Never";
  if (status === "ok") return formatTimestamp(ts);
  if (status === "empty")
    return `${formatTimestamp(ts)} — calendar was empty`;
  return formatTimestamp(ts);
}

export function CalendarSettingsClient({
  initialSources,
  loadError,
}: Props) {
  const [sources, setSources] = useState<CalendarSourceOut[]>(initialSources);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function patchSource(
    id: string,
    body: Partial<Pick<CalendarSourceOut, "privacy_mode" | "llm_full_details_consent" | "display_name">>,
  ) {
    setBusyId(id);
    setError(null);
    try {
      const r = await fetch(`/api/calendar/sources/${id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      const next = (await r.json()) as CalendarSourceOut;
      setSources((prev) => prev.map((s) => (s.id === next.id ? next : s)));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function disconnect(id: string) {
    const confirmed = window.confirm(
      "Disconnect this calendar? Existing events from this source will be hidden from the timeline. You can reconnect later from the iOS app.",
    );
    if (!confirmed) return;
    setBusyId(id);
    setError(null);
    try {
      const r = await fetch(`/api/calendar/sources/${id}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!r.ok && r.status !== 204) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      setSources((prev) => prev.filter((s) => s.id !== id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="mt-7 space-y-9">
      {loadError && (
        <p className="rounded-xl border border-caution/30 bg-caution/10 p-3 text-sm text-caution">
          Couldn&apos;t load calendar sources: {loadError}
        </p>
      )}

      <section>
        <h2 className="text-xs uppercase tracking-widest text-muted">
          Connected
        </h2>
        {sources.length === 0 ? (
          <p className="mt-3 rounded-xl border border-muted/15 bg-surface p-4 text-sm text-muted">
            No calendars are connected for this record. Connect a calendar
            from the OwnChart iOS app to start syncing.
          </p>
        ) : (
          <ul className="mt-3 space-y-3">
            {sources.map((src) => (
              <SourceRow
                key={src.id}
                source={src}
                busy={busyId === src.id}
                onPatch={(body) => patchSource(src.id, body)}
                onDisconnect={() => disconnect(src.id)}
              />
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2 className="text-xs uppercase tracking-widest text-muted">
          Other adapters
        </h2>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          Additional calendar adapters are not configured on this OwnChart
          deployment. Today only the iOS EventKit adapter is wired up.
        </p>
        <ul className="mt-3 space-y-3">
          <PlaceholderRow
            adapter="google_calendar"
            note="Google Calendar OAuth adapter is not yet wired up on this OwnChart deployment."
          />
          <PlaceholderRow
            adapter="ics"
            note="ICS feed adapter is not yet wired up on this OwnChart deployment."
          />
        </ul>
      </section>

      {error && (
        <p className="text-sm text-caution">Couldn&apos;t save: {error}</p>
      )}
    </div>
  );
}

function SourceRow({
  source,
  busy,
  onPatch,
  onDisconnect,
}: {
  source: CalendarSourceOut;
  busy: boolean;
  onPatch: (
    body: Partial<Pick<CalendarSourceOut, "privacy_mode" | "llm_full_details_consent" | "display_name">>,
  ) => void;
  onDisconnect: () => void;
}) {
  const privacyChoice = PRIVACY_CHOICES.find((c) => c.value === source.privacy_mode);
  return (
    <li className="rounded-xl border border-muted/15 bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <p className="font-medium">{source.display_name || "(unnamed)"}</p>
          <p className="mt-1 text-xs text-muted">
            {adapterLabel(source.adapter_type)}
            <span className="mx-2 opacity-50">·</span>
            <span className="font-mono">{source.external_id}</span>
          </p>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={onDisconnect}
          className="rounded-md border border-caution/40 px-3 py-1.5 text-sm text-caution hover:bg-caution/10 disabled:opacity-50"
        >
          Disconnect
        </button>
      </div>

      <dl className="mt-4 grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-[10rem_1fr]">
        <dt className="text-muted">Privacy mode</dt>
        <dd>
          <select
            disabled={busy}
            value={source.privacy_mode}
            onChange={(e) =>
              onPatch({ privacy_mode: e.target.value as CalendarPrivacyMode })
            }
            className="rounded-md border border-muted/30 bg-surface px-2 py-1 text-sm disabled:opacity-50"
            aria-label="Privacy mode"
          >
            {PRIVACY_CHOICES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
          {privacyChoice && (
            <p className="mt-1 max-w-xl text-xs text-muted">
              {privacyChoice.blurb}
            </p>
          )}
        </dd>

        <dt className="text-muted">AI exposure</dt>
        <dd>
          <div className="flex items-center gap-3">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                onPatch({
                  llm_full_details_consent: !source.llm_full_details_consent,
                })
              }
              className={
                "inline-flex h-7 w-12 items-center rounded-full transition-colors disabled:opacity-50 " +
                (source.llm_full_details_consent ? "bg-accent" : "bg-muted/30")
              }
              aria-pressed={source.llm_full_details_consent}
              aria-label="Allow Ask to read calendar titles"
            >
              <span
                className={
                  "inline-block h-5 w-5 transform rounded-full bg-surface shadow transition-transform " +
                  (source.llm_full_details_consent
                    ? "translate-x-6"
                    : "translate-x-1")
                }
              />
            </button>
            <span className="text-xs text-muted">
              {source.llm_full_details_consent ? (
                <>
                  Ask may read event titles from this calendar when they
                  fit a question.
                </>
              ) : (
                <>
                  Ask only sees start, end, and all-day flag from this
                  calendar — never titles, locations, or notes.
                </>
              )}
            </span>
          </div>
        </dd>

        <dt className="text-muted">Connected</dt>
        <dd className="text-muted">{formatTimestamp(source.connected_at)}</dd>

        <dt className="text-muted">Last sync</dt>
        <dd className="text-muted">
          {syncStatusLabel(source.last_sync_status, source.last_sync_at)}
        </dd>

        <dt className="text-muted">Events stored</dt>
        <dd className="text-muted">
          {source.visible_event_count.toLocaleString()}
          {source.stored_event_count > source.visible_event_count && (
            <span className="ml-2 text-xs opacity-70">
              ({(
                source.stored_event_count - source.visible_event_count
              ).toLocaleString()}{" "}
              tombstoned, awaiting 30-day purge)
            </span>
          )}
        </dd>
      </dl>

      {busy && (
        <p className="mt-3 text-xs text-muted">Saving…</p>
      )}
    </li>
  );
}

function PlaceholderRow({
  adapter,
  note,
}: {
  adapter: string;
  note: string;
}) {
  return (
    <li className="rounded-xl border border-muted/15 bg-surface p-4 opacity-70">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <p className="font-medium">{adapterLabel(adapter)}</p>
          <p className="mt-1 max-w-2xl text-sm text-muted">{note}</p>
        </div>
        <span className="rounded-md bg-muted/10 px-2 py-1 text-[10px] uppercase tracking-widest text-muted">
          Not configured by operator
        </span>
      </div>
    </li>
  );
}
