"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

type Citation = { fact_id: string; note?: string | null };
type AskResponse = {
  question: string;
  answer: string | null;
  well_supported: string[];
  uncertain: { statement: string; why_uncertain?: string }[];
  suggested_next_steps: string[];
  citations: Citation[];
  retrieved_fact_count: number;
  model_run_id: string | null;
  safety_response: string | null;
  error: string | null;
};

export function AskClient() {
  // Home's "Questions to ask" chips deep-link with ?q=… — pre-fill
  // the input on first mount when present. Falls back to a generic
  // placeholder when ?q= is missing so the default Ask page still
  // has a sensible starting prompt.
  const searchParams = useSearchParams();
  const initialQ = searchParams?.get("q") ?? "Tell me the story of my knee pain.";
  const [question, setQuestion] = useState(initialQ);
  const [answer, setAnswer] = useState<AskResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // If the user lands here from a chip while the page is already
  // mounted (client-side nav), update the question to match the new
  // ?q= value. Stops the question from sticking on whatever the
  // user typed last.
  useEffect(() => {
    const q = searchParams?.get("q");
    if (q) setQuestion(q);
  }, [searchParams]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      const r = await fetch("/api/ask", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ question }),
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
        setError(`Ask failed (HTTP ${r.status})${detail}`);
        return;
      }
      const out = (await r.json()) as AskResponse;
      setAnswer(out);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-6 space-y-6">
      <form onSubmit={submit} className="grid gap-3 rounded-xl border border-muted/15 bg-surface p-4">
        <label className="text-sm">
          Question
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            rows={3}
            className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
          />
        </label>
        {error && <p className="text-sm text-caution">{error}</p>}
        <button type="submit" disabled={busy} className="justify-self-start rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50">
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>

      {answer?.safety_response && (
        <p className="rounded-md bg-caution/10 p-3 text-sm">{answer.safety_response}</p>
      )}

      {answer && !answer.safety_response && (
        <article className="space-y-5 rounded-xl border border-muted/15 bg-surface p-5">
          {answer.error && (
            <p className="rounded-md bg-caution/10 p-3 text-sm text-caution">
              LLM error — {answer.error}
            </p>
          )}
          <p className="text-xs text-muted">
            Retrieved {answer.retrieved_fact_count} relevant fact{answer.retrieved_fact_count === 1 ? "" : "s"}.
          </p>
          {answer.answer && (
            <p className="whitespace-pre-line text-base leading-relaxed">{answer.answer}</p>
          )}
          {answer.well_supported.length > 0 && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-widest text-muted">Well supported</p>
              <ul className="mt-2 space-y-1 text-sm">
                {answer.well_supported.map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            </div>
          )}
          {answer.uncertain.length > 0 && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-widest text-muted">Uncertain</p>
              <ul className="mt-2 space-y-1 text-sm">
                {answer.uncertain.map((u, i) => (
                  <li key={i}>
                    {u.statement}
                    {u.why_uncertain && <span className="ml-1 text-muted">— {u.why_uncertain}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {answer.suggested_next_steps.length > 0 && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-widest text-muted">Suggested next steps</p>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
                {answer.suggested_next_steps.map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            </div>
          )}
          {answer.citations.length > 0 && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-widest text-muted">
                Citations ({answer.citations.length})
              </p>
              <ul className="mt-2 space-y-1 text-sm">
                {answer.citations.map((c) => (
                  <li key={c.fact_id} className="text-muted">
                    fact {c.fact_id.slice(0, 8)}…{c.note ? ` — ${c.note}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="text-xs text-muted">
            Audit: model_run_id {answer.model_run_id?.slice(0, 8) || "—"}…
          </p>
        </article>
      )}
    </div>
  );
}
