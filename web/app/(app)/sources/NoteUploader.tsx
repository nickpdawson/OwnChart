"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export function NoteUploader() {
  const router = useRouter();
  const [body, setBody] = useState("");
  const [title, setTitle] = useState("");
  const [occurredAt, setOccurredAt] = useState("");
  const [bodySite, setBodySite] = useState("");
  const [laterality, setLaterality] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ id: string } | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setDone(null);
    if (!body.trim()) {
      setError("Note body required.");
      return;
    }
    setBusy(true);
    try {
      const r = await fetch("/api/sources/note", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          body,
          title: title || null,
          occurred_at: occurredAt ? new Date(occurredAt + "T00:00:00").toISOString() : null,
          body_site: bodySite || null,
          laterality: laterality || null,
        }),
      });
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j?.detail ? ` — ${j.detail}` : "";
        } catch {
          /* ignore */
        }
        setError(`Save failed (HTTP ${r.status})${detail}`);
        return;
      }
      const out = await r.json();
      setDone({ id: out.id });
      setBody("");
      setTitle("");
      setOccurredAt("");
      setBodySite("");
      setLaterality("");
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid gap-3">
      <label className="text-sm">
        Title (optional)
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="e.g. Right knee — post-run"
          className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
        />
      </label>
      <label className="text-sm">
        Note
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={5}
          required
          placeholder="Tonight I experienced right knee pain after running and took an extra Celebrex."
          className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2 font-sans"
        />
      </label>
      <div className="grid grid-cols-3 gap-3">
        <label className="text-sm">
          Occurred at
          <input
            type="date"
            value={occurredAt}
            onChange={(e) => setOccurredAt(e.target.value)}
            className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
          />
        </label>
        <label className="text-sm">
          Body site
          <input
            type="text"
            value={bodySite}
            onChange={(e) => setBodySite(e.target.value)}
            placeholder="knee"
            className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
          />
        </label>
        <label className="text-sm">
          Laterality
          <select
            value={laterality}
            onChange={(e) => setLaterality(e.target.value)}
            className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
          >
            <option value="">—</option>
            <option value="left">Left</option>
            <option value="right">Right</option>
            <option value="bilateral">Bilateral</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>
      </div>
      {error && <p className="text-sm text-caution">{error}</p>}
      {done && <p className="text-sm text-accent">Saved — id {done.id.slice(0, 8)}…</p>}
      <button type="submit" disabled={busy} className="justify-self-start rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50">
        {busy ? "Saving…" : "Save note"}
      </button>
    </form>
  );
}
