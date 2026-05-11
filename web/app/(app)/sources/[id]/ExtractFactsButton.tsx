"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

type Job = {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  total_pages: number;
  completed_pages: number;
  facts_added: number;
  page_errors: { page: number; error: string }[];
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

const POLL_MS = 2000;
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function ExtractFactsButton({ sourceId }: { sourceId: string }) {
  const router = useRouter();
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Hydrate any in-flight or recent job for this source on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/sources/${sourceId}/extraction-status`, {
          credentials: "include",
          cache: "no-store",
        });
        if (!cancelled && r.ok) {
          const j = (await r.json()) as Job | null;
          if (j) setJob(j);
        }
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId]);

  // Whenever we have a non-terminal job, poll status.
  useEffect(() => {
    if (!job || TERMINAL.has(job.status)) {
      if (pollTimer.current) {
        clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
      // When a run JUST completed, refresh the page so the new facts appear.
      if (job && TERMINAL.has(job.status) && job.facts_added > 0) {
        router.refresh();
      }
      return;
    }
    pollTimer.current = setTimeout(async () => {
      try {
        const r = await fetch(`/api/sources/${sourceId}/extraction-status`, {
          credentials: "include",
          cache: "no-store",
        });
        if (r.ok) {
          const j = (await r.json()) as Job | null;
          if (j) setJob(j);
        }
      } catch {
        /* ignore */
      }
    }, POLL_MS);
    return () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, [job, sourceId, router]);

  async function startJob() {
    if (busy) return;
    if (!confirm("This will send each page image to Claude (consent-gated, paid). Continue?")) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`/api/sources/${sourceId}/extract-facts`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({}),
      });
      if (r.status === 412) {
        setError("Enable global LLM consent first.");
        return;
      }
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j?.detail ? ` — ${j.detail}` : "";
        } catch {
          /* ignore */
        }
        setError(`Could not start extraction (HTTP ${r.status})${detail}`);
        return;
      }
      const j = (await r.json()) as Job;
      setJob(j);
    } finally {
      setBusy(false);
    }
  }

  const isInFlight = !!job && !TERMINAL.has(job.status);
  const buttonDisabled = busy || isInFlight;
  const buttonLabel = isInFlight
    ? job!.status === "pending"
      ? "Queued…"
      : `Extracting page ${job!.completed_pages + 1}${job!.total_pages ? ` of ${job!.total_pages}` : ""}…`
    : busy
    ? "Starting…"
    : "Extract facts with AI";

  return (
    <div className="flex flex-col items-end gap-1.5">
      <button
        onClick={startJob}
        disabled={buttonDisabled}
        className="rounded-lg bg-accent px-4 py-2 text-sm text-surface disabled:opacity-50"
      >
        {buttonLabel}
      </button>
      {error && <p className="text-sm text-caution">{error}</p>}

      {job && job.status === "running" && job.total_pages > 0 && (
        <div className="w-64">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted/15">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: `${Math.min(100, (job.completed_pages / job.total_pages) * 100)}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-muted">
            {job.completed_pages} / {job.total_pages} pages · {job.facts_added} facts so far
            {job.page_errors.length > 0 ? ` · ${job.page_errors.length} page error${job.page_errors.length === 1 ? "" : "s"}` : ""}
          </p>
        </div>
      )}

      {job && job.status === "completed" && (
        <p className="text-sm text-muted">
          {job.facts_added} fact{job.facts_added === 1 ? "" : "s"} extracted across {job.total_pages} page{job.total_pages === 1 ? "" : "s"}
          {job.page_errors.length > 0 ? ` · ${job.page_errors.length} page error${job.page_errors.length === 1 ? "" : "s"}` : ""}
        </p>
      )}

      {job && job.status === "failed" && (
        <p className="text-sm text-caution">
          Extraction failed{job.error ? ` — ${job.error}` : ""}.
        </p>
      )}
    </div>
  );
}
