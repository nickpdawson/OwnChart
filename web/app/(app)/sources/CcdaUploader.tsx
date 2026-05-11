"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";

type ImportItem = {
  filename: string | null;
  status: "parsed" | "skipped" | "error";
  source_id: string | null;
  fact_count: number | null;
  reason: string | null;
};

type ImportSummary = {
  documents_found: number;
  parsed: number;
  skipped: number;
  errors: number;
  total_facts_created: number;
  items: ImportItem[];
};

export function CcdaUploader() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<ImportSummary | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSummary(null);
    const list = fileRef.current?.files;
    if (!list || list.length === 0) {
      setError("Choose one or more CCDA XML files first.");
      return;
    }
    setBusy(true);
    try {
      const fd = new FormData();
      // FastAPI's `files: list[UploadFile]` reads every form entry
      // named "files" — append each file under the same key.
      for (let i = 0; i < list.length; i++) {
        fd.append("files", list[i]);
      }
      if (label) fd.append("source_label", label);
      const r = await fetch("/api/sources/ccda", {
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
          /* ignore */
        }
        setError(`Upload failed (HTTP ${r.status})${detail}`);
        return;
      }
      const body = (await r.json()) as ImportSummary;
      setSummary(body);
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
        CCDA XML file(s)
        <input
          ref={fileRef}
          type="file"
          accept="application/xml,text/xml,.xml,.ccda"
          multiple
          required
          className="mt-1 block w-full text-sm"
        />
        <span className="mt-1 block text-xs text-muted">
          Select multiple files for an Epic health summary export
          (e.g. <code className="font-mono text-xs">DOC0001.XML</code>{" "}
          through <code className="font-mono text-xs">DOC0011.XML</code>).
        </span>
      </label>
      <label className="text-sm">
        Label (optional, applied to every file in this upload)
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="e.g. KP Health Summary May 2026"
          className="mt-1 w-full rounded-lg border border-muted/30 bg-bg px-3 py-2"
        />
      </label>
      {error && <p className="text-sm text-caution">{error}</p>}
      {summary && <ImportSummaryView summary={summary} />}
      <button
        type="submit"
        disabled={busy}
        className="justify-self-start rounded-lg bg-accent px-4 py-2 text-surface disabled:opacity-50"
      >
        {busy ? "Parsing…" : "Upload CCDA"}
      </button>
    </form>
  );
}

function ImportSummaryView({ summary }: { summary: ImportSummary }) {
  return (
    <div className="rounded-lg border border-muted/15 bg-surface p-3 text-sm">
      <p className="font-medium">
        {summary.parsed} of {summary.documents_found} document
        {summary.documents_found === 1 ? "" : "s"} parsed
        {summary.skipped > 0 ? ` · ${summary.skipped} skipped` : ""}
        {summary.errors > 0 ? ` · ${summary.errors} error${summary.errors === 1 ? "" : "s"}` : ""}
        {" · "}
        {summary.total_facts_created.toLocaleString()} fact
        {summary.total_facts_created === 1 ? "" : "s"} extracted
      </p>
      {summary.items.length > 0 && (
        <ul className="mt-2 divide-y divide-muted/10">
          {summary.items.map((item, i) => (
            <li
              key={`${item.filename ?? "(file)"}:${i}`}
              className="py-1.5 text-xs"
            >
              <span
                className={
                  "inline-block w-16 font-medium " +
                  (item.status === "parsed"
                    ? "text-accent"
                    : item.status === "skipped"
                      ? "text-muted"
                      : "text-caution")
                }
              >
                {item.status}
              </span>
              <span className="font-mono">
                {item.filename || "(unnamed)"}
              </span>
              {item.status === "parsed" && item.fact_count !== null && (
                <span className="ml-2 text-muted">
                  · {item.fact_count.toLocaleString()} facts
                </span>
              )}
              {item.reason && (
                <span className="ml-2 text-muted">· {item.reason}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
