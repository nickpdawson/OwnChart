import { notFound } from "next/navigation";
import { getDossier } from "@/lib/api";
import { ClusterList } from "./ClusterList";
import { DossierConversations } from "./DossierConversations";
import { ExecBriefPanel } from "./ExecBriefPanel";
import { Timeline } from "./Timeline";

type Params = { topic: string };

export const dynamic = "force-dynamic";

// A topic page leads with what the person wants: the summary of where
// things stand, then what happened, then the conversations. Counts,
// aliases and matching machinery live under Evidence (audit 2026-11-10,
// item 11: the old page opened with "N facts · M clusters · matched via
// aliases").
export default async function DossierPage({ params }: { params: Promise<Params> }) {
  const { topic } = await params;
  let dossier;
  try {
    dossier = await getDossier(topic);
  } catch (e) {
    if ((e as Error).message.includes("404")) notFound();
    throw e;
  }
  const { topic: t, clusters, total_facts, timeline_facts } = dossier;

  return (
    <div className="max-w-5xl">
      <p className="text-sm uppercase tracking-widest text-muted">Topic</p>
      <h1 className="mt-2 font-serif text-3xl">{t.name}</h1>
      {t.description && <p className="mt-2 max-w-2xl text-muted">{t.description}</p>}

      <ExecBriefPanel slug={t.slug} />

      <Timeline facts={timeline_facts} />

      <DossierConversations slug={t.slug} />

      <section className="mt-10">
        <h2 className="font-serif text-xl">Evidence</h2>
        <p className="mt-1 text-sm text-muted">
          {total_facts} record{total_facts === 1 ? "" : "s"} on this topic
          {clusters.length !== total_facts
            ? `, shown as ${clusters.length} card${clusters.length === 1 ? "" : "s"} because repeats are grouped`
            : ""}
          . Open a card to see the original.
        </p>
        {t.aliases.length > 0 && (
          <details className="mt-2 text-sm text-muted">
            <summary className="cursor-pointer">How records are matched to this topic</summary>
            <p className="mt-1">
              Records are included when they mention: {t.aliases.join(", ")}.
              Edit these terms from the topic settings if something is missing
              or does not belong.
            </p>
          </details>
        )}
        {t.ignored_aliases && t.ignored_aliases.length > 0 && (
          <p className="mt-2 inline-flex items-baseline gap-2 rounded-md border border-caution/30 bg-caution/5 px-3 py-1.5 text-sm text-caution">
            <span>
              The terms <strong>{t.ignored_aliases.join(", ")}</strong> are too
              short to match on (under 3 characters) and are ignored. Use a
              longer form, for example <em>OA → osteoarthritis</em>.
            </span>
          </p>
        )}
        <ClusterList slug={t.slug} clusters={clusters} />
      </section>
    </div>
  );
}
