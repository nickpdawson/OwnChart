"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type {
  CandidateRef,
  ConvCitation,
  ConvDetail,
  ConvMessage,
  ProviderShape,
} from "@/lib/api";

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function ThreadClient({
  thread,
  providers,
  candidates,
}: {
  thread: ConvDetail;
  providers: ProviderShape[];
  candidates: CandidateRef[];
}) {
  const router = useRouter();
  const [messages, setMessages] = useState<ConvMessage[]>(thread.messages);
  const [composer, setComposer] = useState("");
  const [provider, setProvider] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [promoting, setPromoting] = useState(false);

  const promotableEpisode = candidates.find(
    (c) => c.candidate_type === "episode" && c.disposition === "pending",
  );
  // Q-A1: the planner stamps anchor match_confidence on the episode
  // candidate. Surface 'low' loudly above the thread so the user
  // knows the answer is built on a best-guess match.
  const episodeCandidate = candidates.find(
    (c) => c.candidate_type === "episode",
  );
  const lowConfidence = episodeCandidate?.match_confidence === "low";

  async function promoteEpisode() {
    if (!promotableEpisode) return;
    setPromoting(true);
    setError(null);
    try {
      const r = await fetch(
        `/api/episodes/from-candidate/${encodeURIComponent(promotableEpisode.id)}`,
        { method: "POST", credentials: "include" },
      );
      if (!r.ok) throw new Error(await r.text());
      const out = (await r.json()) as { id: string };
      router.push(`/episode/${out.id}` as never);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPromoting(false);
    }
  }

  async function send() {
    if (!composer.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`/api/conversations/${thread.id}/messages`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          content: composer,
          provider: provider || undefined,
        }),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      const out = (await r.json()) as ConvDetail;
      setMessages(out.messages);
      setComposer("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-6 space-y-5">
      {lowConfidence && (
        <section className="rounded-xl border-2 border-caution/50 bg-caution/10 p-4">
          <p className="text-xs uppercase tracking-widest text-caution">
            ⚠ Low-confidence anchor
          </p>
          <p className="mt-1 text-sm">
            {episodeCandidate?.match_explanation ??
              "OwnChart wasn't sure which event you meant and fell back to the most recent major procedure. The whole answer is anchored on that guess — verify before trusting the narrative."}
          </p>
        </section>
      )}

      {promotableEpisode && (
        <section className="rounded-xl border border-accent/30 bg-accent/5 p-4">
          <p className="text-xs uppercase tracking-widest text-accent">
            Episode candidate
          </p>
          <p className="mt-1 font-serif text-base text-ink">
            {promotableEpisode.title ?? "Save this as an episode"}
          </p>
          <p className="mt-1 text-sm text-muted">
            Promoting saves a canonical episode with members,
            cross-links it to this conversation, and surfaces it on
            Home and Timeline.
          </p>
          <button
            type="button"
            onClick={promoteEpisode}
            disabled={promoting}
            className="mt-3 rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
          >
            {promoting ? "Saving…" : "Save as Episode"}
          </button>
        </section>
      )}

      <ol className="space-y-5">
        {messages.map((m) => (
          <Bubble key={m.id} msg={m} />
        ))}
      </ol>

      <section className="rounded-xl border border-muted/15 bg-surface p-4">
        <textarea
          value={composer}
          onChange={(e) => setComposer(e.target.value)}
          placeholder="Ask a follow-up…"
          rows={3}
          className="w-full rounded-md border border-muted/30 bg-bg/50 px-3 py-2 text-base"
        />
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <label className="text-xs text-muted">
            Provider
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="ml-2 rounded-md border border-muted/30 bg-surface px-2 py-1 text-sm"
            >
              <option value="">
                {thread.provider ?? "Default"}
              </option>
              {providers.map((p) => (
                <option key={p.key} value={p.key} disabled={!p.configured}>
                  {p.label}
                  {!p.configured && " · not configured"}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={send}
            disabled={busy || !composer.trim()}
            className="ml-auto rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
          >
            {busy ? "Thinking…" : "Send"}
          </button>
        </div>
        {error && <p className="mt-3 text-sm text-caution">{error}</p>}
      </section>
    </div>
  );
}

function Bubble({ msg }: { msg: ConvMessage }) {
  const isUser = msg.role === "user";
  return (
    <li
      className={
        "rounded-xl border p-4 " +
        (isUser
          ? "border-accent/30 bg-accent/5"
          : "border-muted/15 bg-surface")
      }
    >
      <p className="text-[10px] uppercase tracking-widest text-muted">
        {msg.role}
        {msg.provider && ` · ${msg.provider}`}
        {msg.model && ` · ${msg.model}`}
        {msg.privacy_mode && ` · privacy ${msg.privacy_mode}`}
        {" · "}
        {fmtTime(msg.created_at)}
      </p>
      <p className="mt-2 whitespace-pre-wrap font-serif text-base leading-relaxed text-ink">
        {msg.content}
      </p>
      {msg.citations.length > 0 && (
        <div className="mt-3">
          <p className="text-xs uppercase tracking-widest text-muted">
            Evidence ({msg.citations.length})
          </p>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {msg.citations.map((c) => (
              <CitationChip key={c.id} c={c} />
            ))}
          </ul>
        </div>
      )}
      {msg.usage && (
        <p className="mt-2 text-[10px] tabular-nums text-muted">
          {String(msg.usage.input_tokens ?? "?")} in /
          {String(msg.usage.output_tokens ?? "?")} out ·{" "}
          {String(msg.usage.latency_ms ?? "?")}ms
        </p>
      )}
    </li>
  );
}

function CitationChip({ c }: { c: ConvCitation }) {
  let href: string | null = null;
  if (c.citation_type === "fact") href = `?fact=${encodeURIComponent(c.subject_id)}`;
  else if (c.citation_type === "source") href = `/sources/${c.subject_id}`;
  else if (c.citation_type === "episode") href = `/episode/${c.subject_id}`;
  const label = `${c.citation_type}${c.claim_label ? ` · ${c.claim_label}` : ""}`;
  if (href) {
    return (
      <li>
        <a
          href={href}
          className="inline-block rounded-md border border-evidence/30 bg-evidence/5 px-2 py-0.5 text-xs text-evidence hover:bg-evidence/10"
          title={c.excerpt ?? c.note ?? c.subject_id}
        >
          {label}
        </a>
      </li>
    );
  }
  return (
    <li
      className="inline-block rounded-md border border-muted/30 bg-bg/40 px-2 py-0.5 text-xs text-muted"
      title={c.excerpt ?? c.note ?? c.subject_id}
    >
      {label}
    </li>
  );
}
