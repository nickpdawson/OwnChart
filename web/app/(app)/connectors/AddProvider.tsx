"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { DirectoryEntry } from "@/lib/api";

type Mode = "search" | "athena" | "manual";

const ATHENA_FHIR_BASE_PROD = "https://api.platform.athenahealth.com/fhir/r4/";
const ATHENA_FHIR_BASE_PREVIEW = "https://api.preview.platform.athenahealth.com/fhir/r4/";

export function AddProvider() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("search");
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

  // athena fields
  const [athenaName, setAthenaName] = useState("");
  const [athenaEnv, setAthenaEnv] = useState<"production" | "preview">("production");

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

  async function addAthena(e: React.FormEvent) {
    e.preventDefault();
    const name = athenaName.trim();
    if (!name) {
      setError("Practice name is required (this is what the connector will be labeled).");
      return;
    }
    const fhirBase = athenaEnv === "preview" ? ATHENA_FHIR_BASE_PREVIEW : ATHENA_FHIR_BASE_PROD;
    await add({ name, fhir_base: fhirBase, ehr_vendor: "athena" });
    setAthenaName("");
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

  function TabButton({ id, label }: { id: Mode; label: string }) {
    return (
      <button
        type="button"
        onClick={() => {
          setMode(id);
          setError(null);
        }}
        className={`-mb-px border-b-2 px-3 py-2 text-sm ${
          mode === id ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink"
        }`}
      >
        {label}
      </button>
    );
  }

  return (
    <div className="mt-3 rounded-xl border border-muted/15 bg-surface p-4">
      <div className="flex flex-wrap gap-1 border-b border-muted/10">
        <TabButton id="search" label="Search Epic directory" />
        <TabButton id="athena" label="athenahealth" />
        <TabButton id="manual" label="Paste FHIR URL" />
      </div>

      {mode === "search" && (
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
      )}

      {mode === "athena" && (
        <form onSubmit={addAthena} className="grid gap-3 pt-4">
          <p className="text-xs text-muted">
            athenahealth doesn&apos;t publish a per-practice endpoint directory — every
            athena patient portal shares the same FHIR base, and your practice is
            identified by who you sign in as. Just name the connector so you
            recognize it in your list, then click Connect on the next screen and
            sign in with the athenaPatient credentials your practice gave you.
          </p>
          <label className="text-sm">
            Practice name
            <input
              type="text"
              value={athenaName}
              onChange={(e) => setAthenaName(e.target.value)}
              placeholder="e.g. Bridger Orthopedic & Sports Medicine"
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
            />
          </label>
          <fieldset className="text-sm">
            <legend>Environment</legend>
            <div className="mt-1 flex flex-wrap gap-4">
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="athena-env"
                  value="production"
                  checked={athenaEnv === "production"}
                  onChange={() => setAthenaEnv("production")}
                />
                <span>Production</span>
                <span className="text-xs text-muted">(real patient data)</span>
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="athena-env"
                  value="preview"
                  checked={athenaEnv === "preview"}
                  onChange={() => setAthenaEnv("preview")}
                />
                <span>Preview / sandbox</span>
                <span className="text-xs text-muted">(test data only)</span>
              </label>
            </div>
          </fieldset>
          <p className="text-xs text-muted">
            FHIR base:{" "}
            <code className="break-all">
              {athenaEnv === "preview" ? ATHENA_FHIR_BASE_PREVIEW : ATHENA_FHIR_BASE_PROD}
            </code>
          </p>
          {error && <p className="text-sm text-caution">{error}</p>}
          <button
            type="submit"
            disabled={adding !== null}
            className="justify-self-start rounded-lg bg-accent px-4 py-2 text-sm text-surface disabled:opacity-50"
          >
            {adding ? "Adding…" : "Add athenahealth connector"}
          </button>
        </form>
      )}

      {mode === "manual" && (
        <form onSubmit={addManual} className="grid gap-3 pt-4">
          <p className="text-xs text-muted">
            Use this for vendors without a public directory or when you already
            know the FHIR base URL from your provider&apos;s developer docs.
          </p>
          <label className="text-sm">
            Provider name
            <input
              type="text"
              value={manualName}
              onChange={(e) => setManualName(e.target.value)}
              placeholder="e.g. My Provider"
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
            />
          </label>
          <label className="text-sm">
            FHIR base URL
            <input
              type="url"
              value={manualUrl}
              onChange={(e) => setManualUrl(e.target.value)}
              placeholder="https://fhir.example.com/api/FHIR/R4/"
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
