"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type {
  CandidateRef,
  ConvCitation,
  ConvDetail,
  ConvMessage,
  ProviderShape,
} from "@/lib/api";
import { SaveMenu } from "./SaveMenu";

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

// Minimal safe markdown renderer for assistant messages.
//
// The LLM (Episode Intelligence in particular) emits markdown with
// `**Section headers**`, occasional `_italic_`, and `\n\n` paragraph
// breaks. Previously we rendered `msg.content` inside a single <p>
// with `whitespace-pre-wrap`, so asterisks showed up as literal
// characters — Nick called this out 2026-05-13 PM as "still
// unformatted MD." Pulling in react-markdown would require a deps
// install and rebuild risk; this inline renderer handles the
// patterns the LLM actually produces with zero new dependencies.
//
// What it does:
//   - splits on blank lines into paragraphs
//   - within each paragraph, wraps `**bold**` in <strong>, `_italic_`
//     in <em>, and `` `code` `` in <code>
//   - preserves single newlines inside a paragraph as <br/>
//   - any literal `**`/`_` the user typed still renders the same
//     because the regex requires a closing pair
//
// What it deliberately doesn't do:
//   - links, lists, headings via `#`, tables — none of these appear
//     in current EI/Ask output. Add when the LLM starts producing
//     them. Until then YAGNI.
//
// Safety: all input is converted via React text nodes (never
// dangerouslySetInnerHTML), so untrusted content can't inject HTML.
// Citation-chip helper. The EI LLM emits citations inline as
// `[fact:UUID]`, `[source:UUID]`, `[fact:UUID — Note Kind]`, and
// sometimes shorthand `[fact:b928e750]` with a UUID prefix. Nick
// flagged 2026-05-14 PM that these were rendering as raw text —
// the user couldn't open them. This regex catches all three
// shapes; the chip's href routes to a fact-context sheet or
// source-detail page. UUID-prefix shorthand opens the same sheet
// because the existing sidesheet accepts partial prefixes.
const CITATION_PATTERN =
  /\[(fact|source):([0-9a-fA-F]{6,36})(?:\s*[—-]\s*([^\]]+))?\]/g;

function renderInline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  // First split on citation chips, then run the markdown pattern
  // on each non-chip segment. Two-pass keeps regex simple.
  let lastIdx = 0;
  let citationIdx = 0;
  for (const m of text.matchAll(CITATION_PATTERN)) {
    const mi = m.index ?? 0;
    if (mi > lastIdx) {
      out.push(
        ...renderMarkdownSegment(text.slice(lastIdx, mi), `${keyBase}-md${citationIdx}`),
      );
    }
    const kind = m[1];
    const id = m[2];
    const label = m[3]?.trim();
    const href = kind === "source"
      ? `/sources/${id}`
      : `/discover?fact=${encodeURIComponent(id)}`;
    out.push(
      <a
        key={`${keyBase}-cite${citationIdx}`}
        href={href}
        className="mx-0.5 inline-flex items-center rounded-md border border-accent/30 bg-accent/5 px-1.5 py-0.5 text-[0.85em] font-mono text-accent hover:bg-accent/10"
        title={`${kind}: ${id}${label ? ` — ${label}` : ""}`}
      >
        {label || `${kind} ${id.slice(0, 8)}`}
      </a>,
    );
    citationIdx += 1;
    lastIdx = mi + m[0].length;
  }
  if (lastIdx < text.length) {
    out.push(
      ...renderMarkdownSegment(text.slice(lastIdx), `${keyBase}-md${citationIdx}`),
    );
  }
  return out;
}

