"use client";

import { useEffect, useState } from "react";

type ConvRow = {
  id: string;
  title: string | null;
  kind: string;
  last_message_at: string | null;
  created_at: string;
  starred: boolean;
  archived: boolean;
};

function fmtDate(iso: string | null): string {
  if (!iso) return "never";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

export function DossierConversations({ slug }: { slug: string }) {
  const [convs, setConvs] = useState<ConvRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(
          `/api/topics/${encodeURIComponent(slug)}/conversations`,
          { credentials: "include", cache: "no-store" },
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const rows = (await r.json()) as ConvRow[];
        if (!cancelled) setConvs(rows);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [slug]);

  if (convs === null && !error) return null; // loading silently
  if (error) {
    return (
      <p className="mt-6 text-xs text-caution">
        Couldn&apos;t load conversations: {error}
      </p>
    );
  }
  if (convs && convs.length === 0) return null;

  return (
    <section className="mt-10">
      <h2 className="font-serif text-xl">Conversations</h2>
      <p className="mt-1 text-xs text-muted">
        Chats promoted into this dossier or started from the dossier page. Pick
        up where you left off.
      </p>
      <ul className="mt-3 divide-y divide-muted/10 rounded-lg border border-muted/15 bg-surface">
        {convs?.map((c) => (
          <li key={c.id} className="flex flex-wrap items-baseline gap-2 px-4 py-3">
            <a
              href={`/chat/${c.id}`}
              className="text-sm underline-offset-4 hover:underline"
            >
              {c.title ?? "(untitled)"}
            </a>
            {c.starred && (
              <span className="text-xs text-accent">★</span>
            )}
            <span className="ml-auto text-xs text-muted">
              last activity {fmtDate(c.last_message_at ?? c.created_at)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
