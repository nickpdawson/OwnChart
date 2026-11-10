import Link from "next/link";
import type { Route } from "next";
import { getDiscover, listSources } from "@/lib/api";
import { DiscoverCard } from "../discover/DiscoverCard";
import { IngestTabs } from "./IngestTabs";

export const dynamic = "force-dynamic";

function formatDate(iso: string | null): string {
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

export default async function SourcesPage() {
  const [sources, coverage] = await Promise.all([
    listSources(),
    getDiscover().catch(() => null),
  ]);

  return (
    <div className="max-w-5xl">
      <p className="text-sm uppercase tracking-widest text-muted">Records</p>
      <h1 className="mt-2 font-serif text-3xl">Originals</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Every record you add is kept exactly as it arrived. You can open the
        original behind any fact. Nothing leaves this instance unless you
        export it.
      </p>

      <section className="mt-8">
        <h2 className="font-serif text-xl">Add a record</h2>
        <p className="mt-1 text-sm text-muted">
          A photo, a PDF (faxes, scanned letters), a clinical export (CCDA
          XML), or a note in your own words.
        </p>
        <div className="mt-3">
          <IngestTabs />
        </div>
      </section>

      {coverage && coverage.items.length > 0 && (
        <section className="mt-12">
          <h2 className="font-serif text-xl">Coverage</h2>
          <p className="mt-1 text-sm text-muted">
            Where your records are dense or thin, and what is waiting for
            review. Counts of dated records, not a measure of importance.
          </p>
          <ul className="mt-4 space-y-4">
            {coverage.items.map((item) => (
              <DiscoverCard key={item.id} item={item} />
            ))}
          </ul>
        </section>
      )}

      <section className="mt-12">
        <h2 className="font-serif text-xl">All records ({sources.length})</h2>
        {sources.length === 0 ? (
          <p className="mt-3 text-muted">Nothing added yet.</p>
        ) : (
          <ul className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            {sources.map((s) => (
              <li key={s.id}>
                <Link
                  href={`/sources/${s.id}` as Route}
                  className="block rounded-xl border border-muted/15 bg-surface p-3 hover:border-accent/40"
                >
                  {s.source_type === "photo" ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={`/api/sources/${s.id}/thumb/sm`}
                      alt={s.user_supplied_caption || s.original_filename || "Source photo"}
                      className="aspect-square w-full rounded-lg object-cover"
                    />
                  ) : (
                    <div className="flex aspect-square w-full items-center justify-center rounded-lg bg-bg text-xs uppercase tracking-widest text-muted">
                      {s.source_type}
                    </div>
                  )}
                  <p className="mt-2 truncate text-sm" title={s.user_supplied_caption || s.original_filename || ""}>
                    {s.user_supplied_caption || s.original_filename || "(untitled)"}
                  </p>
                  <p className="mt-0.5 text-xs text-muted">
                    {formatDate(s.user_supplied_event_date || s.captured_at)}
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
