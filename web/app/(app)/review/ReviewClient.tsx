"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import type { BulkAssertionType, FactDetail } from "@/lib/api";

// `bulkUpdateFacts` is defined in `@/lib/api` for the type contract,
// but importing the value would pull `next/headers` (server-only) into
// this client component. Re-implement the fetch inline — it's the same
// pattern the per-fact PATCH already uses below.
async function bulkUpdateFactsClient(
  factIds: string[],
  assertionType: BulkAssertionType,
  newReviewState?: string,
  reason?: string,
): Promise<void> {
  const r = await fetch("/api/facts/bulk", {
    method: "POST",
    headers: { "content-type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      fact_ids: factIds,
      assertion_type: assertionType,
      new_review_state: newReviewState ?? null,
      reason: reason ?? null,
    }),
  });
  if (!r.ok) throw new Error(`bulk update failed: HTTP ${r.status}`);
}

// Lane split per docs/01 + docs/02 + docs/04: high-impact clinical
// uncertainty surfaces first; provider/contact metadata sits below in
// its own lane with bulk actions because it's typically low-value
// noise from vision extraction (fax cover sheets, "Dr. Smith MD",
// "Records custodian", etc.) that the user shouldn't have to
// individually disposition.
const PROVIDER_FACT_TYPES = new Set(["provider_relationship"]);

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

