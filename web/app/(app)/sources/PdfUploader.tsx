"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";

export function PdfUploader() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ id: string } | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setDone(null);
    const file = fileRef.current?.files?.[0];
    if (!file) {
      setError("Choose a PDF first.");
      return;
    }
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      if (label) fd.append("source_label", label);
      const r = await fetch("/api/sources/pdf", { method: "POST", body: fd, credentials: "include" });
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j?.detail ? ` — ${j.detail}` : "";
        } catch {
          /* ignore */
        }
        setError(`Upload failed (HTTP ${r.status})${detail}`);
        return;
      }
      const body = await r.json();
      setDone({ id: body.id });
      setLabel("");
      if (fileRef.current) fileRef.current.value = "";
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid gap-3">
      <label className="text-sm">
        PDF file
        <input ref={fileRef} type="file" accept="application/pdf,.pdf" required className="mt-1 block w-full text-sm" />
      </label>
      <label className="text-sm">
        Label (optional)
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="e.g. 11-page childhood ophtho fax"
          className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
        />
      </label>
      {error && <p className="text-sm text-caution">{error}</p>}
      {done && <p className="text-sm text-accent">Uploaded — id {done.id.slice(0, 8)}…</p>}
      <button type="submit" disabled={busy} className="justify-self-start rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50">
        {busy ? "Uploading…" : "Upload PDF"}
      </button>
    </form>
  );
}
