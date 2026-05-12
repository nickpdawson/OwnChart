"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
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

  // Save-as-Dossier modal state
  type Suggestion = {
    refuse: boolean;
    refuse_reason: string | null;
    name: string | null;
    aliases: string[];
    description: string | null;
  };
  const [dossierOpen, setDossierOpen] = useState(false);
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [suggLoading, setSuggLoading] = useState(false);
  const [savingDossier, setSavingDossier] = useState(false);
  const [dossierName, setDossierName] = useState("");
  const [dossierAliases, setDossierAliases] = useState("");
  const [dossierDescription, setDossierDescription] = useState("");
  const [conflictTopic, setConflictTopic] = useState<{slug: string} | null>(null);

  const hasAssistantReply = messages.some((m) => m.role === "assistant" && m.content);

  // Deep-link from /ask: ?save=dossier auto-opens the modal so the
  // "Save as Dossier" button on the Ask answer panel lands here ready
  // to act. Only fires once on mount; user can close + reopen normally.
  const searchParams = useSearchParams();
  useEffect(() => {
    if (searchParams?.get("save") === "dossier" && hasAssistantReply) {
      openSaveAsDossier();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  async function openSaveAsDossier() {
    setDossierOpen(true);
    setError(null);
    setConflictTopic(null);
    if (suggestion) return; // already loaded
    setSuggLoading(true);
    try {
      const r = await fetch(
        `/api/conversations/${encodeURIComponent(thread.id)}/suggest-topic`,
        { method: "POST", credentials: "include" },
      );
      if (!r.ok) throw new Error(await r.text());
      const s = (await r.json()) as Suggestion;
      setSuggestion(s);
      if (!s.refuse) {
        setDossierName(s.name ?? "");
        setDossierAliases((s.aliases ?? []).join(", "));
        setDossierDescription(s.description ?? "");
      }
    } catch (e) {
      setError(`Suggest failed: ${(e as Error).message}`);
    } finally {
      setSuggLoading(false);
    }
  }

  async function saveAsDossier() {
    if (!dossierName.trim()) {
      setError("Topic name is required.");
      return;
    }
    setSavingDossier(true);
    setError(null);
    setConflictTopic(null);
    try {
      const aliases = dossierAliases
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const r = await fetch(
        `/api/conversations/${encodeURIComponent(thread.id)}/save-as-topic`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          credentials: "include",
          body: JSON.stringify({
            name: dossierName.trim(),
            aliases,
            description: dossierDescription.trim() || null,
          }),
        },
      );
      if (!r.ok) throw new Error(await r.text());
      const out = (await r.json()) as { topic_id: string; slug: string; conflict: boolean };
      if (out.conflict) {
        setConflictTopic({ slug: out.slug });
        // Don't redirect automatically — let the user confirm they want
        // to attach this chat to the pre-existing dossier.
        return;
      }
      router.push(`/dossier/${out.slug}` as never);
    } catch (e) {
      setError(`Save failed: ${(e as Error).message}`);
    } finally {
      setSavingDossier(false);
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

      {hasAssistantReply && !dossierOpen && (
        <section className="rounded-xl border border-muted/15 bg-surface p-4">
          <p className="text-xs uppercase tracking-widest text-muted">
            Long-running concern?
          </p>
          <p className="mt-1 text-sm">
            Save this conversation as a Dossier — a topic that
            accumulates related facts over time. You can keep chatting
            inside the dossier and any new ingestion that matches the
            topic will land there automatically.
          </p>
          <button
            type="button"
            onClick={openSaveAsDossier}
            className="mt-3 rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/5"
          >
            Save as Dossier
          </button>
        </section>
      )}

      {dossierOpen && (
        <section className="rounded-xl border border-accent/30 bg-accent/5 p-4">
          <div className="flex items-baseline justify-between">
            <p className="text-xs uppercase tracking-widest text-accent">
              Save as Dossier
            </p>
            <button
              type="button"
              onClick={() => setDossierOpen(false)}
              className="text-xs text-muted underline-offset-4 hover:underline"
            >
              Cancel
            </button>
          </div>
          {suggLoading && (
            <p className="mt-2 text-sm text-muted">Asking the model for a topic suggestion…</p>
          )}
          {suggestion?.refuse && (
            <p className="mt-2 text-sm text-caution">
              The model didn&apos;t think this chat warrants a new dossier:
              <em className="ml-1">{suggestion.refuse_reason}</em>. You can still
              save it manually below.
            </p>
          )}
          {!suggLoading && (
            <div className="mt-3 grid gap-3">
              <label className="text-sm">
                Name
                <input
                  type="text"
                  value={dossierName}
                  onChange={(e) => setDossierName(e.target.value)}
                  placeholder="e.g. Right ankle fracture"
                  className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
                />
              </label>
              <label className="text-sm">
                Aliases <span className="text-xs text-muted">(comma-separated; substrings that match related facts)</span>
                <input
                  type="text"
                  value={dossierAliases}
                  onChange={(e) => setDossierAliases(e.target.value)}
                  placeholder="ankle, fibula, malleolus, fracture"
                  className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
                />
              </label>
              <label className="text-sm">
                Description <span className="text-xs text-muted">(optional, &lt; 280 chars)</span>
                <textarea
                  value={dossierDescription}
                  onChange={(e) => setDossierDescription(e.target.value)}
                  rows={2}
                  className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
                />
              </label>
              {conflictTopic && (
                <p className="text-sm text-caution">
                  A dossier with that name already exists. The conversation has
                  been attached to it —{" "}
                  <a
                    href={`/dossier/${conflictTopic.slug}`}
                    className="underline-offset-4 hover:underline"
                  >
                    open {conflictTopic.slug}
                  </a>
                  . Rename above and save again to create a separate dossier
                  instead.
                </p>
              )}
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={saveAsDossier}
                  disabled={savingDossier || !dossierName.trim()}
                  className="rounded-md bg-accent px-3 py-1.5 text-sm text-surface disabled:opacity-50"
                >
                  {savingDossier ? "Saving…" : "Create dossier"}
                </button>
              </div>
            </div>
          )}
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
