import { UsageClient } from "./UsageClient";

// (app) layout fetches /api/auth/me at request time → page must be dynamic.
export const dynamic = "force-dynamic";

export default function UsagePage() {
  return (
    <div className="max-w-5xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        Settings · AI providers · Usage
      </p>
      <h1 className="mt-2 font-serif text-3xl">Where the model spend goes</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Every LLM call OwnChart makes lands in <code>model_runs</code> with
        tokens, cache hits/misses, latency, and which key was billed.
        This page rolls those rows up with public per-token pricing
        so you can see who paid for what — and export CSV for an
        outside spreadsheet.
      </p>
      <UsageClient />
    </div>
  );
}
