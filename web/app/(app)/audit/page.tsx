import { getMe, listModelRuns, type ModelRunReadout } from "@/lib/api";

export const dynamic = "force-dynamic";

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default async function AuditPage() {
  // P0 fix (2026-05-23): the audit API gates on
  // `user.is_instance_admin` and returns 403 for any other role.
  // Pre-fix, the page called listModelRuns() unconditionally and any
  // resulting throw bubbled to a Next.js error boundary (digest
  // 865374892). Now we check the user's admin flag first and render
  // a graceful "instance admin only" panel for non-admins. The
  // sidebar nav item is also hidden for non-admins (see (app)/layout.tsx).
  const me = await getMe();
  const isAdmin = me?.is_instance_admin === true;

  let runs: ModelRunReadout[] = [];
  let listError: string | null = null;
  if (isAdmin) {
    try {
      runs = await listModelRuns();
    } catch (e) {
      listError = (e as Error).message;
    }
  }

  return (
    <div className="max-w-5xl">
      <p className="text-sm uppercase tracking-widest text-muted">Audit log</p>
      <h1 className="mt-2 font-serif text-3xl">LLM calls</h1>

      {!isAdmin && (
        <div className="mt-6 rounded-md border border-muted/15 bg-surface p-4 text-sm">
          <p className="font-medium text-ink">Instance admin only</p>
          <p className="mt-2 text-muted">
            The audit log shows every LLM call made by this instance,
            including calls initiated by other accounts. To protect
            cross-user privacy it&rsquo;s restricted to the instance
            admin. Ask the person who hosts this OwnChart if you need
            to see something specific.
          </p>
        </div>
      )}

      {isAdmin && (
        <>
          <p className="mt-3 max-w-2xl text-muted">
            Every byte sent to Anthropic shows up here, regardless of
            whether it succeeded or failed. Prompt text and response text
            are intentionally <em>not</em> stored &mdash; only their
            SHA-256 hashes, the prompt version (matches a file in{" "}
            <code>api/ownchart/prompts/</code>), and the consent state
            at the time of the call. PHI lives in the source documents
            and the extracted-fact rows the call produced.
          </p>
          <p className="mt-1 text-xs text-muted">
            {runs.length} run{runs.length === 1 ? "" : "s"}
          </p>

          {listError && (
            <p className="mt-4 rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
              Couldn&rsquo;t load audit rows: {listError}
            </p>
          )}

          {!listError && runs.length === 0 ? (
            <p className="mt-6 text-muted">No LLM calls made yet.</p>
          ) : (
            !listError && (
              <div className="mt-6 overflow-hidden rounded-xl border border-muted/15 bg-surface">
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] text-left text-sm">
                    <thead className="bg-bg/60 text-xs uppercase tracking-widest text-muted">
                      <tr>
                        <th className="px-4 py-2">When</th>
                        <th className="px-4 py-2">Purpose</th>
                        <th className="px-4 py-2">Model</th>
                        <th className="px-4 py-2">Prompt</th>
                        <th className="px-4 py-2">Tokens</th>
                        <th className="px-4 py-2">Latency</th>
                        <th className="px-4 py-2">Consent</th>
                        <th className="px-4 py-2">Error</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runs.map((r) => {
                        const usage = r.usage as {
                          input_tokens?: number;
                          output_tokens?: number;
                          latency_ms?: number;
                        } | null;
                        return (
                          <tr
                            key={r.id}
                            className="border-t border-muted/10"
                          >
                            <td className="whitespace-nowrap px-4 py-2 text-xs text-muted">
                              {fmtDate(r.created_at)}
                            </td>
                            <td className="px-4 py-2">{r.purpose}</td>
                            <td className="px-4 py-2 text-xs text-muted">
                              {r.model}
                            </td>
                            <td className="px-4 py-2 text-xs">
                              {r.prompt_version}
                            </td>
                            <td className="px-4 py-2 text-xs text-muted">
                              {usage?.input_tokens != null &&
                              usage?.output_tokens != null
                                ? `${usage.input_tokens} → ${usage.output_tokens}`
                                : "—"}
                            </td>
                            <td className="px-4 py-2 text-xs text-muted">
                              {usage?.latency_ms != null
                                ? `${usage.latency_ms} ms`
                                : "—"}
                            </td>
                            <td className="px-4 py-2 text-xs">
                              {r.consent_state ? (
                                <span className="text-accent">on</span>
                              ) : (
                                <span className="text-caution">off</span>
                              )}
                            </td>
                            <td className="px-4 py-2 text-xs text-caution">
                              {r.error || ""}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )
          )}
        </>
      )}
    </div>
  );
}
