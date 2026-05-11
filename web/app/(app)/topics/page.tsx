import Link from "next/link";
import { listTopics } from "@/lib/api";
import { CreateTopic } from "./CreateTopic";

export const dynamic = "force-dynamic";

export default async function TopicsPage() {
  const topics = await listTopics();
  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">Topics</p>
      <h1 className="mt-2 font-serif text-3xl">Your dossiers</h1>
      <p className="mt-3 max-w-2xl text-muted">
        A topic is a longitudinal subject — a condition, an injury, a procedure
        thread, a body system. OwnChart resolves matching evidence (extracted
        facts, your notes, photos) into the topic&apos;s dossier.
      </p>

      <section className="mt-8">
        <h2 className="font-serif text-xl">Create a topic</h2>
        <CreateTopic />
      </section>

      <section className="mt-12">
        <h2 className="font-serif text-xl">All topics ({topics.length})</h2>
        {topics.length === 0 ? (
          <p className="mt-3 text-muted">
            None yet. Create your first above — Strabismus is seeded as the V1
            proof case if you want to start there.
          </p>
        ) : (
          <ul className="mt-4 divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {topics.map((t) => (
              <li key={t.id} className="px-4 py-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <Link href={`/dossier/${t.slug}` as const} className="font-medium hover:underline">
                    {t.name}
                  </Link>
                  <code className="text-xs text-muted">{t.slug}</code>
                </div>
                {t.description && <p className="mt-1 text-sm text-muted">{t.description}</p>}
                {t.aliases.length > 0 && (
                  <p className="mt-1 text-xs text-muted">
                    Aliases: {t.aliases.slice(0, 6).join(", ")}
                    {t.aliases.length > 6 ? "…" : ""}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
