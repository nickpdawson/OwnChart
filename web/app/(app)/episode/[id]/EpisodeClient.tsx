"use client";

import { useEffect, useState, type ReactNode } from "react";
import type { EpisodeDetail, EpisodeMember } from "@/lib/api";

// Same markdown helper as the chat thread. Keeps the asterisks from
// the LLM output rendering as plain text on the Event page too.
function renderInline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
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
      const lines = p.split("\n");
      lines.forEach((line, li) => {
        if (li > 0) out.push(<br key={`${key}-br-${li}`} />);
        if (line) out.push(line);
      });
    }
  });
  return out;
}

function Paragraph({ text }: { text: string }) {
  const trimmed = (text || "").trim();
  if (!trimmed) return null;
  const paragraphs = trimmed.split(/\n\s*\n/);
  return (
    <div className="space-y-3 font-serif text-base leading-relaxed text-ink">
      {paragraphs.map((para, i) => (
        <p key={`p-${i}`}>{renderInline(para, `p${i}`)}</p>
      ))}
    </div>
  );
}

type RelatedConversation = {
  id: string;
  title: string;
  kind: string;
  last_message_at: string | null;
};

export function EpisodeClient({ episode }: { episode: EpisodeDetail }) {
  const [ep, setEp] = useState(episode);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameInput, setRenameInput] = useState(
    ep.display_title || ep.title || "",
  );
  const [aliasInput, setAliasInput] = useState("");
  const [related, setRelated] = useState<RelatedConversation[]>([]);

  const intelligence = (ep.payload?.intelligence ?? null) as
    | Record<string, unknown>
    | null;
  const followUps = (ep.payload?.follow_up_questions ?? []) as string[];

  // Pull conversations whose scope's anchor_fact_id matches this Event's
  // primary_fact_id — those are the threads the user has had ABOUT this
  // Event. Powers the "Conversations" section in Nick's spec.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!ep.primary_fact_id) return;
      try {
        const r = await fetch(
          `/api/conversations?anchor_fact_id=${encodeURIComponent(ep.primary_fact_id)}`,
          { credentials: "include" },
        );
        if (!r.ok) return;
        const list = (await r.json()) as RelatedConversation[];
        if (!cancelled) setRelated(Array.isArray(list) ? list : []);
      } catch {
        /* non-fatal */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ep.primary_fact_id]);

  const plannerAnchor = (
    (ep.payload?.planner as Record<string, unknown> | undefined)?.anchor ??
    null
  ) as Record<string, unknown> | null;
  const matchConfidence =
    typeof plannerAnchor?.match_confidence === "string"
      ? plannerAnchor.match_confidence
      : null;
  const matchExplanation =
    typeof plannerAnchor?.match_explanation === "string"
      ? plannerAnchor.match_explanation
      : null;

  function sectionString(key: string, fallbackField?: string): string {
    if (!intelligence) return "";
    const value = intelligence[key];
    if (typeof value === "string") return value;
    if (value && typeof value === "object") {
      const v = value as { summary?: string; translation?: string };
      return String(v.summary ?? v.translation ?? "");
    }
    if (fallbackField) {
      const f = intelligence[fallbackField];
      if (typeof f === "string") return f;
    }
    return "";
  }

  async function saveRename() {
    setBusy(true);
    setError(null);
    try {
      const aliases = aliasInput
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const body: { display_title?: string; aliases?: string[] } = {};
      const cleaned = renameInput.trim();
      if (cleaned && cleaned !== (ep.display_title || "")) {
        body.display_title = cleaned;
      }
      // Always send aliases when the user opened the rename UI — let
      // them clear by submitting an empty string.
      body.aliases = aliases;
      const r = await fetch(`/api/episodes/${encodeURIComponent(ep.id)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      const next = (await r.json()) as EpisodeDetail;
      setEp(next);
      setRenaming(false);
      setAliasInput("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const shortAnswer = sectionString("short_answer");
  const whatHappened = sectionString("what_happened");
  const whatTheyDid = sectionString("what_they_did", "translation");
  const medsFound = sectionString("meds_found");
  const medsMissing = sectionString("meds_missing");
  const bodyResponse = sectionString("body_response");
  const interpretation = sectionString("interpretation");
  const travelAndLife = sectionString("travel_and_life");
  const evidence = sectionString("evidence_summary");

  return (
    <>
      {/* Low-confidence banner. Only fires when planner explicitly said
          it fell back to most-recent — same-event collapse means this
          should rarely show now, but the safety net stays. */}
      {matchConfidence === "low" && (
        <section className="mt-6 rounded-xl border-2 border-caution/50 bg-caution/10 p-4">
          <p className="text-xs uppercase tracking-widest text-caution">
            ⚠ Low-confidence match
          </p>
          <p className="mt-1 text-sm">
            {matchExplanation ??
              "OwnChart fell back to the most recent major procedure. Verify before trusting the narrative."}
          </p>
        </section>
      )}

      {/* Rename + alias editor — the single most important UX surface
          on the Event page per Nick's directive. Renaming
          "STRABISMUS SCARRING EO MUSC..." to "2026 left eye surgery"
          should be a one-click affordance. */}
      <section className="mt-6 rounded-xl border border-muted/15 bg-bg/40 p-4">
        {!renaming ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs uppercase tracking-widest text-muted">
              Rename this Event · add aliases
            </p>
            <button
              type="button"
              onClick={() => setRenaming(true)}
              className="rounded-md border border-accent/40 px-2.5 py-1 text-xs text-accent hover:bg-accent/10"
            >
              Rename
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            <label className="block text-xs uppercase tracking-widest text-muted">
              Display title
              <input
                value={renameInput}
                onChange={(e) => setRenameInput(e.target.value)}
                placeholder="e.g. 2026 left eye surgery"
                className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2 text-base"
              />
            </label>
            <label className="block text-xs uppercase tracking-widest text-muted">
              Aliases (comma-separated)
              <input
                value={aliasInput}
                onChange={(e) => setAliasInput(e.target.value)}
                placeholder="left eye, Stanford eye surgery, May 1 eye surgery"
                className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2 text-base"
              />
              <span className="mt-1 block text-[11px] text-muted">
                You can refer to this Event by any of these names in chat.
              </span>
            </label>
            {error && (
              <p className="text-xs text-caution">Couldn&apos;t save: {error}</p>
            )}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={saveRename}
                disabled={busy}
                className="rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
              >
                {busy ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setRenaming(false);
                  setError(null);
                }}
                className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:bg-muted/5"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </section>

      {/* 1. WHAT HAPPENED — the headline section. Leads with the
          short_answer if we have it (the EI prompt's direct
          lead-with-it summary), then the structured what_happened
          detail. */}
      <Section title="What happened">
        {shortAnswer && (
          <div className="mb-3 rounded-lg border border-accent/30 bg-accent/5 p-3">
            <Paragraph text={shortAnswer} />
          </div>
        )}
        <Paragraph text={whatHappened} />
        <Paragraph text={whatTheyDid} />
      </Section>

      {/* 2. WHY IT MATTERS — the interpretation section, what the
          recovery / cross-fact comparison means. */}
      {interpretation && (
        <Section title="Why it matters">
          <Paragraph text={interpretation} />
        </Section>
      )}

      {/* 3. WHAT'S CONNECTED — the explicit member list grouped by
          type (procedure / encounter / source / etc). */}
      {ep.members.length > 0 && (
        <Section title={`What's connected (${ep.members.length})`}>
          <ul className="divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {ep.members.map((m) => (
              <MemberRow key={m.id} m={m} />
            ))}
          </ul>
        </Section>
      )}

      {/* 4. RECOVERY / BODY SIGNAL — wearable summary + body response
          interpretation. */}
      {(bodyResponse || medsFound || medsMissing) && (
        <Section title="Recovery & body signal">
          {bodyResponse && <Paragraph text={bodyResponse} />}
          {medsFound && (
            <div className="mt-3">
              <h3 className="text-xs uppercase tracking-widest text-muted">
                Meds found
              </h3>
              <div className="mt-1"><Paragraph text={medsFound} /></div>
            </div>
          )}
          {medsMissing && (
            <div className="mt-3">
              <h3 className="text-xs uppercase tracking-widest text-muted">
                Meds missing
              </h3>
              <div className="mt-1"><Paragraph text={medsMissing} /></div>
            </div>
          )}
          {travelAndLife && (
            <div className="mt-3">
              <h3 className="text-xs uppercase tracking-widest text-muted">
                Travel & life context
              </h3>
              <div className="mt-1"><Paragraph text={travelAndLife} /></div>
            </div>
          )}
        </Section>
      )}

      {/* 5. OPEN QUESTIONS — the follow-up_questions array as
          chat-ready link cards. */}
      {followUps.length > 0 && (
        <Section title="Open questions">
          <ul className="grid gap-2 sm:grid-cols-2">
            {followUps.map((q, i) => (
              <li key={i}>
                <a
                  href={`/ask?q=${encodeURIComponent(q)}`}
                  className="block rounded-xl border border-muted/15 bg-surface p-3 text-sm hover:border-accent/40"
                >
                  {q}
                </a>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* 6. CONVERSATIONS — chat threads the user has had about this
          Event. Powered by anchor_fact_id matching. */}
      {related.length > 0 && (
        <Section title={`Conversations about this Event (${related.length})`}>
          <ul className="divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {related.map((c) => (
              <li key={c.id}>
                <a
                  href={`/chat/${c.id}`}
                  className="block px-4 py-3 text-sm hover:bg-muted/5"
                >
                  <p className="font-medium text-ink">{c.title}</p>
                  <p className="text-xs text-muted">
                    {c.kind}
                    {c.last_message_at && (
                      <> · {new Date(c.last_message_at).toLocaleDateString()}</>
                    )}
                  </p>
                </a>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* 7. EVIDENCE — what the answer leans on. */}
      {evidence && (
        <Section title="Evidence">
          <Paragraph text={evidence} />
        </Section>
      )}
    </>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="mt-8">
      <h2 className="font-serif text-xl">{title}</h2>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function MemberRow({ m }: { m: EpisodeMember }) {
  let href: string | null = null;
  if (m.member_type === "fact") href = `?fact=${encodeURIComponent(m.subject_id)}`;
  else if (m.member_type === "source") href = `/sources/${m.subject_id}`;
  else if (m.member_type === "conversation") href = `/chat/${m.subject_id}`;
  const body = (
    <div className="flex flex-wrap items-baseline gap-2 px-4 py-3 text-sm">
      <span className="text-[10px] uppercase tracking-widest text-muted">
        {m.member_type}
      </span>
      <span className="font-medium">{m.role}</span>
      <span className="ml-auto font-mono text-xs text-muted">
        {m.subject_id.slice(0, 8)}…
      </span>
    </div>
  );
  return (
    <li>
      {href ? (
        <a href={href} className="block hover:bg-muted/5">
          {body}
        </a>
      ) : (
        body
      )}
    </li>
  );
}
