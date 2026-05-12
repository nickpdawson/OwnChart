"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

// /ask is now a thin entry point: type a question, get redirected to
// /chat/<id> where the answer + Save-as-Dossier panel render. Until
// 2026-05-13 this page rendered its own structured answer (Well
// supported / Uncertain / Suggested next steps) via POST /api/ask;
// that ran a different prompt (ask_query) than the conversation path
// (general_ask) and persisted a Conversation as a side effect, so
// there were two surfaces showing the same thing. Collapsed in favor
// of one rendering surface (the chat thread) per docs/10's
// "everything is a Conversation" doctrine.
//
// POST /api/ask is still alive for external callers — only the web
// flow changed.

export function AskClient() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Home's "Questions to ask" chips deep-link with ?q=… — pre-fill
  // the input on first mount when present. Default empty so the
  // public-release input never leaks a personal default.
  const initialQ = searchParams?.get("q") ?? "";
  const [question, setQuestion] = useState(initialQ);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const q = searchParams?.get("q");
    if (q) setQuestion(q);
  }, [searchParams]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch("/api/conversations", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          kind: "ask",
          // title left blank — the prompt's `short_title` field
          // overrides Conversation.title on the first assistant turn.
          scope: { type: "whole_record" },
          first_message: q,
        }),
      });
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
      const conv = (await r.json()) as { id: string };
      router.push(`/chat/${conv.id}` as never);
    } catch (e) {
      setError((e as Error).message);
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
            placeholder="Tell me about my sleep this winter…"
            className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
          />
        </label>
        <p className="text-xs text-muted">
          Your question opens a saved chat thread. From there you can
          continue the conversation, save it as a Dossier, or promote
          a specific event to an Episode.
        </p>
        {error && <p className="text-sm text-caution">{error}</p>}
        <button
          type="submit"
          disabled={busy || !question.trim()}
          className="justify-self-start rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50"
        >
          {busy ? "Asking…" : "Ask"}
        </button>
      </form>
    </div>
  );
}
