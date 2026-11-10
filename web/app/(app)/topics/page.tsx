import Link from "next/link";
import { listTopics } from "@/lib/api";
import { CreateTopic } from "./CreateTopic";

export const dynamic = "force-dynamic";

export default async function TopicsPage() {
  const topics = await listTopics();
  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">My health</p>
      <h1 className="mt-2 font-serif text-3xl">Topics</h1>
      <p className="mt-3 max-w-2xl text-muted">
        A topic is something in your health that matters over time: a
        condition, an injury, a procedure and its recovery, a body system.
        OwnChart gathers the records that mention it in one place.
      </p>

      <section className="mt-8">
        <h2 className="font-serif text-xl">
          {topics.length === 0 ? "No topics yet" : `Your topics (${topics.length})`}
        </h2>
        {topics.length === 0 ? (
          <p className="mt-3 text-muted">
            Start one below with the words your records use for it.
          </p>
        ) : (
          <ul className="mt-4 divide-y divide-muted/10 rounded-xl border border-muted/15 bg-surface">
            {topics.map((t) => (
              <li key={t.id} className="px-4 py-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <Link href={`/dossier/${t.slug}` as const} className="font-medium hover:underline">
                    {t.name}
                  </Link>
                </div>
                {t.description && <p className="mt-1 text-sm text-muted">{t.description}</p>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-12">
        <h2 className="font-serif text-xl">Start a topic</h2>
        <CreateTopic />
      </section>
    </div>
  );
}
