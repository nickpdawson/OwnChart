"use client";

// Chat → Event / Dossier save menu (#89).
//
// Four manual paths, no LLM dependency:
//   1. Save as new Event   → POST /api/episodes/from-conversation/{id}
//   2. Attach to existing Event → POST /api/episodes/{id}/attach-conversation
//   3. Save as new Dossier → POST /api/conversations/{id}/save-as-topic
//   4. Attach to existing Dossier → POST /api/topics/{slug}/attach-conversation
//
// The pickers (#2, #4) hit /api/episodes?q=... and /api/topics?q=... so the
// user can search by title, alias, date, and kind (Events only).

import { useEffect, useMemo, useState } from "react";
import type { EpisodeSummary, TopicSummary } from "@/lib/api";

type SavedTarget =
  | { kind: "event"; id: string; title: string }
  | { kind: "dossier"; slug: string; name: string };

type Props = {
  conversationId: string;
  // When set, we already saved into this target on a prior interaction
  // in the same session — show the "Saved to…" banner instead of the
  // full menu. Page reload re-fetches from the server (the conversation
  // scope reflects topic attaches; episode_members reflects event
  // attaches) so this state is just a session optimization.
  initialSavedTarget?: SavedTarget | null;
};

const EVENT_KINDS = ["", "surgery", "diagnosis", "medication", "procedure", "other"] as const;

export function SaveMenu({ conversationId, initialSavedTarget }: Props) {
  const [open, setOpen] = useState(false);
  const [pane, setPane] = useState<"menu" | "new_event" | "attach_event" | "new_dossier" | "attach_dossier">("menu");
  const [saved, setSaved] = useState<SavedTarget | null>(initialSavedTarget ?? null);
  const [error, setError] = useState<string | null>(null);

  function close() {
    setOpen(false);
    setPane("menu");
    setError(null);
  }

  if (saved && !open) {
    return <SavedBanner target={saved} onMoreActions={() => { setSaved(null); setOpen(true); }} />;
  }

  if (!open) {
    return (
      <div className="mt-2">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/5"
        >
          Save / attach this chat →
        </button>
      </div>
    );
  }

  return (
    <section className="rounded-xl border border-accent/30 bg-accent/5 p-4">
      <div className="flex items-baseline justify-between">
        <p className="text-xs uppercase tracking-widest text-accent">
          {pane === "menu" && "Save or attach this chat"}
          {pane === "new_event" && "Save as new Event"}
          {pane === "attach_event" && "Attach to existing Event"}
          {pane === "new_dossier" && "Save as new Dossier"}
          {pane === "attach_dossier" && "Attach to existing Dossier"}
        </p>
        <button
          type="button"
          onClick={pane === "menu" ? close : () => { setPane("menu"); setError(null); }}
          className="text-xs text-muted underline-offset-4 hover:underline"
        >
          {pane === "menu" ? "Cancel" : "← Back"}
        </button>
      </div>

      {error && (
        <p className="mt-3 rounded-md border border-caution/30 bg-caution/10 p-2 text-sm text-caution">
          {error}
        </p>
      )}

      {pane === "menu" && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <PaneButton
            label="Save as new Event"
            blurb="A dated thing that happened — surgery, diagnosis, ER trip."
            onClick={() => setPane("new_event")}
          />
          <PaneButton
            label="Attach to existing Event"
            blurb="Add this chat to an Event you already saved."
            onClick={() => setPane("attach_event")}
          />
          <PaneButton
            label="Save as new Dossier"
            blurb="A topic that accumulates over time — a body system, a chronic concern."
            onClick={() => setPane("new_dossier")}
          />
          <PaneButton
            label="Attach to existing Dossier"
            blurb="Add this chat to a Dossier you already saved."
            onClick={() => setPane("attach_dossier")}
          />
        </div>
      )}

      {pane === "new_event" && (
        <NewEventPane
          conversationId={conversationId}
          onError={setError}
          onSaved={(t) => { setSaved(t); close(); }}
        />
      )}
      {pane === "attach_event" && (
        <AttachEventPane
          conversationId={conversationId}
          onError={setError}
          onSaved={(t) => { setSaved(t); close(); }}
        />
      )}
      {pane === "new_dossier" && (
        <NewDossierPane
          conversationId={conversationId}
          onError={setError}
          onSaved={(t) => { setSaved(t); close(); }}
        />
      )}
      {pane === "attach_dossier" && (
        <AttachDossierPane
          conversationId={conversationId}
          onError={setError}
          onSaved={(t) => { setSaved(t); close(); }}
        />
      )}
    </section>
  );
}

