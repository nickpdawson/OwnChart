"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";

export function PhotoUploader() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [caption, setCaption] = useState("");
  const [eventDate, setEventDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ id: string; filename: string | null } | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setDone(null);
    const file = fileRef.current?.files?.[0];
    if (!file) {
      setError("Choose a photo first.");
      return;
    }
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      if (caption) fd.append("caption", caption);
      if (eventDate) {
        const iso = new Date(eventDate + "T00:00:00").toISOString();
        fd.append("event_date", iso);
      }
      const r = await fetch("/api/sources/photo", {
        method: "POST",
        body: fd,
        credentials: "include",
      });
      if (!r.ok) {
        let detail = "";
        try {
          const j = await r.json();
          detail = j?.detail ? ` — ${j.detail}` : "";
        } catch {
          // ignore
        }
        setError(`Upload failed (HTTP ${r.status})${detail}`);
        return;
      }
      const body = await r.json();
      setDone({ id: body.id, filename: body.original_filename ?? null });
      setCaption("");
      setEventDate("");
      if (fileRef.current) fileRef.current.value = "";
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="mt-4 grid max-w-xl gap-3 rounded-xl border border-muted/15 bg-surface p-4">
      <label className="text-sm">
        Photo (JPEG, PNG, HEIC, GIF, TIFF)
        <input
          ref={fileRef}
          type="file"
          accept="image/jpeg,image/png,image/gif,image/tiff,image/heic,image/heif,image/webp"
          required
          className="mt-1 block w-full text-sm"
        />
      </label>
      <label className="text-sm">
        Caption (optional)
        <input
          type="text"
          value={caption}
          onChange={(e) => setCaption(e.target.value)}
          placeholder="e.g. Day 3 post-op, left eye"
          className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
        />
      </label>
      <label className="text-sm">
        Event date (optional — overrides EXIF)
        <input
          type="date"
          value={eventDate}
          onChange={(e) => setEventDate(e.target.value)}
          className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
        />
      </label>

      {error && <p className="text-sm text-caution">{error}</p>}
      {done && (
        <p className="text-sm text-accent">
          Uploaded {done.filename || "photo"} (id {done.id.slice(0, 8)}…).
        </p>
      )}

      <button
        type="submit"
        disabled={busy}
        className="mt-2 justify-self-start rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50"
      >
        {busy ? "Uploading…" : "Upload photo"}
      </button>
    </form>
  );
}