function humanizeFactType(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function ReviewClient({ initial }: { initial: FactDetail[] }) {
  const [items, setItems] = useState(initial);
  const router = useRouter();

  const { clinical, providers } = useMemo(() => {
    const c: FactDetail[] = [];
    const p: FactDetail[] = [];
    for (const f of items) {
      if (PROVIDER_FACT_TYPES.has(f.fact_type)) p.push(f);
      else c.push(f);
    }
    // Clinical: most-recent first.
    c.sort((a, b) => {
      const da = a.date_start ? Date.parse(a.date_start) : 0;
      const db = b.date_start ? Date.parse(b.date_start) : 0;
      return db - da;
    });
    return { clinical: c, providers: p };
  }, [items]);

  function removeIds(ids: Set<string>) {
    setItems((prev) => prev.filter((x) => !ids.has(x.id)));
  }

  async function applyToOne(
    fact: FactDetail,
    assertionType: BulkAssertionType,
    newReviewState?: string,
  ): Promise<void> {
    const r = await fetch(`/api/facts/${fact.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      credentials: "include",
      body: JSON.stringify({
        assertion_type: assertionType,
        new_review_state: newReviewState ?? null,
      }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    removeIds(new Set([fact.id]));
    router.refresh();
  }

  async function applyToMany(
    factIds: string[],
    assertionType: BulkAssertionType,
    newReviewState?: string,
  ): Promise<void> {
    if (factIds.length === 0) return;
    await bulkUpdateFactsClient(factIds, assertionType, newReviewState);
    removeIds(new Set(factIds));
    router.refresh();
  }

  if (items.length === 0) {
    return (
      <p className="mt-6 text-muted">
        Nothing to review. New facts will land here as you ingest sources.
      </p>
    );
  }

  return (
    <div className="mt-6 space-y-10">
      {/* --- Clinical lane ---------------------------------------------- */}
      <section>
        <h2 className="font-serif text-xl">Clinical uncertainty</h2>
        <p className="mt-1 text-sm text-muted">
          Conditions, procedures, medications, encounters, and other facts
          that affect timelines and dossiers. Triage one at a time, or use
          the multi-select toolbar to clear a batch.
        </p>
        <p className="mt-1 text-xs text-muted">
          {clinical.length} item{clinical.length === 1 ? "" : "s"}.
        </p>
        {clinical.length === 0 ? (
          <p className="mt-3 text-sm text-muted">
            No clinical items pending review.
          </p>
        ) : (
          <ClinicalLane
            clinical={clinical}
            applyToOne={applyToOne}
            applyToMany={applyToMany}
            onRemove={removeIds}
          />
        )}
      </section>

      {/* --- Providers / orgs lane -------------------------------------- */}
      <section>
        <h2 className="font-serif text-xl">People & organizations</h2>
        <p className="mt-1 text-sm text-muted">
          Provider names, fax recipients, scheduling clerks, records
          custodians — grouped by source. Confirm the real care-team
          contacts, defer the noise (cover-sheet clerks, illegible
          signatures), reject the wrong extractions. Use the per-source
          group actions for trusted sources where every row is genuine.
        </p>
        <p className="mt-1 text-xs text-muted">
          {providers.length} item{providers.length === 1 ? "" : "s"}.
        </p>
        {providers.length === 0 ? (
          <p className="mt-3 text-sm text-muted">
            No provider/contact items pending review.
          </p>
        ) : (
          <ProviderLane
            providers={providers}
            applyToMany={applyToMany}
            applyToOne={applyToOne}
          />
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Clinical lane — multi-select toolbar + per-row triage. Triage gestures are
// per-row because each clinical fact carries a different burden of proof
// (a confirmed condition reshapes the dossier; a rejected procedure
// disappears from the timeline). The toolbar exists for the obvious bulk
// case — "I scanned 30 rows from FOXHALL MRI, all look right, confirm
// them all" — but defaults to nothing selected so you never apply a
// disposition you didn't intend.
// ---------------------------------------------------------------------------

function ClinicalLane({
  clinical,
  applyToOne,
  applyToMany,
  onRemove,
}: {
  clinical: FactDetail[];
  applyToOne: (
    fact: FactDetail,
    at: BulkAssertionType,
    newReviewState?: string,
  ) => Promise<void>;
  applyToMany: (
    factIds: string[],
    at: BulkAssertionType,
    newReviewState?: string,
  ) => Promise<void>;
  onRemove: (ids: Set<string>) => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function bulkRun(
    at: BulkAssertionType,
    newReviewState?: string,
  ): Promise<void> {
    const ids = [...selected];
    if (ids.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      await applyToMany(ids, at, newReviewState);
      setSelected(new Set());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-muted/15 bg-bg/50 px-3 py-2 text-sm">
        <span className="text-muted">
          {selected.size} selected of {clinical.length}
        </span>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => bulkRun("confirm")}
          className="rounded-md bg-accent px-3 py-1 text-xs text-surface disabled:opacity-50"
        >
          Confirm selected
        </button>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => bulkRun("annotate", "deferred")}
          className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Defer selected
        </button>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => bulkRun("reject")}
          className="rounded-md border border-caution/40 px-3 py-1 text-xs text-caution hover:bg-caution/5 disabled:opacity-50"
        >
          Reject selected
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => setSelected(new Set(clinical.map((f) => f.id)))}
          className="ml-auto rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Select all
        </button>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => setSelected(new Set())}
          className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Clear
        </button>
      </div>
      {error && <p className="mt-2 text-xs text-caution">{error}</p>}

      <ul className="mt-4 space-y-2">
        {clinical.map((f) => (
          <ClinicalRow
            key={f.id}
            fact={f}
            selected={selected.has(f.id)}
            onToggleSelected={() => toggle(f.id)}
            onApply={(at, ns) => applyToOne(f, at, ns)}
            onRemove={() => onRemove(new Set([f.id]))}
          />
        ))}
      </ul>
    </>
  );
}

// ---------------------------------------------------------------------------
// Clinical lane row — quick-triage buttons + collapsible field editor
// ---------------------------------------------------------------------------

function ClinicalRow({
  fact,
  selected,
  onToggleSelected,
  onApply,
  onRemove,
}: {
  fact: FactDetail;
  selected: boolean;
  onToggleSelected: () => void;
  onApply: (
    assertionType: BulkAssertionType,
    newReviewState?: string,
  ) => Promise<void>;
  onRemove: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showEditor, setShowEditor] = useState(false);

  async function quick(
    assertionType: BulkAssertionType,
    newReviewState?: string,
  ): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await onApply(assertionType, newReviewState);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li
      className={`rounded-xl border bg-surface px-4 py-3 ${
        selected ? "border-accent/60 ring-1 ring-accent/30" : "border-muted/15"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-2">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelected}
          aria-label={`Select ${fact.display_label ?? fact.label}`}
          className="self-center"
        />
        <span className="text-[10px] uppercase tracking-widest text-muted">
          {humanizeFactType(fact.fact_type)}
        </span>
        <p className="font-medium">{fact.display_label ?? fact.label}</p>
        <span className="text-xs text-muted">{fmtDate(fact.date_start)}</span>
        {fact.source_label && (
          <span className="text-xs text-muted">· from {fact.source_label}</span>
        )}
      </div>
      {fact.description && (
        <p className="mt-1 text-sm text-muted">{fact.description}</p>
      )}
      {fact.why_needs_review_text && (
        <p className="mt-1 font-serif text-sm italic text-muted">
          {fact.why_needs_review_text}
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => quick("confirm")}
          className="rounded-md bg-accent px-3 py-1 text-xs text-surface disabled:opacity-50"
        >
          Confirm
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => quick("annotate", "deferred")}
          className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Not important
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => quick("annotate", "deferred")}
          className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Duplicate
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => quick("reject")}
          className="rounded-md border border-caution/40 px-3 py-1 text-xs text-caution hover:bg-caution/5 disabled:opacity-50"
        >
          Wrong
        </button>
        {fact.source_context_only_eligible && (
          <button
            type="button"
            disabled={busy}
            onClick={() => quick("annotate", "source_only")}
            title="Keep this on the source page only — won't appear on timelines, dossiers, or answers."
            className="rounded-md border border-evidence/40 px-3 py-1 text-xs text-evidence hover:bg-evidence/5 disabled:opacity-50"
          >
            Source-only
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowEditor((s) => !s)}
          className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5"
        >
          {showEditor ? "Close edit" : "Edit details"}
        </button>
        {fact.source_id && (
          <a
            href={`/sources/${fact.source_id}`}
            className="ml-auto self-center text-xs text-muted underline-offset-4 hover:underline"
          >
            view source →
          </a>
        )}
      </div>
      {error && <p className="mt-2 text-xs text-caution">{error}</p>}
      {showEditor && (
        <FieldEditor
          fact={fact}
          onResolved={() => {
            // FieldEditor's PATCH already updated the fact + bumped
            // review_state to "corrected" — just drop it from the
            // pending-review list locally.
            setShowEditor(false);
            onRemove();
          }}
        />
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Provider lane — grouped by source, multi-select, bulk actions
// ---------------------------------------------------------------------------

function ProviderLane({
  providers,
  applyToMany,
  applyToOne,
}: {
  providers: FactDetail[];
  applyToMany: (
    factIds: string[],
    at: BulkAssertionType,
    newReviewState?: string,
  ) => Promise<void>;
  applyToOne: (
    fact: FactDetail,
    at: BulkAssertionType,
    newReviewState?: string,
  ) => Promise<void>;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Group by source_id; null source goes to its own bucket so the user
  // can still bulk-clear orphans.
  const groups = useMemo(() => {
    const m = new Map<string, { source: FactDetail | null; items: FactDetail[] }>();
    for (const f of providers) {
      const key = f.source_id ?? "__no_source__";
      const slot = m.get(key) ?? { source: f.source_id ? f : null, items: [] };
      slot.items.push(f);
      m.set(key, slot);
    }
    return Array.from(m.entries()).map(([key, slot]) => ({
      key,
      sourceLabel: slot.source?.source_label ?? "(no source)",
      items: slot.items,
    }));
  }, [providers]);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function selectAll(ids: string[]) {
    setSelected((prev) => {
      const next = new Set(prev);
      const allSelected = ids.every((id) => next.has(id));
      if (allSelected) ids.forEach((id) => next.delete(id));
      else ids.forEach((id) => next.add(id));
      return next;
    });
  }

  async function bulkRun(
    ids: string[],
    at: BulkAssertionType,
    newReviewState?: string,
  ) {
    if (ids.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      await applyToMany(ids, at, newReviewState);
      setSelected((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {/* Sticky multi-select toolbar */}
      <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-muted/15 bg-bg/50 px-3 py-2 text-sm">
        <span className="text-muted">
          {selected.size} selected of {providers.length}
        </span>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => bulkRun([...selected], "confirm")}
          className="rounded-md bg-accent px-3 py-1 text-xs text-surface disabled:opacity-50"
        >
          Confirm selected
        </button>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => bulkRun([...selected], "annotate", "deferred")}
          className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Defer selected
        </button>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => bulkRun([...selected], "reject")}
          className="rounded-md border border-caution/40 px-3 py-1 text-xs text-caution hover:bg-caution/5 disabled:opacity-50"
        >
          Reject selected
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => setSelected(new Set(providers.map((f) => f.id)))}
          className="ml-auto rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Select all
        </button>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => setSelected(new Set())}
          className="rounded-md border border-muted/30 px-3 py-1 text-xs hover:bg-muted/5 disabled:opacity-50"
        >
          Clear
        </button>
      </div>
      {error && <p className="mt-2 text-xs text-caution">{error}</p>}

      <ul className="mt-4 space-y-4">
        {groups.map((g) => {
          const ids = g.items.map((x) => x.id);
          const allSelected = ids.every((id) => selected.has(id));
          return (
            <li key={g.key}>
              <div className="flex flex-wrap items-baseline gap-2">
                <h3 className="text-sm font-semibold">{g.sourceLabel}</h3>
                <span className="text-xs text-muted">
                  {g.items.length} item{g.items.length === 1 ? "" : "s"}
                </span>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => selectAll(ids)}
                  className="text-xs text-muted underline-offset-4 hover:underline"
                >
                  {allSelected ? "Unselect group" : "Select group"}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => bulkRun(ids, "confirm")}
                  className="text-xs text-accent underline-offset-4 hover:underline disabled:opacity-50"
                >
                  Confirm all from this source
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => bulkRun(ids, "annotate", "deferred")}
                  className="text-xs text-muted underline-offset-4 hover:underline disabled:opacity-50"
                >
                  Defer all from this source
                </button>
              </div>
              <ul className="mt-2 divide-y divide-muted/10 rounded-lg border border-muted/15 bg-surface">
                {g.items.map((f) => (
                  <li
                    key={f.id}
                    className="flex flex-wrap items-baseline gap-2 px-3 py-2 text-sm"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(f.id)}
                      onChange={() => toggle(f.id)}
                      aria-label={`Select ${f.label}`}
                      className="self-center"
                    />
                    <span>{f.label}</span>
                    {f.description && (
                      <span className="text-xs text-muted">
                        · {f.description}
                      </span>
                    )}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        applyToOne(f, "annotate", "deferred").catch((e) =>
                          setError((e as Error).message),
                        )
                      }
                      className="ml-auto text-xs text-muted underline-offset-4 hover:underline disabled:opacity-50"
                    >
                      defer
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        applyToOne(f, "reject").catch((e) =>
                          setError((e as Error).message),
                        )
                      }
                      className="text-xs text-caution underline-offset-4 hover:underline disabled:opacity-50"
                    >
                      reject
                    </button>
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ul>
    </>
  );
}

// ---------------------------------------------------------------------------
// Field editor — kept for the "Edit details" affordance on a clinical row
// ---------------------------------------------------------------------------

function FieldEditor({
  fact,
  onResolved,
}: {
  fact: FactDetail;
  onResolved: (c: FactDetail) => void;
}) {
  const router = useRouter();
  const [label, setLabel] = useState(fact.canonical_label || fact.label);
  const [desc, setDesc] = useState(
    fact.canonical_description || fact.description || "",
  );
  const [dateStart, setDateStart] = useState(
    (fact.canonical_date_start || fact.date_start || "").slice(0, 10),
  );
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        assertion_type: "correct",
        canonical_label: label,
        canonical_description: desc || null,
        canonical_date_start: dateStart
          ? new Date(dateStart + "T00:00:00").toISOString()
          : null,
        reason: reason || null,
      };
      const r = await fetch(`/api/facts/${fact.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const out = (await r.json()) as FactDetail;
      onResolved(out);
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 grid gap-2 rounded-lg border border-muted/15 bg-bg p-3 text-sm">
      <label>
        Canonical label
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2"
        />
      </label>
      <label>
        Canonical description
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          rows={3}
          className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2"
        />
      </label>
      <label>
        Canonical date
        <input
          type="date"
          value={dateStart}
          onChange={(e) => setDateStart(e.target.value)}
          className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2"
        />
      </label>
      <label>
        Reason (optional)
        <input
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="e.g. fixed date based on EHR letter"
          className="mt-1 w-full rounded-md border border-muted/30 bg-surface px-3 py-2"
        />
      </label>
      {error && <p className="text-xs text-caution">{error}</p>}
      <div>
        <button
          type="button"
          disabled={busy}
          onClick={save}
          className="rounded-md bg-evidence px-3 py-1.5 text-xs text-surface disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save correction"}
        </button>
      </div>
    </div>
  );
}
