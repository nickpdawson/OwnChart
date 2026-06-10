"use client";

import { useEffect, useRef, useState } from "react";
import type {
  ExportDateRangeKind,
  ExportDomain,
  ExportFiltersIn,
  ExportFormat,
  ExportJob,
  ExportStatus,
} from "@/lib/api";

// Section D — Export client.
//
// Single component because the panels (create form + active jobs +
// historical jobs) share state: the form starts a job, the list
// reflects its progress, downloads/deletes run against the same list.
//
// Async posture: the backend POST /api/exports today runs the
// snapshot + mappers INLINE. For a real record that can take
// several minutes — long enough to hit the reverse-proxy timeout
// even if the api worker finishes. We solve this by firing the
// POST AND immediately starting a 5s polling loop on GET
// /api/exports. The job appears in the list within ~1s of POST
// (committed to DB before the runner starts), so even if the
// POST connection drops the user sees status flip from pending
// to running to completed.

type Props = {
  initialJobs: ExportJob[];
};

const POLL_INTERVAL_MS = 5_000;

function fileTypeLabel(t: string): string {
  switch (t) {
    case "ownchart_json":
      return "OwnChart JSON";
    case "txt":
      return "TXT packet";
    case "pictal_json":
      return "Pictal JSON";
    default:
      return t;
  }
}

function fmtBytes(n: number | null): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function statusBadge(s: ExportStatus, expired: boolean): {
  label: string;
  cls: string;
} {
  if (expired) {
    return {
      label: "Expired",
      cls: "border-muted/20 bg-bg/40 text-muted",
    };
  }
  if (s === "pending") {
    return {
      label: "Pending",
      cls: "border-muted/30 bg-bg/40 text-muted",
    };
  }
  if (s === "running") {
    return {
      label: "Running…",
      cls: "border-accent/40 bg-accent/10 text-accent",
    };
  }
  if (s === "completed") {
    return {
      label: "Completed",
      cls: "border-evidence/30 bg-evidence/10 text-evidence",
    };
  }
  return {
    label: "Failed",
    cls: "border-warning/40 bg-warning/10 text-warning",
  };
}

function jobIsExpired(j: ExportJob): boolean {
  if (j.status !== "completed" || !j.expires_at) return false;
  try {
    return new Date(j.expires_at).getTime() < Date.now();
  } catch {
    return false;
  }
}