function PaneButton({ label, blurb, onClick }: { label: string; blurb: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border border-muted/30 bg-surface p-3 text-left hover:border-accent/40"
    >
      <p className="font-medium text-sm">{label}</p>
      <p className="mt-1 text-xs text-muted">{blurb}</p>
    </button>
  );
}

function SavedBanner({ target, onMoreActions }: { target: SavedTarget; onMoreActions: () => void }) {
  const href = target.kind === "event"
    ? `/episode/${target.id}`
    : `/dossier/${target.slug}`;
  const label = target.kind === "event" ? "Event" : "Dossier";
  const name = target.kind === "event" ? target.title : target.name;
  return (
    <section className="mt-2 rounded-xl border border-evidence/40 bg-evidence/5 p-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
        <span className="text-xs uppercase tracking-widest text-evidence">Saved</span>
        <span>
          Attached to {label}{" "}
          <a href={href} className="font-medium text-evidence underline-offset-4 hover:underline">
            {name} →
          </a>
        </span>
        <button
          type="button"
          onClick={onMoreActions}
          className="ml-auto text-xs text-muted underline-offset-4 hover:underline"
        >
          Save / attach again
        </button>
      </div>
    </section>
  );
}

// --- New Event pane ---------------------------------------------------------

// Refined P0-1 (Nick 2026-05-15 PM): event creation must NEVER
// silently treat the conversation date as the event date. The form
// asks the user to be explicit about date confidence:
//
//   "known"     → a hard date pulled from a source (or typed)
//   "approx"    → e.g. "around June 2017" — user knows it's fuzzy
//   "before"    → "before Jun 8, 2017" — bounded by a later source
//   "unknown"   → undated historical event; no date stored
//
// The stored `date_start` reflects the user's choice. UI surfaces
// the date-confidence label on the Event detail page so future
// readers know whether to trust the date.
type DateConfidence = "known" | "approx" | "before" | "unknown";

