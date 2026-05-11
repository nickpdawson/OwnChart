"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function ConsentPage() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function setConsent(granted: boolean) {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch("/api/consent", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ granted }),
        credentials: "include",
      });
      if (!r.ok) throw new Error("Failed to update consent");
      router.replace("/dashboard");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unexpected error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center px-6 py-16">
      <p className="text-sm uppercase tracking-widest text-muted">Before we begin</p>
      <h1 className="mt-3 font-serif text-3xl">Global LLM consent</h1>

      <div className="mt-8 space-y-4 text-base leading-relaxed">
        <p>
          OwnChart can use a large language model to help read your records, extract
          structured facts, translate clinical language, and answer your questions —
          always with citations to the source evidence.
        </p>
        <p>
          To do that, the system may send the configured provider:
        </p>
        <ul className="list-disc space-y-1 pl-6 text-muted">
          <li>page images and OCR text from your documents,</li>
          <li>extracted clinical context and your annotations,</li>
          <li>your questions and the retrieved evidence excerpts.</li>
        </ul>
        <p>
          The provider receives this under <strong>your</strong> API key. OwnChart
          itself does not send telemetry to third parties. The provider&apos;s retention,
          legal, and security terms are between you and them — review them for your
          deployment.
        </p>
        <p className="text-sm text-muted">
          You can disable LLM use at any time. Without consent, OwnChart still
          works — it just won&apos;t run extraction, summarization, or natural-language
          answering.
        </p>
      </div>

      {error && <p className="mt-6 text-sm text-caution">{error}</p>}

      <div className="mt-10 flex flex-col gap-3 sm:flex-row">
        <button
          disabled={busy}
          onClick={() => setConsent(true)}
          className="rounded-lg bg-accent px-5 py-3 text-surface disabled:opacity-50"
        >
          I understand — enable LLM features
        </button>
        <button
          disabled={busy}
          onClick={() => setConsent(false)}
          className="rounded-lg border border-muted/30 px-5 py-3 disabled:opacity-50"
        >
          Continue without LLM features
        </button>
      </div>
    </main>
  );
}
