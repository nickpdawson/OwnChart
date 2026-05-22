"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type {
  CalendarPrivacyMode,
  GoogleCalendarChoice,
} from "@/lib/api";

type BindOptions = {
  privacy_mode: CalendarPrivacyMode;
  llm_full_details_consent: boolean;
  history_window_back: string;
};

type Props = {
  credentialId: string;
  accountEmail: string;
  calendars: GoogleCalendarChoice[];
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

const HISTORY_CHOICES: { value: string; label: string }[] = [
  { value: "90d", label: "Last 90 days" },
  { value: "1y", label: "Last 1 year" },
  { value: "3y", label: "Last 3 years" },
  { value: "5y", label: "Last 5 years" },
  { value: "all", label: "All history" },
];

export function GoogleCalendarPickerClient({
  credentialId,
  accountEmail,
  calendars,
}: Props) {
  const router = useRouter();
  // Default the primary calendar to selected; users almost
  // always want it.
  const initialSelected: Record<string, boolean> = {};
  for (const c of calendars) {
    initialSelected[c.external_id] = c.primary;
  }
  const [selected, setSelected] = useState<Record<string, boolean>>(
    initialSelected,
  );
  const [options, setOptions] = useState<Record<string, BindOptions>>(
    Object.fromEntries(
      calendars.map((c) => [
        c.external_id,
        {
          privacy_mode: "title_and_time",
          llm_full_details_consent: false,
          history_window_back: "90d",
        },
      ]),
    ),
  );
  const [binding, setBinding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [doneCount, setDoneCount] = useState(0);

  const selectedIds = calendars
    .map((c) => c.external_id)
    .filter((id) => selected[id]);

  async function bindAll() {
    setBinding(true);
    setError(null);
    setDoneCount(0);
    try {
      for (const externalId of selectedIds) {
        const cal = calendars.find((c) => c.external_id === externalId);
        if (!cal) continue;
        const opts = options[externalId];
        const r = await fetch(
          `/api/calendar/google/credentials/${credentialId}/bind`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            credentials: "include",
            body: JSON.stringify({
              external_id: externalId,
              display_name: cal.summary,
              privacy_mode: opts.privacy_mode,
              llm_full_details_consent: opts.llm_full_details_consent,
              history_window_back: opts.history_window_back,
            }),
          },
        );
        if (!r.ok) {
          const body = await r.text();
          throw new Error(
            `Couldn't bind "${cal.summary}" (HTTP ${r.status}): ${body}`,
          );
        }
        setDoneCount((n) => n + 1);
      }
      router.push("/settings/calendar?google_bind=ok");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBinding(false);
    }
  }

  if (calendars.length === 0) {
    return (
      <div className="mt-6 rounded-xl border border-muted/15 bg-surface p-4 text-sm text-muted">
        Google returned no calendars for{" "}
        <span className="font-mono">{accountEmail}</span>. This is
        unusual — try reconnecting, or verify that the account has at
        least one calendar.
      </div>
    );
  }

  return (
    <div className="mt-7 space-y-7">
      <section>
        <h2 className="text-xs uppercase tracking-widest text-muted">
          Calendars under this Google account
        </h2>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          Selected calendars will start syncing into OwnChart
          immediately after you bind. You can change privacy mode
          and AI exposure later from the Calendar settings page.
        </p>
        <ul className="mt-4 space-y-3">
          {calendars.map((c) => {
            const isSelected = !!selected[c.external_id];
            const opts = options[c.external_id];
            return (
              <li
                key={c.external_id}
                className="rounded-xl border border-muted/15 bg-surface p-4"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <label className="flex items-baseline gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={(e) =>
                        setSelected((prev) => ({
                          ...prev,
                          [c.external_id]: e.target.checked,
                        }))
                      }
                      className="mt-1"
                    />
                    <span>
                      <span className="font-medium">{c.summary}</span>
                      {c.primary && (
                        <span className="ml-2 rounded-md bg-accent/15 px-1.5 py-0.5 text-[10px] uppercase tracking-widest text-accent">
                          primary
                        </span>
                      )}
                      <span className="ml-2 font-mono text-xs text-muted">
                        {c.external_id}
                      </span>
                      {c.time_zone && (
                        <span className="ml-2 text-xs text-muted">
                          ({c.time_zone})
                        </span>
                      )}
                    </span>
                  </label>
                </div>

                {isSelected && (
                  <dl className="mt-4 grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-[10rem_1fr]">
                    <dt className="text-muted">Privacy mode</dt>
                    <dd>
                      <select
                        value={opts.privacy_mode}
                        onChange={(e) =>
                          setOptions((prev) => ({
                            ...prev,
                            [c.external_id]: {
                              ...prev[c.external_id],
                              privacy_mode: e.target.value as CalendarPrivacyMode,
                            },
                          }))
                        }
                        className="rounded-md border border-muted/30 bg-surface px-2 py-1 text-sm"
                      >
                        {PRIVACY_CHOICES.map((p) => (
                          <option key={p.value} value={p.value}>
                            {p.label}
                          </option>
                        ))}
                      </select>
                      <p className="mt-1 max-w-xl text-xs text-muted">
                        {
                          PRIVACY_CHOICES.find(
                            (p) => p.value === opts.privacy_mode,
                          )?.blurb
                        }
                      </p>
                    </dd>

                    <dt className="text-muted">AI exposure</dt>
                    <dd>
                      <label className="inline-flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={opts.llm_full_details_consent}
                          onChange={(e) =>
                            setOptions((prev) => ({
                              ...prev,
                              [c.external_id]: {
                                ...prev[c.external_id],
                                llm_full_details_consent: e.target.checked,
                              },
                            }))
                          }
                        />
                        <span>
                          Let Ask read titles from this calendar
                        </span>
                      </label>
                      <p className="mt-1 max-w-xl text-xs text-muted">
                        Off (recommended for sensitive calendars): Ask
                        only sees start, end, and all-day flag.
                      </p>
                    </dd>

                    <dt className="text-muted">History window</dt>
                    <dd>
                      <select
                        value={opts.history_window_back}
                        onChange={(e) =>
                          setOptions((prev) => ({
                            ...prev,
                            [c.external_id]: {
                              ...prev[c.external_id],
                              history_window_back: e.target.value,
                            },
                          }))
                        }
                        className="rounded-md border border-muted/30 bg-surface px-2 py-1 text-sm"
                      >
                        {HISTORY_CHOICES.map((h) => (
                          <option key={h.value} value={h.value}>
                            {h.label}
                          </option>
                        ))}
                      </select>
                      <p className="mt-1 max-w-xl text-xs text-muted">
                        Events older than this window are hidden from
                        Ask and OwnChart summaries (events still stay
                        in your record).
                      </p>
                    </dd>
                  </dl>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      <div className="flex items-center gap-4">
        <button
          type="button"
          disabled={binding || selectedIds.length === 0}
          onClick={bindAll}
          className="rounded-md border border-accent/40 px-4 py-2 text-sm text-accent hover:bg-accent/10 disabled:opacity-50"
        >
          {binding
            ? `Binding ${doneCount} / ${selectedIds.length}…`
            : `Bind ${selectedIds.length} calendar${
                selectedIds.length === 1 ? "" : "s"
              }`}
        </button>
        <a
          href="/settings/calendar"
          className="text-sm text-muted hover:text-fg"
        >
          Cancel
        </a>
      </div>

      {error && (
        <p className="text-sm text-caution">Couldn&apos;t bind: {error}</p>
      )}
    </div>
  );
}