function NewEventPane({
  conversationId,
  onError,
  onSaved,
}: {
  conversationId: string;
  onError: (e: string | null) => void;
  onSaved: (t: SavedTarget) => void;
}) {
  const [title, setTitle] = useState("");
  const [displayTitle, setDisplayTitle] = useState("");
  const [aliases, setAliases] = useState("");
  const [summary, setSummary] = useState("");
  const [kind, setKind] = useState<typeof EVENT_KINDS[number]>("");
  const [dateConfidence, setDateConfidence] = useState<DateConfidence>("known");
  const [date, setDate] = useState("");
  const [saving, setSaving] = useState(false);

  const showDateInput = dateConfidence !== "unknown";
  const dateLabel =
    dateConfidence === "known"   ? "When did this happen?" :
    dateConfidence === "approx"  ? "Approximate date (best guess)" :
    dateConfidence === "before"  ? "This happened before…" :
    "";

  async function submit() {
    if (!title.trim()) {
      onError("Title is required.");
      return;
    }
    setSaving(true);
    onError(null);
    try {
      // Encode date confidence into the title / summary so the Event
      // detail page reads correctly even before backend supports an
      // explicit date_origin column. Backend treats date_start as
      // optional; an "unknown" event gets date_start=null.
      let finalSummary = summary.trim();
      let finalDate: string | null = date || null;
      if (dateConfidence === "approx" && date) {
        finalSummary = `Approximate date. ${finalSummary}`.trim();
      } else if (dateConfidence === "before" && date) {
        finalSummary = `Event happened before ${date}. Exact date unknown. ${finalSummary}`.trim();
        // Store the upper bound as date_start so timeline anchoring
        // works; the summary flags the "before" semantics.
      } else if (dateConfidence === "unknown") {
        finalSummary = `Undated historical event. ${finalSummary}`.trim();
        finalDate = null;
      }

      const body = {
        title: title.trim(),
        display_title: displayTitle.trim() || null,
        aliases: aliases.split(",").map((s) => s.trim()).filter(Boolean),
        summary: finalSummary || null,
        kind: kind || null,
        date_start: finalDate,
      };
      const r = await fetch(`/api/episodes/from-conversation/${encodeURIComponent(conversationId)}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      const out = (await r.json()) as { episode_id: string };
      onSaved({ kind: "event", id: out.episode_id, title: title.trim() });
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-3 grid gap-3">
      <p className="text-xs text-muted">
        <strong className="text-ink">Events are things that happened</strong> —
        a surgery, a marathon, a diagnosis, an injury, a trip. They are
        bounded in time. Dossiers are long-running stories.
      </p>

      <Field label="Title (required)">
        <input
          type="text" value={title} onChange={(e) => setTitle(e.target.value)}
          placeholder="e.g. 2026 left eye surgery"
          className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
      </Field>
      <Field label="Display title (optional, friendly name)">
        <input
          type="text" value={displayTitle} onChange={(e) => setDisplayTitle(e.target.value)}
          placeholder="2026 left eye"
          className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
      </Field>
      <Field label="Aliases (comma-separated)">
        <input
          type="text" value={aliases} onChange={(e) => setAliases(e.target.value)}
          placeholder="left eye, May 2026 surgery, vitrectomy"
          className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Kind">
          <select
            value={kind} onChange={(e) => setKind(e.target.value as typeof EVENT_KINDS[number])}
            className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
          >
            {EVENT_KINDS.map((k) => (
              <option key={k} value={k}>{k || "— (other)"}</option>
            ))}
          </select>
        </Field>
        <Field label="Date confidence">
          <select
            value={dateConfidence}
            onChange={(e) => setDateConfidence(e.target.value as DateConfidence)}
            className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
          >
            <option value="known">Known — I have the actual date</option>
            <option value="approx">Approximate — roughly when</option>
            <option value="before">Before a later event (bounded)</option>
            <option value="unknown">Unknown — undated historical event</option>
          </select>
        </Field>
      </div>
      {showDateInput && (
        <Field label={dateLabel}>
          <input
            type="date" value={date} onChange={(e) => setDate(e.target.value)}
            className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
          />
          <p className="mt-1 text-xs text-muted">
            {dateConfidence === "known" && "The actual event date — not today, not the date you saved this chat."}
            {dateConfidence === "approx" && "Best guess. The Event will note that the date is approximate."}
            {dateConfidence === "before" && "We'll store this as the upper bound — the actual event happened before this date."}
          </p>
        </Field>
      )}
      {!showDateInput && (
        <p className="rounded-md border border-muted/15 bg-muted/5 p-2 text-xs text-muted">
          This Event will be saved as <strong>undated</strong>. Good
          for things you know happened but can't pin a date on (an
          older surgery, an injury years ago). You can add a date later.
        </p>
      )}
      <Field label="Summary (optional)">
        <textarea
          value={summary} onChange={(e) => setSummary(e.target.value)} rows={2}
          className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
      </Field>
      <div className="flex gap-2">
        <button
          type="button" onClick={submit} disabled={saving || !title.trim()}
          className="rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
        >
          {saving ? "Saving…" : "Create Event"}
        </button>
      </div>
    </div>
  );
}

// --- Attach to existing Event pane ----------------------------------------

function AttachEventPane({
  conversationId,
  onError,
  onSaved,
}: {
  conversationId: string;
  onError: (e: string | null) => void;
  onSaved: (t: SavedTarget) => void;
}) {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<typeof EVENT_KINDS[number]>("");
  const [results, setResults] = useState<EpisodeSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [attaching, setAttaching] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const params = new URLSearchParams();
        if (q.trim()) params.set("q", q.trim());
        if (kind) params.set("kind", kind);
        params.set("limit", "20");
        const r = await fetch(`/api/episodes?${params}`, { credentials: "include" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const out = (await r.json()) as EpisodeSummary[];
        if (!cancelled) setResults(out);
      } catch (e) {
        if (!cancelled) onError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 200);
    return () => { cancelled = true; clearTimeout(t); };
  }, [q, kind, onError]);

  async function attach(ep: EpisodeSummary) {
    setAttaching(ep.id);
    onError(null);
    try {
      const r = await fetch(`/api/episodes/${encodeURIComponent(ep.id)}/attach-conversation`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ conversation_id: conversationId }),
      });
      if (!r.ok) throw new Error(await r.text());
      onSaved({ kind: "event", id: ep.id, title: ep.display_title || ep.title });
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setAttaching(null);
    }
  }

  return (
    <div className="mt-3 space-y-3">
      <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
        <input
          type="text" value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="Search by title, alias, or kind…"
          className="rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
        <select
          value={kind} onChange={(e) => setKind(e.target.value as typeof EVENT_KINDS[number])}
          className="rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        >
          {EVENT_KINDS.map((k) => (
            <option key={k} value={k}>{k || "any kind"}</option>
          ))}
        </select>
      </div>
      {loading && <p className="text-xs text-muted">Searching…</p>}
      {!loading && results.length === 0 && (
        <EmptyEventPickerHelp query={q} />
      )}
      <ul className="max-h-72 space-y-1 overflow-y-auto">
        {results.map((ep) => (
          <li key={ep.id}>
            <button
              type="button"
              onClick={() => attach(ep)}
              disabled={attaching !== null}
              className="flex w-full flex-wrap items-baseline gap-x-2 rounded-md border border-muted/15 bg-surface px-3 py-2 text-left text-sm hover:border-accent/40 disabled:opacity-50"
            >
              <span className="font-medium">{ep.display_title || ep.title}</span>
              <span className="text-xs text-muted">{ep.kind}</span>
              {ep.date_start && (
                <span className="ml-auto font-mono text-xs tabular-nums text-muted">
                  {new Date(ep.date_start).toLocaleDateString()}
                </span>
              )}
              {attaching === ep.id && <span className="ml-2 text-xs text-accent">Attaching…</span>}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// When the Event picker is empty, don't dead-end. Offer the three
// next moves Nick called out (2026-05-15 PM): create new Event,
// add to a related Dossier, search again. We also fetch candidate
// Dossiers matching the same query so the user can jump straight
// to one. Doctrine: Events are things that happened; Dossiers are
// long-running stories.
function EmptyEventPickerHelp({ query }: { query: string }) {
  const [candidates, setCandidates] = useState<TopicSummary[]>([]);
  useEffect(() => {
    let cancelled = false;
    const trimmed = query.trim();
    if (!trimmed) {
      setCandidates([]);
      return;
    }
    (async () => {
      try {
        const params = new URLSearchParams();
        params.set("q", trimmed);
        params.set("limit", "5");
        const r = await fetch(`/api/topics?${params}`, { credentials: "include" });
        if (!r.ok) return;
        const out = (await r.json()) as TopicSummary[];
        if (!cancelled) setCandidates(out);
      } catch {/* non-fatal */}
    })();
    return () => { cancelled = true; };
  }, [query]);

  return (
    <div className="space-y-2 rounded-md border border-muted/15 bg-muted/5 p-3 text-sm">
      <p className="text-muted">
        No Events match. <strong className="text-ink">Events are things that happened</strong> —
        a surgery, a marathon, a diagnosis. <strong className="text-ink">Dossiers</strong> are
        long-running stories (Right Knee, Hearing, Sleep). Pick the one that fits.
      </p>
      {candidates.length > 0 && (
        <div>
          <p className="text-xs uppercase tracking-widest text-muted">
            Dossiers that match &ldquo;{query}&rdquo;
          </p>
          <ul className="mt-1 space-y-1">
            {candidates.map((t) => (
              <li key={t.slug}>
                <a
                  href={`/dossier/${t.slug}`}
                  className="inline-flex items-center gap-2 text-sm text-accent underline-offset-4 hover:underline"
                >
                  Open dossier: {t.name} →
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// --- New Dossier pane (no LLM) --------------------------------------------

function NewDossierPane({
  conversationId,
  onError,
  onSaved,
}: {
  conversationId: string;
  onError: (e: string | null) => void;
  onSaved: (t: SavedTarget) => void;
}) {
  const [name, setName] = useState("");
  const [aliases, setAliases] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit() {
    if (!name.trim()) {
      onError("Dossier name is required.");
      return;
    }
    setSaving(true);
    onError(null);
    try {
      const body = {
        name: name.trim(),
        aliases: aliases.split(",").map((s) => s.trim()).filter(Boolean),
        description: description.trim() || null,
      };
      const r = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/save-as-topic`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      const out = (await r.json()) as { slug: string; conflict: boolean };
      onSaved({ kind: "dossier", slug: out.slug, name: name.trim() });
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-3 grid gap-3">
      <Field label="Name (required)">
        <input
          type="text" value={name} onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Right ankle"
          className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
      </Field>
      <Field label="Aliases (comma-separated; substrings used to match facts)">
        <input
          type="text" value={aliases} onChange={(e) => setAliases(e.target.value)}
          placeholder="ankle, fibula, malleolus, fracture"
          className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
      </Field>
      <Field label="Description (optional)">
        <textarea
          value={description} onChange={(e) => setDescription(e.target.value)} rows={2}
          className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
      </Field>
      <div className="flex gap-2">
        <button
          type="button" onClick={submit} disabled={saving || !name.trim()}
          className="rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
        >
          {saving ? "Saving…" : "Create Dossier"}
        </button>
      </div>
    </div>
  );
}

// --- Attach to existing Dossier pane ---------------------------------------

function AttachDossierPane({
  conversationId,
  onError,
  onSaved,
}: {
  conversationId: string;
  onError: (e: string | null) => void;
  onSaved: (t: SavedTarget) => void;
}) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<TopicSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [attaching, setAttaching] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const params = new URLSearchParams();
        if (q.trim()) params.set("q", q.trim());
        params.set("limit", "50");
        const r = await fetch(`/api/topics?${params}`, { credentials: "include" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const out = (await r.json()) as TopicSummary[];
        if (!cancelled) setResults(out);
      } catch (e) {
        if (!cancelled) onError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 200);
    return () => { cancelled = true; clearTimeout(t); };
  }, [q, onError]);

  async function attach(t: TopicSummary) {
    setAttaching(t.slug);
    onError(null);
    try {
      const r = await fetch(`/api/topics/${encodeURIComponent(t.slug)}/attach-conversation`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ conversation_id: conversationId }),
      });
      if (!r.ok) throw new Error(await r.text());
      onSaved({ kind: "dossier", slug: t.slug, name: t.name });
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setAttaching(null);
    }
  }

  // Surface aliases inline so users with many dossiers of the same
  // body system can disambiguate ("Right ankle 2024" vs "Right ankle 2026").
  const rows = useMemo(() => results.slice(0, 30), [results]);

  return (
    <div className="mt-3 space-y-3">
      <input
        type="text" value={q} onChange={(e) => setQ(e.target.value)}
        placeholder="Search by name, alias, or slug…"
        className="w-full rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
      />
      {loading && <p className="text-xs text-muted">Searching…</p>}
      {!loading && rows.length === 0 && (
        <p className="text-xs text-muted">
          No Dossiers match. Create a new Dossier instead.
        </p>
      )}
      <ul className="max-h-72 space-y-1 overflow-y-auto">
        {rows.map((t) => (
          <li key={t.slug}>
            <button
              type="button"
              onClick={() => attach(t)}
              disabled={attaching !== null}
              className="flex w-full flex-wrap items-baseline gap-x-2 rounded-md border border-muted/15 bg-surface px-3 py-2 text-left text-sm hover:border-accent/40 disabled:opacity-50"
            >
              <span className="font-medium">{t.name}</span>
              {t.aliases.length > 0 && (
                <span className="text-xs text-muted">
                  ({t.aliases.slice(0, 3).join(", ")}{t.aliases.length > 3 ? "…" : ""})
                </span>
              )}
              {attaching === t.slug && <span className="ml-auto text-xs text-accent">Attaching…</span>}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="block text-xs uppercase tracking-widest text-muted">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}
