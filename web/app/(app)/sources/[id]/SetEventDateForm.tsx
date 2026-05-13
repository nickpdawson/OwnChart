"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

// Inline "No date on this upload" hint shown on personal-upload source
// detail pages (photo / note / voice_memo) when neither EXIF
// captured_at nor user_supplied_event_date is set. Without a date, the
// upload doesn't land on the timeline and the "Same window in your
// record" panel can't populate. One date input + Save sets it via
// PATCH /api/sources/{id}, which also re-runs
// attach_nearby_clinical_events server-side so the panel appears
// on refresh.

export function SetEventDateForm({ sourceId }: { sourceId: string }) {
  const router = useRouter();
  const [date, setDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!date) return;
    setBusy(true);
    setError(null);
    try {
      const iso = new Date(date + "T12:00:00Z").toISOString();
      const r = await fetch(`/api/sources/${encodeURIComponent(sourceId)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ event_date: iso }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-6 rounded-xl border border-caution/30 bg-caution/5 p-4">
      <p className="text-xs uppercase tracking-widest text-caution">
        No date on this upload
      </p>
      <p className="mt-1 text-sm text-muted">
        Without a date, this upload can&apos;t be anchored on the timeline
        or surface in dossiers. Set the date the photo / note / memo
        captures — it doesn&apos;t have to be exact.
      </p>
      <form onSubmit={save} className="mt-3 flex flex-wrap items-center gap-2">
        <input
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          required
          className="rounded-md border border-muted/30 bg-bg px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={busy || !date}
          className="rounded-md bg-accent px-3 py-2 text-sm text-surface disabled:opacity-50"
        >
          {busy ? "Saving…" : "Set date"}
        </button>
        {error && <span className="text-xs text-caution">{error}</span>}
      </form>
    </section>
  );
}
