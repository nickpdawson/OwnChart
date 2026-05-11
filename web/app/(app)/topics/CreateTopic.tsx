"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export function CreateTopic() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [aliases, setAliases] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) {
      setError("Name required.");
      return;
    }
    setBusy(true);
    try {
      const r = await fetch("/api/topics", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          name: name.trim(),
          aliases: aliases.split(",").map((s) => s.trim()).filter(Boolean),
          description: description.trim() || null,
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
        setError(`Create failed (HTTP ${r.status})${detail}`);
        return;
      }
      setName("");
      setAliases("");
      setDescription("");
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="mt-3 grid max-w-2xl gap-3 rounded-xl border border-muted/15 bg-surface p-4">
      <label className="text-sm">
        Name
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Right knee, Migraines, Sleep"
          className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
        />
      </label>
      <label className="text-sm">
        Aliases (comma-separated, optional)
        <input
          type="text"
          value={aliases}
          onChange={(e) => setAliases(e.target.value)}
          placeholder="e.g. ACL tear, MCL strain, knee surgery"
          className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
        />
      </label>
      <label className="text-sm">
        Description (optional)
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          placeholder="What does this topic span? Helps the AI brief and the retrieval layer."
          className="mt-1 w-full rounded-md border border-muted/30 bg-bg px-3 py-2"
        />
      </label>
      {error && <p className="text-sm text-caution">{error}</p>}
      <button
        type="submit"
        disabled={busy}
        className="justify-self-start rounded-lg bg-accent px-4 py-2 text-sm text-surface disabled:opacity-50"
      >
        {busy ? "Creating…" : "Create topic"}
      </button>
    </form>
  );
}
