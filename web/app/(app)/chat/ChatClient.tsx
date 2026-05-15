"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type {
  ConvSummary,
  ProviderShape,
  SuggestedQuestion,
} from "@/lib/api";

type Props = {
  initialConversations: ConvSummary[];
  providers: ProviderShape[];
  suggestedQuestions: SuggestedQuestion[];
};

const SCOPE_LABEL: Record<string, string> = {
  whole_record: "Whole record",
  period: "Time period",
  source: "One source",
  topic: "Dossier",
  episode: "Event",
  fact: "Single fact",
};

function fmtWhen(iso: string | null): string {
  if (!iso) return "—";
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

export function ChatClient({
  initialConversations,
  providers,
  suggestedQuestions,
}: Props) {
  const router = useRouter();
  const [conversations] = useState<ConvSummary[]>(initialConversations);
  const [search, setSearch] = useState("");
  const [composer, setComposer] = useState("");
  const [provider, setProvider] = useState<string>("");
  const [scopeJson, setScopeJson] = useState<string>(
    JSON.stringify({ type: "whole_record" }),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const filtered = !search.trim()
    ? conversations
    : conversations.filter((c) =>
        (c.title ?? "").toLowerCase().includes(search.toLowerCase()),
      );

  async function startConversation(
    message: string,
    scopeOverride?: unknown,
    scopeHint?: string,
  ) {
    setBusy(true);
    setError(null);
    try {
      let scope: { type?: string; anchor_fact_id?: string } & Record<string, unknown> = {};
      if (scopeOverride && typeof scopeOverride === "object") {
        scope = scopeOverride as typeof scope;
      } else {
        try {
          scope = JSON.parse(scopeJson);
        } catch {
          scope = { type: "whole_record" };
        }
      }

      // Episode-shaped chats route through /api/episodes/intelligence
      // — the planner does the work, the LLM emits the 7-section
      // structured answer, and the response carries a conversation_id
      // we can navigate to. Generic chats go through /api/conversations.
      const useEpisodeIntelligence =
        scopeHint === "episode" &&
        typeof scope.anchor_fact_id === "string";

      if (useEpisodeIntelligence) {
        const r = await fetch(`/api/episodes/intelligence`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          credentials: "include",
          body: JSON.stringify({
            fact_id: scope.anchor_fact_id,
            question: message,
          }),
        });
        if (!r.ok) {
          const detail = await r.text();
          throw new Error(detail || `HTTP ${r.status}`);
        }
        const out = (await r.json()) as {
          conversation_id: string | null;
          status: string;
          error: string | null;
        };
        if (out.conversation_id) {
          router.push(`/chat/${out.conversation_id}` as never);
        } else {
          throw new Error(
            out.error ?? `Episode intelligence ${out.status}; no conversation created.`,
          );
        }
        return;
      }

      const r = await fetch(`/api/conversations`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          kind: "ask",
          scope,
          first_message: message,
        }),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      const out = (await r.json()) as { id: string };
      router.push(`/chat/${out.id}` as never);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function patchSetting(key: string, value: unknown) {
    try {
      await fetch(`/api/settings/user`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ key, value }),
      });
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="mt-7 grid gap-6 lg:grid-cols-[18rem_1fr]">
      {/* Sidebar — saved threads + search */}
      <aside className="space-y-4">
        <input
          type="search"
          placeholder="Search conversations…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full rounded-md border border-muted/30 bg-surface px-3 py-2 text-sm"
        />
        <ul className="space-y-1.5">
          {filtered.length === 0 && (
            <li className="text-xs text-muted">No conversations yet.</li>
          )}
          {filtered.map((c) => (
            <li key={c.id}>
              <a
                href={`/chat/${c.id}`}
                className="block rounded-md border border-muted/15 bg-surface px-3 py-2 text-sm hover:border-accent/40"
              >
                <p className="line-clamp-2 font-medium">
                  {c.title ?? "(untitled thread)"}
                </p>
                <p className="mt-0.5 text-xs text-muted">
                  {SCOPE_LABEL[String(c.scope?.type ?? "whole_record")] ??
                    String(c.scope?.type ?? "whole_record")}
                  {" · "}
                  {c.provider ?? "—"}
                  {" · "}
                  {fmtWhen(c.last_message_at ?? c.created_at)}
                </p>
              </a>
            </li>
          ))}
        </ul>
      </aside>

      {/* Composer + suggested */}
      <div>
        <section className="rounded-xl border border-muted/15 bg-surface p-5">
          <p className="text-xs uppercase tracking-widest text-muted">
            New conversation
          </p>
          <textarea
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            placeholder="What do you want to understand about your health?"
            rows={3}
            className="mt-2 w-full rounded-md border border-muted/30 bg-bg/50 px-3 py-2 text-base"
          />
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="text-xs text-muted">
              Provider
              <select
                value={provider}
                onChange={(e) => {
                  setProvider(e.target.value);
                  if (e.target.value) patchSetting("ai.default_provider", e.target.value);
                }}
                className="ml-2 rounded-md border border-muted/30 bg-surface px-2 py-1 text-sm"
              >
                <option value="">Default (settings)</option>
                {providers.map((p) => (
                  <option
                    key={p.key}
                    value={p.key}
                    disabled={!p.configured}
                  >
                    {p.label}
                    {!p.configured && " · not configured"}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-muted">
              Scope
              <select
                value={String(JSON.parse(scopeJson)?.type ?? "whole_record")}
                onChange={(e) => {
                  const t = e.target.value;
                  if (t === "period") {
                    const to = new Date();
                    const from = new Date(to.getTime() - 30 * 86400_000);
                    setScopeJson(
                      JSON.stringify({
                        type: "period",
                        period: {
                          from: from.toISOString(),
                          to: to.toISOString(),
                        },
                      }),
                    );
                  } else {
                    setScopeJson(JSON.stringify({ type: t }));
                  }
                }}
                className="ml-2 rounded-md border border-muted/30 bg-surface px-2 py-1 text-sm"
              >
                <option value="whole_record">Whole record</option>
                <option value="period">Last 30 days</option>
              </select>
            </label>
            <button
              type="button"
              onClick={() => startConversation(composer)}
              disabled={busy || !composer.trim()}
              className="ml-auto rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
            >
              {busy ? "Asking…" : "Ask"}
            </button>
          </div>
          {error && (
            <p className="mt-3 text-sm text-caution">{error}</p>
          )}
        </section>

        {suggestedQuestions.length > 0 && (
          <section className="mt-6">
            <p className="text-xs uppercase tracking-widest text-muted">
              Suggested questions
            </p>
            <ul className="mt-2 grid gap-2 sm:grid-cols-2">
              {suggestedQuestions.map((sq, i) => (
                <li key={i}>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      startConversation(
                        sq.submitted_text,
                        sq.scope ?? undefined,
                        sq.scope_hint,
                      )
                    }
                    className="block w-full rounded-xl border border-muted/15 bg-surface p-4 text-left hover:border-accent/40 disabled:opacity-50"
                  >
                    <p className="font-medium">{sq.visible_text}</p>
                    <p className="mt-1 text-[10px] uppercase tracking-widest text-muted">
                      {SCOPE_LABEL[sq.scope_hint] ?? sq.scope_hint}
                    </p>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
