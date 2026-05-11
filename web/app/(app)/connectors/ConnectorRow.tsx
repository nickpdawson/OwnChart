"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { ConnectorSummary } from "@/lib/api";

function fmtDate(iso: string | null): string {
  if (!iso) return "never";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function totalResources(counts: Record<string, number> | null): number {
  if (!counts) return 0;
  return Object.values(counts).reduce((a, b) => a + b, 0);
}

export function ConnectorRow({ c }: { c: ConnectorSummary }) {
  const router = useRouter();
  const [busy, setBusy] = useState<"connect" | "sync" | "disconnect" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncResult, setSyncResult] = useState<string | null>(null);

  async function startConnect() {
    setBusy("connect");
    setError(null);
    try {
      const r = await fetch(`/api/connectors/${c.slug}/connect`, {
        method: "POST",
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
        setError(`Connect failed (HTTP ${r.status})${detail}`);
        return;
      }
      const body = await r.json();
      // EHR will redirect back to /api/connectors/callback → /connectors?connected=slug
      window.location.href = body.authorize_url;
    } finally {
      setBusy(null);
    }
  }

  async function sync() {
    if (!c.connection) return;
    setBusy("sync");
    setError(null);
    setSyncResult(null);
    try {
      const r = await fetch(`/api/connectors/${c.connection.id}/sync`, {
        method: "POST",
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
        setError(`Sync failed (HTTP ${r.status})${detail}`);
        return;
      }
      const body = await r.json();
      setSyncResult(
        `Pulled ${totalResources(body.counts)} resource${
          totalResources(body.counts) === 1 ? "" : "s"
        }; ${body.fact_count} fact${body.fact_count === 1 ? "" : "s"} created.`,
      );
      router.refresh();
    } finally {
      setBusy(null);
    }
  }

  async function disconnect() {
    if (!c.connection) return;
    if (!confirm(`Disconnect ${c.name}? Your tokens will be wiped and the connection marked revoked.`)) return;
    setBusy("disconnect");
    setError(null);
    try {
      const r = await fetch(`/api/connectors/${c.connection.id}/disconnect`, {
        method: "POST",
        credentials: "include",
      });
      if (!r.ok) {
        setError(`Disconnect failed (HTTP ${r.status})`);
        return;
      }
      router.refresh();
    } finally {
      setBusy(null);
    }
  }

  const connected = c.connection && c.connection.status === "connected";
  const tokenCount = totalResources(c.connection?.cached_resource_counts ?? null);

  return (
    <li className="rounded-xl border border-muted/15 bg-surface p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <p className="font-medium">{c.name}</p>
          <p className="mt-0.5 text-xs text-muted">
            {c.ehr_vendor || "unknown vendor"}
          </p>
          <p className="mt-0.5 break-all text-xs text-muted">{c.fhir_base}</p>
          {!c.has_client_id && (
            <span className="mt-2 inline-block rounded-md bg-caution/15 px-2 py-0.5 text-xs text-caution">
              client_id not configured
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:flex-nowrap sm:gap-2">
          {connected ? (
            <>
              <button
                onClick={sync}
                disabled={busy !== null}
                className="inline-flex min-h-[44px] flex-1 items-center justify-center rounded-lg bg-accent px-4 py-2.5 text-sm text-surface disabled:opacity-50 sm:flex-initial"
              >
                {busy === "sync" ? "Syncing…" : "Sync now"}
              </button>
              <button
                onClick={disconnect}
                disabled={busy !== null}
                className="inline-flex min-h-[44px] flex-1 items-center justify-center rounded-lg border border-muted/30 px-4 py-2.5 text-sm hover:bg-bg disabled:opacity-50 sm:flex-initial"
              >
                {busy === "disconnect" ? "…" : "Disconnect"}
              </button>
            </>
          ) : (
            <button
              onClick={startConnect}
              disabled={busy !== null || !c.has_client_id || !c.enabled}
              className="inline-flex min-h-[44px] w-full items-center justify-center rounded-lg bg-accent px-4 py-2.5 text-sm text-surface disabled:opacity-50 sm:w-auto"
            >
              {busy === "connect" ? "Opening…" : "Connect"}
            </button>
          )}
        </div>
      </div>

      {c.connection && (
        <p className="mt-3 text-xs text-muted">
          Status:{" "}
          <span className={connected ? "text-accent" : "text-caution"}>
            {c.connection.status}
          </span>
          {c.connection.patient_display_name ? ` · ${c.connection.patient_display_name}` : ""}
          {tokenCount > 0 ? ` · ${tokenCount} resources cached` : ""} · last sync{" "}
          {fmtDate(c.connection.last_synced_at)}
        </p>
      )}

      {error && <p className="mt-2 text-sm text-caution">{error}</p>}
      {syncResult && <p className="mt-2 text-sm text-accent">{syncResult}</p>}
    </li>
  );
}
