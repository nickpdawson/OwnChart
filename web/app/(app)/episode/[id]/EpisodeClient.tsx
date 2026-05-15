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

export function EpisodeClient({ episode }: { episode: EpisodeDetail }) {
  const [ep, setEp] = useState(episode);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameInput, setRenameInput] = useState(
    ep.display_title || ep.title || "",
  );
  const [aliasInput, setAliasInput] = useState("");

  const intelligence = (ep.payload?.intelligence ?? null) as
    | Record<string, unknown>
    | null;
  const followUps = (ep.payload?.follow_up_questions ?? []) as string[];

  // Conversations come from the backend's `ep.related_conversations`
  // which already merges (a) explicit episode_member rows and (b)
  // anchor_fact_id matches with a `link_source` discriminator. No
  // separate client-side fetch needed — and the prior duplicate
  // section was visually confusing (Nick caught it 2026-05-15).

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

      {/* 3b. RELATED CONVERSATIONS — chats explicitly attached via
          the chat Save menu (#89), plus legacy EI conversations
          linked via scope.anchor_fact_id. The backend merges the
          two paths and tags each with link_source; we render
          explicit attaches FIRST and visually distinct so Nick's
          "I just saved this here" mental model survives. P1-5
          from 2026-05-15 PM read. */}
      {ep.related_conversations.length > 0 && (
        <Section title={`Conversations about this Event (${ep.related_conversations.length})`}>
          <ul className="space-y-2">
            {[...ep.related_conversations]
              .sort((a, b) => {
                // Explicit attaches (member) above anchor_fact links.
                if (a.link_source !== b.link_source) {
                  return a.link_source === "member" ? -1 : 1;
                }
                // Within group, newest activity first.
                const at = a.last_message_at ? new Date(a.last_message_at).getTime() : 0;
                const bt = b.last_message_at ? new Date(b.last_message_at).getTime() : 0;
                return bt - at;
              })
              .map((c) => {
                const isAttached = c.link_source === "member";
                return (
                  <li key={c.id}>
                    <a
                      href={`/chat/${c.id}`}
                      className={
                        "block rounded-xl border p-4 hover:border-accent/60 " +
                        (isAttached
                          ? "border-accent/40 bg-accent/5"
                          : "border-muted/15 bg-surface")
                      }
                    >
                      <div className="flex flex-wrap items-baseline gap-2">
                        {isAttached && (
                          <span className="rounded-md bg-accent/20 px-1.5 py-0.5 text-[10px] uppercase tracking-widest text-accent">
                            Attached from chat
                          </span>
                        )}
                        <span className="font-serif text-base text-ink">
                          {c.title || "(untitled chat)"}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted">
                        {c.kind.replace(/_/g, " ")}
                        {c.last_message_at && (
                          <>{" · "}{new Date(c.last_message_at).toLocaleString()}</>
                        )}
                        {!isAttached && <> · linked via anchor</>}
                      </p>
                    </a>
                  </li>
                );
              })}
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

      {/* (Conversations now render in section 3b above using
          ep.related_conversations from the backend — explicit
          attaches first, anchor_fact_id matches below.) */}

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

// Patient-readable labels for the "What's connected" rows. The
// backend stores enum values (member_type, role) that read like
// database columns; the UI translates them so a normal user can
// tell what each row is. Left bar color codes the row by type.
const MEMBER_TYPE_LABEL: Record<string, string> = {
  fact: "Fact",
  source: "Source document",
  conversation: "Conversation",
  event: "Related event",
  candidate: "Sensemaking candidate",
};

const MEMBER_ROLE_LABEL: Record<string, string> = {
  primary: "Main event",
  component: "Part of this event",
  context: "Related context",
  followup: "Follow-up",
  recovery_metric: "Recovery signal",
};

const MEMBER_TYPE_BAR: Record<string, string> = {
  fact: "bg-ink",
  source: "bg-accent",
  conversation: "bg-evidence",
  event: "bg-muted",
  candidate: "bg-caution",
};

function MemberRow({ m }: { m: EpisodeMember }) {
  let href: string | null = null;
  if (m.member_type === "fact") href = `?fact=${encodeURIComponent(m.subject_id)}`;
  else if (m.member_type === "source") href = `/sources/${m.subject_id}`;
  else if (m.member_type === "conversation") href = `/chat/${m.subject_id}`;
  const typeLabel = MEMBER_TYPE_LABEL[m.member_type] ?? m.member_type;
  const roleLabel = MEMBER_ROLE_LABEL[m.role] ?? m.role;
  const barColor = MEMBER_TYPE_BAR[m.member_type] ?? "bg-muted";
  const body = (
    <div className="flex items-stretch text-sm">
      <span aria-hidden className={`w-1 shrink-0 rounded-l-md ${barColor}`} />
      <div className="flex flex-1 flex-wrap items-baseline gap-x-2 gap-y-1 px-4 py-3">
        <span className="text-[10px] uppercase tracking-widest text-muted">
          {typeLabel}
        </span>
        <span className="font-medium">{roleLabel}</span>
      </div>
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