export function ExportClient({ initialJobs }: Props) {
  const [jobs, setJobs] = useState<ExportJob[]>(initialJobs);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // form state
  const [format, setFormat] = useState<ExportFormat>("all");
  const [dateRange, setDateRange] =
    useState<ExportDateRangeKind>("all");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [domains, setDomains] = useState<Record<ExportDomain, boolean>>({
    clinical: true,
    body_signals: true,
    calendar: true,
  });

  // Cleanup poll on unmount.
  useEffect(
    () => () => {
      if (pollRef.current) clearInterval(pollRef.current);
    },
    [],
  );

  function startPolling() {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      try {
        const r = await fetch("/api/exports", { credentials: "include" });
        if (!r.ok) return;
        const next = (await r.json()) as ExportJob[];
        setJobs(next);
        // Stop polling when no jobs are pending/running.
        const anyActive = next.some(
          (j) => j.status === "pending" || j.status === "running",
        );
        if (!anyActive && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        /* network blip — keep polling */
      }
    }, POLL_INTERVAL_MS);
  }

  function toggleDomain(d: ExportDomain) {
    setDomains((prev) => ({ ...prev, [d]: !prev[d] }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const selectedDomains = (
      ["clinical", "body_signals", "calendar"] as ExportDomain[]
    ).filter((d) => domains[d]);
    if (selectedDomains.length === 0) {
      setError("Pick at least one domain to export.");
      return;
    }
    if (
      dateRange === "custom" &&
      !customStart &&
      !customEnd
    ) {
      setError(
        "Custom range needs at least a start date (end defaults to today).",
      );
      return;
    }

    const filters: ExportFiltersIn = {
      date_range_kind: dateRange,
      date_range_start:
        dateRange === "custom" && customStart
          ? new Date(customStart).toISOString()
          : null,
      date_range_end:
        dateRange === "custom" && customEnd
          ? new Date(customEnd).toISOString()
          : null,
      domains: selectedDomains,
    };
    const localId = `local-${Date.now()}`;
    setActiveRequestId(localId);
    // Kick off polling BEFORE the POST so we see the row land even
    // if the POST connection drops at the proxy timeout.
    startPolling();

    try {
      // No abort, no timeout — the backend may take minutes. If the
      // proxy disconnects, the polling loop above will surface the
      // server-side job state anyway.
      const r = await fetch("/api/exports", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ requested_format: format, filters }),
      });
      if (!r.ok && r.status !== 504 && r.status !== 502) {
        let msg = `Create failed (${r.status}).`;
        try {
          const body = await r.json();
          if (body?.detail?.message) msg = body.detail.message;
          else if (typeof body?.detail === "string") msg = body.detail;
        } catch {
          /* default */
        }
        setError(msg);
        setActiveRequestId(null);
        return;
      }
      // Successful POST OR proxy timeout: in both cases the job is
      // running server-side. The polling loop will refresh the list
      // and the new job shows up.
      // Refresh once eagerly so the user sees movement.
      const refresh = await fetch("/api/exports", {
        credentials: "include",
      });
      if (refresh.ok) {
        setJobs((await refresh.json()) as ExportJob[]);
      }
      setOpen(false);
    } catch {
      // Network error → still keep polling; the server-side job may
      // be running.
      setError(
        "Network error while submitting. The export may still be running — watch the list below.",
      );
    } finally {
      setActiveRequestId(null);
    }
  }

  async function deleteJob(id: string) {
    if (!confirm("Delete this export? Files are removed immediately.")) {
      return;
    }
    try {
      const r = await fetch(`/api/exports/${id}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!r.ok) {
        alert(`Delete failed (${r.status}).`);
        return;
      }
      setJobs((prev) => prev.filter((j) => j.id !== id));
    } catch (e) {
      alert((e as Error).message);
    }
  }

  function downloadHref(jobId: string, fileType: string): string {
    return `/api/exports/${encodeURIComponent(jobId)}/download?file_type=${encodeURIComponent(fileType)}`;
  }

  return (
    <div className="mt-8">
      {/* New-export form */}
      <section>
        <div className="flex items-center justify-between">
          <h2 className="font-serif text-2xl">Create a new export</h2>
          {!open && (
            <button
              type="button"
              onClick={() => setOpen(true)}
              className="rounded-md border border-accent/40 bg-accent/10 px-3 py-1.5 text-sm text-accent hover:bg-accent/20"
            >
              New export
            </button>
          )}
        </div>
        {open && (
          <form
            onSubmit={submit}
            className="mt-4 space-y-5 rounded-md border border-muted/15 bg-surface p-4"
          >
            <fieldset>
              <legend className="text-xs uppercase tracking-widest text-muted">
                Date range
              </legend>
              <div className="mt-2 space-y-2 text-sm">
                {(
                  [
                    ["all", "All time"],
                    ["last_90d", "Last 90 days"],
                    ["last_1y", "Last 1 year"],
                    ["custom", "Custom range"],
                  ] as const
                ).map(([val, label]) => (
                  <label
                    key={val}
                    className="flex items-center gap-2"
                  >
                    <input
                      type="radio"
                      name="date_range"
                      value={val}
                      checked={dateRange === val}
                      onChange={() => setDateRange(val)}
                    />
                    {label}
                  </label>
                ))}
              </div>
              {dateRange === "custom" && (
                <div className="mt-3 grid grid-cols-2 gap-3">
                  <label className="text-xs text-muted">
                    Start
                    <input
                      type="date"
                      value={customStart}
                      onChange={(e) => setCustomStart(e.target.value)}
                      className="mt-1 w-full rounded-md border border-muted/30 bg-bg/40 px-2 py-1.5 text-sm"
                    />
                  </label>
                  <label className="text-xs text-muted">
                    End (defaults to today)
                    <input
                      type="date"
                      value={customEnd}
                      onChange={(e) => setCustomEnd(e.target.value)}
                      className="mt-1 w-full rounded-md border border-muted/30 bg-bg/40 px-2 py-1.5 text-sm"
                    />
                  </label>
                </div>
              )}
            </fieldset>

            <fieldset>
              <legend className="text-xs uppercase tracking-widest text-muted">
                What to include
              </legend>
              <div className="mt-2 space-y-2 text-sm">
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    checked={domains.clinical}
                    onChange={() => toggleDomain("clinical")}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">Clinical</span>
                    <span className="text-muted">
                      {" "}
                      &mdash; conditions, procedures, medications,
                      encounters, lab + imaging, notes.
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    checked={domains.body_signals}
                    onChange={() => toggleDomain("body_signals")}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">
                      Body signals / measured health data
                    </span>
                    <span className="text-muted">
                      {" "}
                      &mdash; heart rate, sleep, workouts, steps
                      (from HealthKit / Auto Export). May be large.
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    checked={domains.calendar}
                    onChange={() => toggleDomain("calendar")}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">Calendar / life context</span>
                    <span className="text-muted">
                      {" "}
                      &mdash; calendar events used to give context
                      to clinical questions.
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2 opacity-60">
                  <input
                    type="checkbox"
                    disabled
                    aria-disabled="true"
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">
                      AI summaries / conversations
                    </span>
                    <span className="text-muted">
                      {" "}
                      &mdash; Ask / Chat conversations, dossier briefs.{" "}
                      <em>Coming soon.</em> Not included in today&rsquo;s
                      exports.
                    </span>
                  </span>
                </label>
              </div>
            </fieldset>

            <fieldset>
              <legend className="text-xs uppercase tracking-widest text-muted">
                Format
              </legend>
              <div className="mt-2 space-y-2 text-sm">
                <label className="flex items-start gap-2">
                  <input
                    type="radio"
                    name="format"
                    value="all"
                    checked={format === "all"}
                    onChange={() => setFormat("all")}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">
                      OwnChart JSON + TXT packet
                    </span>
                    <span className="text-muted">
                      {" "}
                      &mdash; recommended. Machine + human readable.
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2">
                  <input
                    type="radio"
                    name="format"
                    value="ownchart_json"
                    checked={format === "ownchart_json"}
                    onChange={() => setFormat("ownchart_json")}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">OwnChart JSON only</span>
                  </span>
                </label>
                <label className="flex items-start gap-2">
                  <input
                    type="radio"
                    name="format"
                    value="txt"
                    checked={format === "txt"}
                    onChange={() => setFormat("txt")}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">TXT packet only</span>
                  </span>
                </label>
                <label className="flex items-start gap-2">
                  <input
                    type="radio"
                    name="format"
                    value="pictal_json"
                    checked={format === "pictal_json"}
                    onChange={() => setFormat("pictal_json")}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">
                      Pictal Health JSON
                    </span>
                    <span className="text-muted">
                      {" "}
                      &mdash; structured health-history JSON for
                      import into Pictal Health. This is a download
                      you import yourself; OwnChart does not send
                      anything to Pictal. High-volume body-signal
                      data (HealthKit / auto-export rows) is not
                      included in the Pictal mapping.
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2 opacity-60">
                  <input
                    type="radio"
                    disabled
                    aria-disabled="true"
                    className="mt-1"
                  />
                  <span>
                    <span className="text-ink">CCDA XML</span>
                    <span className="text-muted">
                      {" "}
                      &mdash; <em>Coming soon.</em> Not yet wired in
                      the export pipeline.
                    </span>
                  </span>
                </label>
              </div>
            </fieldset>

            {error && (
              <p
                role="alert"
                className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning"
              >
                {error}
              </p>
            )}

            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  setError(null);
                }}
                disabled={activeRequestId !== null}
                className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:border-muted/60 hover:text-ink"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={activeRequestId !== null}
                className="rounded-md border border-accent/40 bg-accent/10 px-4 py-1.5 text-sm font-medium text-accent transition-colors hover:bg-accent/20 disabled:opacity-60"
              >
                {activeRequestId !== null
                  ? "Starting export…"
                  : "Create export"}
              </button>
            </div>
          </form>
        )}
      </section>

      {/* List */}
      <section className="mt-10">
        <h2 className="font-serif text-2xl">Recent exports</h2>
        {jobs.length === 0 ? (
          <p className="mt-3 text-sm text-muted">
            No exports yet. Click &ldquo;New export&rdquo; above to
            create one.
          </p>
        ) : (
          <ul className="mt-4 space-y-3">
            {jobs.map((j) => {
              const expired = jobIsExpired(j);
              const badge = statusBadge(j.status, expired);
              return (
                <li
                  key={j.id}
                  className="rounded-md border border-muted/15 bg-surface p-4"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <p className="font-medium text-ink">
                        Export from {fmtDateTime(j.requested_at)}
                      </p>
                      <p className="mt-0.5 text-xs text-muted">
                        Format: {j.requested_format}
                        {j.filters && (
                          <>
                            {" · Range: "}
                            {j.filters.date_range_kind === "custom"
                              ? `${j.filters.date_range_start?.slice(0, 10) ?? "?"} → ${j.filters.date_range_end?.slice(0, 10) ?? "today"}`
                              : j.filters.date_range_kind.replace("_", " ")}
                            {" · Domains: "}
                            {j.filters.domains.join(", ")}
                          </>
                        )}
                        {j.expires_at && !expired && (
                          <>
                            {" · Expires: "}
                            {fmtDateTime(j.expires_at)}
                          </>
                        )}
                      </p>
                      {j.error_message && (
                        <p className="mt-1 text-xs text-warning">
                          Error: {j.error_message}
                        </p>
                      )}
                    </div>
                    <span
                      className={
                        "shrink-0 rounded-md border px-2 py-0.5 text-xs " +
                        badge.cls
                      }
                    >
                      {badge.label}
                    </span>
                  </div>
                  {j.status === "completed" && !expired && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      {j.files.map((f) => (
                        <a
                          key={f.id}
                          href={downloadHref(j.id, f.file_type)}
                          download
                          className="rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/10"
                        >
                          Download {fileTypeLabel(f.file_type)} (
                          {fmtBytes(f.byte_size)})
                        </a>
                      ))}
                      <button
                        type="button"
                        onClick={() => deleteJob(j.id)}
                        className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:border-warning/60 hover:text-warning"
                      >
                        Delete
                      </button>
                    </div>
                  )}
                  {(j.status === "pending" || j.status === "running") && (
                    <p className="mt-3 text-xs text-muted">
                      This may take several minutes for a record with
                      a lot of body-signal data. You can leave the
                      page open and come back.
                    </p>
                  )}
                  {expired && (
                    <p className="mt-3 text-xs text-muted">
                      The download link expired 72 hours after
                      completion. Create a new export if you still
                      need the data.
                    </p>
                  )}
                  {j.status === "failed" && (
                    <button
                      type="button"
                      onClick={() => deleteJob(j.id)}
                      className="mt-3 rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:border-warning/60 hover:text-warning"
                    >
                      Dismiss
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
