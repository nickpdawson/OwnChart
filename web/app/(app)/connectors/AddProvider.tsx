"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { DirectoryEntry } from "@/lib/api";

export function AddProvider() {
  const router = useRouter();
  const [mode, setMode] = useState<"search" | "manual">("search");
  const [vendor, setVendor] = useState("epic");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<DirectoryEntry[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState<string | null>(null);

  // manual fields
  const [manualName, setManualName] = useState("");
  const [manualUrl, setManualUrl] = useState("");
  const [manualVendor, setManualVendor] = useState<"athena" | "cerner" | "epic" | "unknown">("athena");

  async function search(e?: React.FormEvent) {
    e?.preventDefault();
    setError(null);
    setSearching(true);
    setResults(null);
    try {
      const r = await fetch(`/api/connectors/directory/search?q=${encodeURIComponent(query)}&vendor=${vendor}&limit=25`, {
        credentials: "include",
        cache: "no-store",
      });
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j?.detail ? ` — ${j.detail}` : "";
        } catch {
          /* ignore */
        }
        setError(`Search failed (HTTP ${r.status})${detail}`);
        return;
      }
      setResults((await r.json()) as DirectoryEntry[]);
    } finally {
      setSearching(false);
    }
  }

  async function add(entry: { name: string; fhir_base: string; ehr_vendor: string }) {
    setAdding(entry.name);
    setError(null);
    try {
      const r = await fetch("/api/connectors", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(entry),
      });
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j?.detail ? ` — ${j.detail}` : "";
        } catch {
          /* ignore */
        }
        setError(`Add failed (HTTP ${r.status})${detail}`);
        return;
      }
      router.refresh();
    } finally {
      setAdding(null);
    }
  }

  async function addManual(e: React.FormEvent) {
    e.preventDefault();
    if (!manualName.trim() || !manualUrl.trim()) {
      setError("Both name and FHIR base URL are required.");
      return;
    }
    await add({ name: manualName.trim(), fhir_base: manualUrl.trim(), ehr_vendor: manualVendor });
    setManualName("");
    setManualUrl("");
  }

  return (
    <div className="mt-3 rounded-xl border border-muted/15 bg-surface p-4">
      <div className="flex gap-1 border-b border-muted/10">
        <button
          type="button"
          onClick={() => setMode("search")}
          className={`-mb-px border-b-2 px-3 py-2 text-sm ${
            mode === "search" ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink"
          }`}
        >
          Search Epic directory
        </button>
        <button
          type="button"
          onClick={() => setMode("manual")}
          className={`-mb-px border-b-2 px-3 py-2 text-sm ${
            mode === "manual" ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink"
          }`}
        >
          Paste FHIR URL
        </button>
      </div>

      {mode === "search" ? (
        <div className="pt-4">
          <form onSubmit={search} className="flex flex-wrap gap-2">
            <select
              value={vendor}
              onChange={(e) => setVendor(e.target.value)}
              className="rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
            >
              <option value="epic">Epic</option>
            </select>
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder='Hospital or system name (e.g. "Stanford")'
              className="flex-1 min-w-[12rem] rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
            />
            <button
              type="submit"
              disabled={searching}
              className="rounded-lg bg-accent px-3 py-2 text-sm text-surface disabled:opacity-50"
            >
              {searching ? "Searching…" : "Search"}
            </button>
          </form>

          {error && <p className="mt-3 text-sm text-caution">{error}</p>}

          {results && (
            <ul className="mt-4 max-h-96 space-y-1 overflow-auto rounded-md border border-muted/10">
              {results.length === 0 ? (
                <li className="px-3 py-2 text-sm text-muted">No matches.</li>
              ) : (
                results.map((r) => (
                  <li
                    key={`${r.name}|${r.fhir_base}`}
                    className="flex flex-wrap items-baseline justify-between gap-2 border-b border-muted/10 px-3 py-2 last:border-b-0"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm">{r.name}</p>
                      <p className="break-all text-xs text-muted">{r.fhir_base}</p>
                    </div>
                    <button
                      onClick={() => add({ name: r.name, fhir_base: r.fhir_base, ehr_vendor: r.ehr_vendor })}
                      disabled={adding === r.name}
                      className="rounded-md bg-accent px-2.5 py-1 text-xs text-surface disabled:opacity-50"
                    >
                      {adding === r.name ? "Adding…" : "Add"}
                    </button>
                  </li>
                ))
              )}
            </ul>
          )}
        </div>
      ) : (
        <form onSubmit={addManual} className="grid gap-3 pt-4">
          <p className="text-xs text-muted">
            Use this for vendors without a public directory (Athena, Cerner) or when
            you already know the FHIR base URL from your provider&apos;s developer
            docs.
          </p>
          <label className="text-sm">
            Provider name
            <input
              type="text"
              value={manualName}
              onChange={(e) => setManualName(e.target.value)}
              placeholder="e.g. Bridger Orthopedic & Sports Medicine"
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
            />
          </label>
          <label className="text-sm">
            FHIR base URL
            <input
              type="url"
              value={manualUrl}
              onChange={(e) => setManualUrl(e.target.value)}
              placeholder="https://api.platform.athenahealth.com/fhir/r4/<practice-id>/"
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
            />
          </label>
          <label className="text-sm">
            EHR vendor
            <select
              value={manualVendor}
              onChange={(e) => setManualVendor(e.target.value as typeof manualVendor)}
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
            >
              <option value="athena">Athena</option>
              <option value="cerner">Cerner / Oracle Health</option>
              <option value="epic">Epic</option>
              <option value="unknown">Other / unknown</option>
            </select>
          </label>
          {error && <p className="text-sm text-caution">{error}</p>}
          <button
            type="submit"
            disabled={adding !== null}
            className="justify-self-start rounded-lg bg-accent px-4 py-2 text-sm text-surface disabled:opacity-50"
          >
            {adding ? "Adding…" : "Add provider"}
          </button>
        </form>
      )}
    </div>
  );
}
