"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

// Companion to the photo upload's batch_import=true mode. Camera-roll
// bulk imports don't auto-trigger Claude vision; this button lets the
// user opt-in per-photo via POST /api/sources/{id}/analyze.

export function AnalyzePhotoButton({ sourceId }: { sourceId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  async function go() {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(
        `/api/sources/${encodeURIComponent(sourceId)}/analyze`,
        { method: "POST", credentials: "include" },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const out = (await r.json()) as { status: string };
      setStatus(out.status);
      // Vision worker takes ~5-10s; poll won't happen here — user
      // can refresh manually or the next page render will pick up
      // raw_metadata.vision once it lands.
      setTimeout(() => router.refresh(), 8000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-6 rounded-xl border border-evidence/30 bg-evidence/5 p-4">
      <p className="text-xs uppercase tracking-widest text-evidence">
        Vision analysis pending
      </p>
      <p className="mt-1 text-sm text-muted">
        This photo was uploaded as part of a batch import. Click below
        to run Claude vision over it — describes the clinically-
        relevant content (body parts visible, medical devices, setting)
        so the photo participates in retrieval.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={go}
          disabled={busy}
          className="rounded-md bg-evidence px-3 py-2 text-sm text-surface disabled:opacity-50"
        >
          {busy ? "Enqueuing…" : status === "enqueued"
            ? "Analyzing… (refresh in a few seconds)"
            : status === "already_analyzed" ? "Already analyzed"
            : "Analyze this photo"}
        </button>
        {error && <span className="text-xs text-caution">{error}</span>}
      </div>
    </section>
  );
}