function renderMarkdownSegment(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  // Token order matters: **bold** before *italic* so the inner
  // match doesn't eat the bold markers.
  const pattern = /(\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|`[^`\n]+`)/g;
  const parts = text.split(pattern);
  parts.forEach((p, i) => {
    if (!p) return;
    const key = `${keyBase}-${i}`;
    if ((p.startsWith("**") && p.endsWith("**")) || (p.startsWith("__") && p.endsWith("__"))) {
      out.push(<strong key={key}>{p.slice(2, -2)}</strong>);
    } else if ((p.startsWith("*") && p.endsWith("*") && p.length > 2)
              || (p.startsWith("_") && p.endsWith("_") && p.length > 2)) {
      out.push(<em key={key}>{p.slice(1, -1)}</em>);
    } else if (p.startsWith("`") && p.endsWith("`") && p.length > 2) {
      out.push(
        <code key={key} className="rounded bg-muted/10 px-1 py-0.5 text-[0.92em]">
          {p.slice(1, -1)}
        </code>,
      );
    } else {
      // Preserve single newlines within a paragraph as <br/>.
      const lines = p.split("\n");
      lines.forEach((line, li) => {
        if (li > 0) out.push(<br key={`${key}-br-${li}`} />);
        if (line) out.push(line);
      });
    }
  });
  return out;
}

function MessageBody({ content }: { content: string }) {
  const trimmed = (content || "").trim();
  if (!trimmed) return null;
  // Split on blank lines (paragraph boundaries). The LLM emits
  // `\n\n` between sections; this also tolerates `\n  \n` etc.
  const paragraphs = trimmed.split(/\n\s*\n/);
  return (
    <div className="mt-2 space-y-3 font-serif text-base leading-relaxed text-ink">
      {paragraphs.map((para, i) => (
        <p key={`p-${i}`}>{renderInline(para, `p${i}`)}</p>
      ))}
    </div>
  );
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
  // Stuck-EI detector: if the spinner has been up for >90s without an
  // assistant reply and the scope hasn't flipped to "failed", surface
  // a recoverable message. Most common cause: Anthropic credits / key
  // exhausted, so the background task errored before stamping
  // status="failed". Prevents the infinite spinner Nick hit on
  // 2026-05-15 while testing #89.
  const [stuckPendingEi, setStuckPendingEi] = useState(false);

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

  // EI conversations are created with kind=episode_intelligence and
  // scope.status='running' while the background planner+LLM run. The
  // assistant message lands ~60s later. Frontend polls until it does
  // OR until scope.status flips to 'failed' (background hit an error
  // and surfaced a fallback assistant message).
  const isPendingEi =
    thread.kind === "episode_intelligence" &&
    !hasAssistantReply &&
    (thread.scope?.status === "running" || !thread.scope?.status);

  // 90s after the thread loads, if it's still pending, mark stuck.
  useEffect(() => {
    if (!isPendingEi) {
      setStuckPendingEi(false);
      return;
    }
    const t = setTimeout(() => setStuckPendingEi(true), 90_000);
    return () => clearTimeout(t);
  }, [isPendingEi, thread.id]);
  useEffect(() => {
    if (!isPendingEi) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const r = await fetch(
          `/api/conversations/${encodeURIComponent(thread.id)}`,
          { credentials: "include" },
        );
        if (!r.ok) return;
        const fresh = (await r.json()) as ConvDetail;
        if (cancelled) return;
        setMessages(fresh.messages);
        const done = fresh.messages.some((m) => m.role === "assistant" && m.content);
        const failed = fresh.scope?.status === "failed";
        if (done || failed) return; // stop the loop
        setTimeout(tick, 2500);
      } catch {
        if (!cancelled) setTimeout(tick, 5000);
      }
    };
    const t = setTimeout(tick, 2000);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPendingEi, thread.id]);

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
            Event candidate
          </p>
          <p className="mt-1 font-serif text-base text-ink">
            {promotableEpisode.title ?? "Save this as an Event"}
          </p>
          <p className="mt-1 text-sm text-muted">
            Saving keeps this Event in your timeline with all the
            evidence it pulled in — and you can rename it (e.g.
            &ldquo;2026 left eye&rdquo;) and refer to it by that
            name in chat from now on.
          </p>
          <button
            type="button"
            onClick={promoteEpisode}
            disabled={promoting}
            className="mt-3 rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
          >
            {promoting ? "Saving…" : "Save as Event"}
          </button>
        </section>
      )}

      {/* Unified save/attach menu (#89). Replaces the inline "Save as
          Dossier" prompt — the SaveMenu covers all four paths:
          new Event, attach Event, new Dossier (via /save-as-topic),
          attach Dossier. No LLM. The EI-candidate "Save as Event"
          banner above still renders when an EI candidate is pending.
          Intentionally NOT gated on hasAssistantReply: the save path
          is LLM-free, so a chat that's stuck waiting on an answer
          (LLM credits exhausted, EI background task crashed, etc.)
          still needs to be savable. The thread itself existing is
          enough — we're attaching a *conversation*, not an answer. */}
      {!dossierOpen && (
        <SaveMenu conversationId={thread.id} />
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
        {isPendingEi && !stuckPendingEi && <ThinkingBubble />}
        {isPendingEi && stuckPendingEi && (
          <li className="rounded-xl border border-caution/30 bg-caution/5 p-4">
            <p className="text-[10px] uppercase tracking-widest text-caution">
              assistant · looks stuck
            </p>
            <p className="mt-2 text-sm text-ink">
              This Episode Intelligence run has been pending for over 90
              seconds. Most likely the LLM call failed (no Anthropic
              credits, missing API key, or background task crashed)
              and never wrote back the failure status.
            </p>
            <p className="mt-1 text-xs text-muted">
              You can still save / attach this chat below — the save
              path doesn&apos;t depend on the LLM. Or check{" "}
              <a href="/audit" className="underline-offset-4 hover:underline">
                /audit
              </a>{" "}
              for the matching model run.
            </p>
          </li>
        )}
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

// ThinkingBubble — staged status while EI / Ask is pending.
//
// The actual backend phases (planner → LLM call → tool emit → audit
// write) don't expose intermediate progress, so this isn't a literal
// progress bar. The stages are illustrative — they advance on a timer
// so the user knows the system is doing something instead of staring
// at a static ellipsis. P1-4 from Nick's 2026-05-15 PM smoke read.
const _THINKING_STAGES = [
  { at: 0,    headline: "Finding the right event",     hint: "Matching your question to the most likely anchor on your record." },
  { at: 8000, headline: "Checking primary records",    hint: "Preferring operative reports and specialist notes over copy-forward history." },
  { at: 22000, headline: "Reading supporting evidence", hint: "Pulling anesthesia notes, instructions, and the wearable windows around the date." },
  { at: 42000, headline: "Writing cited answer",       hint: "Drafting the response with fact_ids inline so you can verify." },
];

function ThinkingBubble() {
  const [stageIdx, setStageIdx] = useState(0);
  const [startedAt] = useState(() => Date.now());
  useEffect(() => {
    const interval = setInterval(() => {
      const elapsed = Date.now() - startedAt;
      let next = 0;
      for (let i = _THINKING_STAGES.length - 1; i >= 0; i--) {
        if (elapsed >= _THINKING_STAGES[i].at) { next = i; break; }
      }
      setStageIdx(next);
    }, 1000);
    return () => clearInterval(interval);
  }, [startedAt]);
  const stage = _THINKING_STAGES[stageIdx];
  return (
    <li className="rounded-xl border border-muted/15 bg-surface p-4">
      <p className="text-[10px] uppercase tracking-widest text-muted">
        assistant · {stage.headline}
        <span className="ml-1.5 inline-flex gap-0.5 align-middle">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="inline-block h-1 w-1 rounded-full bg-accent animate-pulse"
              style={{ animationDelay: `${i * 200}ms` }}
            />
          ))}
        </span>
      </p>
      <ol className="mt-2 space-y-1 text-sm">
        {_THINKING_STAGES.map((s, i) => {
          const isDone = i < stageIdx;
          const isActive = i === stageIdx;
          return (
            <li key={s.headline} className={
              "flex items-baseline gap-2 " +
              (isActive ? "text-ink" : isDone ? "text-muted line-through" : "text-muted/50")
            }>
              <span aria-hidden className={
                "inline-block h-1.5 w-1.5 shrink-0 rounded-full " +
                (isActive ? "bg-accent" : isDone ? "bg-evidence" : "bg-muted/30")
              } />
              <span>{s.headline}</span>
            </li>
          );
        })}
      </ol>
      <p className="mt-2 text-xs text-muted">{stage.hint}</p>
    </li>
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
      <MessageBody content={msg.content} />
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
