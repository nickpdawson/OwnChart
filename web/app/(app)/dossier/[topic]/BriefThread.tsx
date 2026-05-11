"use client";

import { useEffect, useRef, useState } from "react";

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: { fact_id: string; note?: string }[];
  retrieved_fact_count: number | null;
  model_run_id: string | null;
  safety_response: string | null;
  error: string | null;
  created_at: string;
};

export function BriefThread({
  slug,
  suggestedQuestions,
}: {
  slug: string;
  suggestedQuestions: string[];
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const tailRef = useRef<HTMLLIElement | null>(null);

  // Hydrate the existing thread on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/topics/${slug}/thread`, {
          credentials: "include",
          cache: "no-store",
        });
        if (!cancelled && r.ok) {
          const out = (await r.json()) as Message[];
          setMessages(out);
        }
      } catch {
        /* ignore */
      } finally {
        if (!cancelled) setHydrated(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [slug]);

  // Scroll the newest turn into view as the conversation grows.
  useEffect(() => {
    tailRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);

  async function send(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    // Optimistic local user turn so the textbox feels responsive.
    const optimisticId = `opt-${Date.now()}`;
    setMessages((prev) => [
      ...prev,
      {
        id: optimisticId,
        role: "user",
        content: q,
        citations: [],
        retrieved_fact_count: null,
        model_run_id: null,
        safety_response: null,
        error: null,
        created_at: new Date().toISOString(),
      },
    ]);
    setInput("");
    try {
      const r = await fetch(`/api/topics/${slug}/ask`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ question: q }),
      });
      if (r.status === 412) {
        setError("Enable global LLM consent first to continue thinking with your dossier.");
        // Roll back the optimistic turn.
        setMessages((prev) => prev.filter((m) => m.id !== optimisticId));
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
        setError(`Couldn't send (HTTP ${r.status})${detail}`);
        setMessages((prev) => prev.filter((m) => m.id !== optimisticId));
        return;
      }
      const pair = (await r.json()) as Message[];
      // Replace the optimistic turn with the persisted user message
      // and append the assistant reply.
      setMessages((prev) => [...prev.filter((m) => m.id !== optimisticId), ...pair]);
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    send(input);
  }

  return (
    <section className="mt-6 rounded-xl border border-muted/15 bg-surface p-5">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="font-serif text-lg">Continue thinking with this dossier</h2>
        {messages.length > 0 && (
          <p className="text-xs text-muted">
            {messages.length} message{messages.length === 1 ? "" : "s"}
          </p>
        )}
      </div>

      {/* Suggested questions — shown when the thread is empty so the
          user has a friction-free way to start. */}
      {hydrated && messages.length === 0 && suggestedQuestions.length > 0 && (
        <div className="mt-3">
          <p className="text-xs uppercase tracking-widest text-muted">Try asking</p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {suggestedQuestions.map((q, i) => (
              <li key={i}>
                <button
                  type="button"
                  onClick={() => send(q)}
                  disabled={busy}
                  className="rounded-full border border-muted/30 bg-bg px-3 py-1.5 text-xs hover:border-accent/50 hover:bg-surface disabled:opacity-50"
                >
                  {q}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Empty-state copy when there are no suggested questions either. */}
      {hydrated && messages.length === 0 && suggestedQuestions.length === 0 && (
        <p className="mt-3 text-sm text-muted">
          Ask a follow-up about anything in the brief — the system will pull additional facts from
          this dossier and reply with citations.
        </p>
      )}

      {/* Conversation */}
      {messages.length > 0 && (
        <ol className="mt-4 space-y-4">
          {messages.map((m) => (
            <li
              key={m.id}
              className={
                m.role === "user"
                  ? "rounded-lg bg-bg/60 p-3"
                  : "rounded-lg border border-muted/15 p-3"
              }
            >
              <p className="text-xs uppercase tracking-widest text-muted">
                {m.role === "user" ? "You" : "Research partner"}
                {m.retrieved_fact_count !== null && m.role === "assistant" && (
                  <span className="ml-2 normal-case tracking-normal">
                    · retrieved {m.retrieved_fact_count} fact{m.retrieved_fact_count === 1 ? "" : "s"}
                  </span>
                )}
              </p>
              {m.safety_response ? (
                <p className="mt-2 whitespace-pre-line rounded-md bg-caution/10 p-3 text-sm">
                  {m.safety_response}
                </p>
              ) : m.error ? (
                <p className="mt-2 text-sm text-caution">LLM error — {m.error}</p>
              ) : (
                <p className="mt-2 whitespace-pre-line text-sm leading-relaxed">{m.content}</p>
              )}
              {m.role === "assistant" && m.citations.length > 0 && (
                <p className="mt-2 text-xs text-muted">
                  Citations:{" "}
                  {m.citations.map((c, i) => (
                    <a
                      key={c.fact_id + i}
                      href={`#fact-${c.fact_id}`}
                      title={c.note || "Jump to this fact on the dossier"}
                      className="mr-2 inline-block rounded border border-muted/20 bg-bg/60 px-1.5 py-0.5 hover:border-accent/40 hover:text-ink"
                    >
                      <code>{c.fact_id.slice(0, 8)}…</code>
                      {c.note ? <span className="ml-1">{c.note.slice(0, 60)}{c.note.length > 60 ? "…" : ""}</span> : null}
                    </a>
                  ))}
                </p>
              )}
            </li>
          ))}
          <li ref={tailRef} />
        </ol>
      )}

      {error && <p className="mt-3 text-sm text-caution">{error}</p>}

      {/* Input */}
      {hydrated && (
        <form onSubmit={onSubmit} className="mt-4 flex flex-wrap items-end gap-2">
          <label className="flex-1 text-sm">
            <span className="sr-only">Your question</span>
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                messages.length === 0
                  ? "Ask a follow-up about this dossier…"
                  : "Continue the conversation…"
              }
              disabled={busy}
              className="block w-full rounded-md border border-muted/30 bg-bg px-3 py-2 disabled:opacity-50"
            />
          </label>
          <button
            type="submit"
            disabled={busy || !input.trim()}
            className="rounded-lg bg-accent px-4 py-2 text-sm text-surface disabled:opacity-50"
          >
            {busy ? "Thinking…" : "Send"}
          </button>
        </form>
      )}
    </section>
  );
}
