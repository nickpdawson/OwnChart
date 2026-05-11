"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

type AutoExportSummary = {
  id: string;
  raw_metadata?: {
    fact_count?: number;
    workout_count?: number;
    sleep_session_count?: number;
    metric_counts?: Record<string, number>;
    skipped_metrics?: string[];
    parse_warnings?: string[];
  } | null;
};

type PushConfig = {
  push_url: string;
  token: string | null;
  configured: boolean;
};

export function AutoExportUploader() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<AutoExportSummary | null>(null);
  const [pushConfig, setPushConfig] = useState<PushConfig | null>(null);
  const [revealToken, setRevealToken] = useState(false);

  // Fetch the push URL + token so the user can paste them into the
  // iOS app's Automations → REST API screen.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/auto-export/config", {
          credentials: "include",
          cache: "no-store",
        });
        if (!cancelled && r.ok) {
          const c = (await r.json()) as PushConfig;
          setPushConfig(c);
        }
      } catch {
        /* ignore — file upload still works */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      /* user can long-press to copy */
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setDone(null);
    const file = fileRef.current?.files?.[0];
    if (!file) {
      setError("Choose a Health Auto Export JSON file first.");
      return;
    }
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      if (label) fd.append("source_label", label);
      const r = await fetch("/api/sources/auto-export", {
        method: "POST",
        body: fd,
        credentials: "include",
      });
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j?.detail ? ` — ${j.detail}` : "";
        } catch {
          /* ignore */
        }
        setError(`Upload failed (HTTP ${r.status})${detail}`);
        return;
      }
      const body = (await r.json()) as AutoExportSummary;
      setDone(body);
      setLabel("");
      if (fileRef.current) fileRef.current.value = "";
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  const counts = done?.raw_metadata;
  const metricEntries = counts?.metric_counts
    ? Object.entries(counts.metric_counts).sort((a, b) => b[1] - a[1])
    : [];

  return (
    <div className="grid gap-5">
      {/* Push (REST) — preferred. Continuous, no manual files. */}
      {pushConfig && (
        <section className="rounded-lg border border-muted/15 bg-bg/40 p-4">
          <p className="text-xs uppercase tracking-widest text-muted">Stream from your phone (recommended)</p>
          <p className="mt-2 text-sm text-muted">
            The iOS app can POST new data automatically as it accumulates — no
            re-exporting the whole history. Open <em>Health Auto Export →
            Automations → REST API</em> and paste:
          </p>
          {!pushConfig.configured ? (
            <p className="mt-3 rounded-md bg-caution/10 p-3 text-sm text-caution">
              The server&apos;s <code>OWNCHART_AUTO_EXPORT_TOKEN</code> isn&apos;t
              configured yet. Run <code>bash infra/deploy.sh</code> to generate
              one (or set it in <code>infra/.env</code> manually), then come
              back to this page to copy the credentials.
            </p>
          ) : (
            <div className="mt-3 grid gap-3 text-sm">
              <div>
                <p className="text-xs uppercase tracking-widest text-muted">URL</p>
                <div className="mt-1 flex items-center gap-2">
                  <code className="flex-1 truncate rounded bg-bg px-2 py-1.5 text-xs">
                    {pushConfig.push_url}
                  </code>
                  <button
                    type="button"
                    onClick={() => copy(pushConfig.push_url)}
                    className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:bg-bg"
                  >
                    Copy
                  </button>
                </div>
              </div>
              <div>
                <p className="text-xs uppercase tracking-widest text-muted">
                  Authorization header (the iOS app asks for key + value separately)
                </p>
                <div className="mt-1 grid gap-2">
                  <div className="flex items-center gap-2">
                    <span className="w-16 shrink-0 text-xs text-muted">Key</span>
                    <code className="flex-1 truncate rounded bg-bg px-2 py-1.5 text-xs">
                      Authorization
                    </code>
                    <button
                      type="button"
                      onClick={() => copy("Authorization")}
                      className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:bg-bg"
                    >
                      Copy
                    </button>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="w-16 shrink-0 text-xs text-muted">Value</span>
                    <code className="flex-1 truncate rounded bg-bg px-2 py-1.5 font-mono text-xs">
                      Bearer{" "}
                      {revealToken
                        ? pushConfig.token
                        : pushConfig.token
                        ? "•".repeat(Math.min(pushConfig.token.length, 32))
                        : ""}
                    </code>
                    <button
                      type="button"
                      onClick={() => setRevealToken((v) => !v)}
                      className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:bg-bg"
                    >
                      {revealToken ? "Hide" : "Show"}
                    </button>
                    <button
                      type="button"
                      onClick={() => pushConfig.token && copy(`Bearer ${pushConfig.token}`)}
                      className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:bg-bg"
                    >
                      Copy
                    </button>
                  </div>
                </div>
              </div>
              <p className="text-xs text-muted">
                Method <code>POST</code> · Body <code>JSON</code> · The iOS app
                handles incremental pushes automatically once the URL + header
                are saved. Each push appears here as a new source.
              </p>
            </div>
          )}
        </section>
      )}

      {/* Manual file upload — works without iOS app config, good for
          backfilling history. */}
      <form onSubmit={submit} className="grid gap-3">
        <p className="text-xs uppercase tracking-widest text-muted">Or upload an export file</p>
      <p className="text-xs text-muted">
        Export from the Health Auto Export iOS app → JSON. V1 ingests sleep,
        workouts, steps, heart rate, resting HR, HRV, VO₂ max, body metrics,
        and exercise time. Lab values come in via the clinical lane (FHIR /
        CCDA) — not this one.
      </p>
      <label className="text-sm">
        Health Auto Export JSON
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          required
          className="mt-1 block w-full text-sm"
        />
      </label>
      <label className="text-sm">
        Label (optional)
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="e.g. Apple Watch Q4 2025 export"
          className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
        />
      </label>
      {error && <p className="text-sm text-caution">{error}</p>}
      {done && counts && (
        <div className="text-sm">
          <p className="text-accent">
            Imported — id {done.id.slice(0, 8)}…
            {counts.fact_count != null ? ` · ${counts.fact_count} facts` : ""}
            {counts.workout_count != null && counts.workout_count > 0
              ? ` · ${counts.workout_count} workouts`
              : ""}
            {counts.sleep_session_count != null && counts.sleep_session_count > 0
              ? ` · ${counts.sleep_session_count} sleep sessions`
              : ""}
          </p>
          {metricEntries.length > 0 && (
            <p className="mt-1 text-xs text-muted">
              Metrics:{" "}
              {metricEntries
                .slice(0, 8)
                .map(([k, v]) => `${k}: ${v}`)
                .join(" · ")}
              {metricEntries.length > 8 ? ` … +${metricEntries.length - 8} more` : ""}
            </p>
          )}
          {counts.skipped_metrics && counts.skipped_metrics.length > 0 && (
            <p className="mt-1 text-xs text-muted">
              Skipped (out-of-scope or unknown):{" "}
              {counts.skipped_metrics.slice(0, 8).join(", ")}
              {counts.skipped_metrics.length > 8 ? "…" : ""}
            </p>
          )}
        </div>
      )}
      <button
        type="submit"
        disabled={busy}
        className="justify-self-start rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50"
      >
        {busy ? "Importing…" : "Upload Auto Export"}
      </button>
      </form>
    </div>
  );
}
