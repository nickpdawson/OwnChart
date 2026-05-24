"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { DirectoryEntry } from "@/lib/api";

type Mode = "search" | "athena" | "modmed" | "cerner" | "manual";

const ATHENA_FHIR_BASE_PROD = "https://api.platform.athenahealth.com/fhir/r4/";
const ATHENA_FHIR_BASE_PREVIEW = "https://api.preview.platform.athenahealth.com/fhir/r4/";

// Centra Health (Lynchburg, VA) — the PM-named Beta 1 Cerner smoke
// target. Surfaced as a quick-add chip on the Cerner tab so Nick
// doesn't have to search for it. Real users on other Cerner
// tenants use the directory search instead.
const CENTRA_HEALTH_FHIR_BASE =
  "https://fhir-myrecord.cerner.com/r4/ab208292-75a1-4788-9fc7-1e9a40a7eee3/";
const CENTRA_HEALTH_NAME = "Centra Health, Inc.";

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
  const [manualVendor, setManualVendor] = useState<"athena" | "cerner" | "epic" | "modmed" | "unknown">("athena");

  // athena fields
  const [athenaName, setAthenaName] = useState("");
  const [athenaEnv, setAthenaEnv] = useState<"production" | "preview">("production");

  // modmed fields — ModMed (also branded as EMA, Electronic Medical
  // Assistant) is per-practice; there's no shared multi-tenant base
  // URL like Athena. The user gets the fhir_base from their practice
  // or from ModMed's FHIR vendor dashboard.
  const [modmedName, setModmedName] = useState("");
  const [modmedUrl, setModmedUrl] = useState("");

  // cerner / Oracle Health Millennium fields. Per-tenant fhir_base
  // under fhir-myrecord.cerner.com/r4/<tenant>/. The Cerner tab
  // searches Oracle's public millennium_patient_r4_endpoints.json
  // (proxied via /api/connectors/directory/search?vendor=cerner)
  // and offers a quick-add chip for the Centra Health Beta 1 smoke
  // target. A manual paste fallback handles tenants Oracle's
  // published Bundle hasn't indexed yet.
  const [cernerQuery, setCernerQuery] = useState("");
  const [cernerResults, setCernerResults] = useState<DirectoryEntry[] | null>(null);
  const [cernerSearching, setCernerSearching] = useState(false);
  const [cernerManualName, setCernerManualName] = useState("");
  const [cernerManualUrl, setCernerManualUrl] = useState("");

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

  async function searchCerner(e?: React.FormEvent) {
    e?.preventDefault();
    setError(null);
    setCernerSearching(true);
    setCernerResults(null);
    try {
      const url =
        `/api/connectors/directory/search?q=${encodeURIComponent(cernerQuery)}` +
        `&vendor=cerner&limit=25`;
      const r = await fetch(url, { credentials: "include", cache: "no-store" });
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
      setCernerResults((await r.json()) as DirectoryEntry[]);
    } finally {
      setCernerSearching(false);
    }
  }

  async function quickAddCentraHealth() {
    await add({
      name: CENTRA_HEALTH_NAME,
      fhir_base: CENTRA_HEALTH_FHIR_BASE,
      ehr_vendor: "cerner",
    });
  }

  async function addCernerManual(e: React.FormEvent) {
    e.preventDefault();
    const name = cernerManualName.trim();
    const url = cernerManualUrl.trim();
    if (!name) {
      setError("Health system / hospital name is required.");
      return;
    }
    if (!url) {
      setError("Cerner FHIR base URL is required.");
      return;
    }
    if (!/^https:\/\//i.test(url)) {
      setError(
        "FHIR base URL should start with https://. (Backend validation is authoritative; this is a hint.)",
      );
      return;
    }
    await add({ name, fhir_base: url, ehr_vendor: "cerner" });
    setCernerManualName("");
    setCernerManualUrl("");
  }

  async function addModmed(e: React.FormEvent) {
    e.preventDefault();
    const name = modmedName.trim();
    const url = modmedUrl.trim();
    if (!name) {
      setError("Practice name is required.");
      return;
    }
    if (!url) {
      setError("ModMed FHIR base URL is required.");
      return;
    }
    if (!/^https:\/\//i.test(url)) {
      setError(
        "FHIR base URL should start with https://. (Backend validation is authoritative; this is a hint.)",
      );
      return;
    }
    await add({ name, fhir_base: url, ehr_vendor: "modmed" });
    setModmedName("");
    setModmedUrl("");
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
        <TabButton id="modmed" label="ModMed / EMA" />
        <TabButton id="cerner" label="Cerner / Oracle Health" />
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

      {mode === "modmed" && (
        <form onSubmit={addModmed} className="grid gap-3 pt-4">
          <p className="text-xs text-muted">
            ModMed (also branded <strong>EMA</strong>, Electronic Medical
            Assistant) is a per-practice EHR &mdash; every practice gets
            its own FHIR base URL. You&rsquo;ll need the FHIR base URL
            from your practice or from the ModMed FHIR vendor dashboard
            that the operator of this OwnChart instance set up.
          </p>
          <label className="text-sm">
            Practice name
            <input
              type="text"
              value={modmedName}
              onChange={(e) => setModmedName(e.target.value)}
              placeholder="e.g. Bridger Orthopedic & Sports Medicine"
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
            />
          </label>
          <label className="text-sm">
            ModMed FHIR base URL
            <input
              type="url"
              value={modmedUrl}
              onChange={(e) => setModmedUrl(e.target.value)}
              placeholder="https://stage.ema-api.com/ema-dev/firm/<practice>/ema/fhir/v2/"
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
            />
          </label>
          <p className="text-xs text-muted">
            On Connect, you&rsquo;ll be redirected to ModMed&rsquo;s
            login screen. Sign in with the{" "}
            <strong>patient-portal credentials</strong> your practice
            gave you (the same ones you use to view your records on
            their patient app) &mdash; not developer credentials.
          </p>
          <p className="text-xs text-muted">
            The ModMed client ID is configured by the OwnChart operator
            via the <code>OWNCHART_MODMED_CLIENT_ID</code> environment
            variable; you don&rsquo;t enter it here.
          </p>
          {error && <p className="text-sm text-caution">{error}</p>}
          <button
            type="submit"
            disabled={adding !== null}
            className="justify-self-start rounded-lg bg-accent px-4 py-2 text-sm text-surface disabled:opacity-50"
          >
            {adding ? "Adding…" : "Add ModMed connector"}
          </button>
        </form>
      )}

      {mode === "cerner" && (
        <div className="pt-4">
          <p className="text-xs text-muted">
            Oracle Health Millennium (formerly Cerner) hosts your
            records under a per-tenant URL on{" "}
            <code>fhir-myrecord.cerner.com</code>. Search Oracle&rsquo;s
            published Millennium patient R4 endpoint directory for
            your health system, or paste your tenant URL directly
            below.
          </p>

          {/* Quick-add: Centra Health (Lynchburg, VA) is the Beta 1
              smoke target Nick named. Surfacing as a chip so the
              user doesn't have to type or search. */}
          <div className="mt-3 rounded-md border border-muted/10 bg-bg/40 p-3 text-sm">
            <p className="text-xs uppercase tracking-widest text-muted">
              Quick add
            </p>
            <div className="mt-2 flex flex-wrap items-baseline justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate">{CENTRA_HEALTH_NAME}</p>
                <p className="break-all text-xs text-muted">
                  {CENTRA_HEALTH_FHIR_BASE}
                </p>
              </div>
              <button
                type="button"
                onClick={quickAddCentraHealth}
                disabled={adding === CENTRA_HEALTH_NAME}
                className="shrink-0 rounded-md bg-accent px-2.5 py-1 text-xs text-surface disabled:opacity-50"
              >
                {adding === CENTRA_HEALTH_NAME ? "Adding…" : "Add"}
              </button>
            </div>
          </div>

          {/* Search Oracle's published directory by name / alias. */}
          <form onSubmit={searchCerner} className="mt-4 flex flex-wrap gap-2">
            <input
              type="text"
              value={cernerQuery}
              onChange={(e) => setCernerQuery(e.target.value)}
              placeholder='Health system name (e.g. "Centra", "Adventist")'
              className="flex-1 min-w-[12rem] rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
            />
            <button
              type="submit"
              disabled={cernerSearching}
              className="rounded-lg bg-accent px-3 py-2 text-sm text-surface disabled:opacity-50"
            >
              {cernerSearching ? "Searching…" : "Search"}
            </button>
          </form>

          {error && <p className="mt-3 text-sm text-caution">{error}</p>}

          {cernerResults && (
            <ul className="mt-4 max-h-96 space-y-1 overflow-auto rounded-md border border-muted/10">
              {cernerResults.length === 0 ? (
                <li className="px-3 py-2 text-sm text-muted">
                  No matches in Oracle&rsquo;s published directory. If
                  you know your tenant URL, use the manual paste below.
                </li>
              ) : (
                cernerResults.map((r) => (
                  <li
                    key={`${r.name}|${r.fhir_base}`}
                    className="flex flex-wrap items-baseline justify-between gap-2 border-b border-muted/10 px-3 py-2 last:border-b-0"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm">{r.name}</p>
                      <p className="break-all text-xs text-muted">{r.fhir_base}</p>
                    </div>
                    <button
                      onClick={() =>
                        add({
                          name: r.name,
                          fhir_base: r.fhir_base,
                          ehr_vendor: "cerner",
                        })
                      }
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

          {/* Manual paste fallback — for tenants not yet in Oracle's
              published directory, or when the user already knows the
              tenant URL. */}
          <details className="mt-6 rounded-md border border-muted/15 bg-surface p-3">
            <summary className="cursor-pointer text-sm font-medium text-ink">
              Paste a Cerner tenant URL directly
            </summary>
            <form onSubmit={addCernerManual} className="mt-3 grid gap-3">
              <p className="text-xs text-muted">
                Use this if your health system isn&rsquo;t in
                Oracle&rsquo;s directory yet, or you already have the
                tenant URL from your patient portal&rsquo;s SMART
                configuration.
              </p>
              <label className="text-sm">
                Health system name
                <input
                  type="text"
                  value={cernerManualName}
                  onChange={(e) => setCernerManualName(e.target.value)}
                  placeholder="e.g. Your Health System"
                  className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
                />
              </label>
              <label className="text-sm">
                Cerner FHIR base URL
                <input
                  type="url"
                  value={cernerManualUrl}
                  onChange={(e) => setCernerManualUrl(e.target.value)}
                  placeholder="https://fhir-myrecord.cerner.com/r4/<tenant-id>/"
                  className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
                />
              </label>
              <button
                type="submit"
                disabled={adding !== null}
                className="justify-self-start rounded-lg bg-accent px-4 py-2 text-sm text-surface disabled:opacity-50"
              >
                {adding ? "Adding…" : "Add Cerner connector"}
              </button>
            </form>
          </details>

          <p className="mt-3 text-xs text-muted">
            On Connect, you&rsquo;ll be redirected to
            <code> authorization.cerner.com</code>. Sign in with your{" "}
            <strong>patient-portal credentials</strong> for that
            health system. The Cerner client ID is operator config
            via the <code>OWNCHART_CERNER_CLIENT_ID</code> env var;
            you don&rsquo;t enter it here.
          </p>
        </div>
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
              <option value="modmed">ModMed (Modernizing Medicine)</option>
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
