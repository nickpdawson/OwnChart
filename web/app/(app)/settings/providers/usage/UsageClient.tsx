"use client";

// /settings/providers/usage — cost attribution view (#109).
//
// Backend: GET /api/llm-providers/usage. Query params: date_from,
// date_to, provider, model, purpose, billed_to, limit, format=csv.
// V1 single-user: every authenticated user sees all rows.

import { useEffect, useMemo, useState } from "react";

type UsageRow = {
  id: string;
  created_at: string;
  provider: string;
  model: string;
  purpose: string;
  prompt_version: string | null;
  billed_to: string | null;
  billed_credential_id: string | null;
  billed_credential_label: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_input_tokens: number | null;
  cache_creation_input_tokens: number | null;
  latency_ms: number | null;
  estimated_usd_cost: number | null;
  error: string | null;
};

type UsageAggregate = {
  total_runs: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_creation_tokens: number;
  total_estimated_usd_cost: number;
  runs_with_unknown_cost: number;
};

type UsageResponse = { rows: UsageRow[]; aggregate: UsageAggregate };

function fmtNum(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

function fmtUsd(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function fmtTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function daysAgoIso(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export function UsageClient() {
  const [dateFrom, setDateFrom] = useState(daysAgoIso(7));
  const [dateTo, setDateTo] = useState(todayIso());
  const [provider, setProvider] = useState("");
  const [purpose, setPurpose] = useState("");
  const [billedTo, setBilledTo] = useState("");
  const [data, setData] = useState<UsageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function buildQuery(extra: Record<string, string> = {}): string {
    const p = new URLSearchParams();
    if (dateFrom) p.set("date_from", dateFrom);
    if (dateTo) p.set("date_to", dateTo);
    if (provider) p.set("provider", provider);
    if (purpose) p.set("purpose", purpose);
    if (billedTo) p.set("billed_to", billedTo);
    for (const [k, v] of Object.entries(extra)) p.set(k, v);
    return p.toString();
  }

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`/api/llm-providers/usage?${buildQuery()}`, {
        credentials: "include", cache: "no-store",
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData((await r.json()) as UsageResponse);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function downloadCsv() {
    const href = `/api/llm-providers/usage?${buildQuery({ format: "csv" })}`;
    // window.location vs anchor: anchor avoids leaving the SPA shell
    const a = document.createElement("a");
    a.href = href;
    a.click();
  }

  // Per-purpose breakdown derived client-side from the row set. Lets
  // the user see at a glance which feature is burning money without a
  // separate aggregation query.
  const byPurpose = useMemo(() => {
    if (!data) return [];
    const m = new Map<string, { runs: number; cost: number; unknownCost: number }>();
    for (const r of data.rows) {
      const cur = m.get(r.purpose) ?? { runs: 0, cost: 0, unknownCost: 0 };
      cur.runs += 1;
      if (r.estimated_usd_cost != null) cur.cost += r.estimated_usd_cost;
      else cur.unknownCost += 1;
      m.set(r.purpose, cur);
    }
    return [...m.entries()]
      .map(([purpose, v]) => ({ purpose, ...v }))
      .sort((a, b) => b.cost - a.cost);
  }, [data]);

  return (
    <div className="mt-6 space-y-6">
      {/* Filters */}
      <section className="rounded-xl border border-muted/15 bg-surface p-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Field label="From">
            <input type="date" value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              className="w-full rounded-md border border-muted/30 bg-bg px-2 py-1.5 text-sm" />
          </Field>
          <Field label="To">
            <input type="date" value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              className="w-full rounded-md border border-muted/30 bg-bg px-2 py-1.5 text-sm" />
          </Field>
          <Field label="Provider">
            <select value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full rounded-md border border-muted/30 bg-bg px-2 py-1.5 text-sm">
              <option value="">any</option>
              <option value="anthropic">anthropic</option>
              <option value="openai">openai</option>
              <option value="local_echo">local_echo</option>
            </select>
          </Field>
          <Field label="Purpose">
            <input type="text" value={purpose}
              onChange={(e) => setPurpose(e.target.value)}
              placeholder="e.g. episode_intelligence"
              className="w-full rounded-md border border-muted/30 bg-bg px-2 py-1.5 text-sm" />
          </Field>
          <Field label="Billed to">
            <select value={billedTo}
              onChange={(e) => setBilledTo(e.target.value)}
              className="w-full rounded-md border border-muted/30 bg-bg px-2 py-1.5 text-sm">
              <option value="">any</option>
              <option value="user_byok">user BYOK</option>
              <option value="deployment_default">deployment default</option>
            </select>
          </Field>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" onClick={load} disabled={loading}
            className="rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50">
            {loading ? "Loading…" : "Refresh"}
          </button>
          <button type="button" onClick={downloadCsv} disabled={loading || !data}
            className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5 disabled:opacity-50">
            Download CSV
          </button>
        </div>
        {error && (
          <p className="mt-3 rounded-md border border-caution/30 bg-caution/10 p-2 text-sm text-caution">
            {error}
          </p>
        )}
      </section>

      {/* Aggregate summary */}
      {data && (
        <section className="rounded-xl border border-evidence/30 bg-evidence/5 p-4">
          <p className="text-xs uppercase tracking-widest text-evidence">Totals (this window)</p>
          <div className="mt-2 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Runs" value={fmtNum(data.aggregate.total_runs)} />
            <Stat label="Est. spend" value={fmtUsd(data.aggregate.total_estimated_usd_cost)}
              note={data.aggregate.runs_with_unknown_cost > 0
                ? `${data.aggregate.runs_with_unknown_cost} runs not priced`
                : null} />
            <Stat label="Input tokens" value={fmtNum(data.aggregate.total_input_tokens)} />
            <Stat label="Output tokens" value={fmtNum(data.aggregate.total_output_tokens)} />
            <Stat label="Cache read" value={fmtNum(data.aggregate.total_cache_read_tokens)} />
            <Stat label="Cache create" value={fmtNum(data.aggregate.total_cache_creation_tokens)} />
            <Stat label="Cache hit rate"
              value={(() => {
                const reads = data.aggregate.total_cache_read_tokens;
                const inp = data.aggregate.total_input_tokens;
                const denom = reads + inp;
                return denom > 0 ? `${Math.round(100 * reads / denom)}%` : "—";
              })()}
              note="of all input tokens served from cache" />
          </div>
        </section>
      )}

      {/* By-purpose breakdown */}
      {data && byPurpose.length > 0 && (
        <section>
          <h2 className="font-serif text-lg">By purpose</h2>
          <ul className="mt-2 divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {byPurpose.map((p) => (
              <li key={p.purpose} className="flex items-baseline justify-between px-4 py-2 text-sm">
                <span className="font-mono text-xs">{p.purpose}</span>
                <span className="flex items-baseline gap-4 text-xs text-muted">
                  <span>{p.runs} runs</span>
                  <span className="font-medium text-ink">{fmtUsd(p.cost)}</span>
                  {p.unknownCost > 0 && <span>· {p.unknownCost} unpriced</span>}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Row table */}
      {data && (
        <section>
          <h2 className="font-serif text-lg">Rows</h2>
          {data.rows.length === 0 ? (
            <p className="mt-2 text-sm text-muted">No model runs in this window.</p>
          ) : (
            <div className="mt-2 overflow-x-auto rounded-xl border border-muted/15 bg-surface">
              <table className="w-full min-w-[920px] text-xs">
                <thead className="border-b border-muted/15 text-left text-muted">
                  <tr>
                    <th className="px-3 py-2">When</th>
                    <th className="px-3 py-2">Purpose</th>
                    <th className="px-3 py-2">Model</th>
                    <th className="px-3 py-2">Billed</th>
                    <th className="px-3 py-2 text-right">Input</th>
                    <th className="px-3 py-2 text-right">Output</th>
                    <th className="px-3 py-2 text-right">Cache R/C</th>
                    <th className="px-3 py-2 text-right">Latency</th>
                    <th className="px-3 py-2 text-right">Est. cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-muted/10">
                  {data.rows.map((r) => (
                    <tr key={r.id} className={r.error ? "bg-caution/5" : ""}>
                      <td className="px-3 py-1.5 whitespace-nowrap text-muted">{fmtTs(r.created_at)}</td>
                      <td className="px-3 py-1.5 font-mono">{r.purpose}</td>
                      <td className="px-3 py-1.5 font-mono text-muted">{r.model}</td>
                      <td className="px-3 py-1.5">
                        {r.billed_to === "user_byok" ? (
                          <span className="rounded-md bg-accent/10 px-1.5 py-0.5 text-accent">
                            BYOK{r.billed_credential_label ? ` · ${r.billed_credential_label}` : ""}
                          </span>
                        ) : r.billed_to === "deployment_default" ? (
                          <span className="text-muted">deployment</span>
                        ) : (
                          <span className="text-muted/60">—</span>
                        )}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{fmtNum(r.input_tokens)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{fmtNum(r.output_tokens)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {fmtNum(r.cache_read_input_tokens)}
                        <span className="text-muted/60"> / </span>
                        {fmtNum(r.cache_creation_input_tokens)}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-muted">
                        {r.latency_ms != null ? `${r.latency_ms}ms` : "—"}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums font-medium">
                        {fmtUsd(r.estimated_usd_cost)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="block text-xs uppercase tracking-widest text-muted">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note?: string | null }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-widest text-muted">{label}</p>
      <p className="mt-1 font-serif text-2xl tabular-nums">{value}</p>
      {note && <p className="mt-0.5 text-xs text-muted">{note}</p>}
    </div>
  );
}
