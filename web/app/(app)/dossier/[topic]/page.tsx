import { notFound } from "next/navigation";
import { getDossier } from "@/lib/api";
import { ClusterList } from "./ClusterList";
import { ExecBriefPanel } from "./ExecBriefPanel";
import { Timeline } from "./Timeline";

type Params = { topic: string };

export const dynamic = "force-dynamic";

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
      <p className="text-sm uppercase tracking-widest text-muted">Dossier</p>
      <h1 className="mt-2 font-serif text-3xl">{t.name}</h1>
      {t.description && <p className="mt-2 max-w-2xl text-muted">{t.description}</p>}
      <p className="mt-1 text-xs text-muted">
        {total_facts} fact{total_facts === 1 ? "" : "s"} on this topic ·{" "}
        {clusters.length} cluster{clusters.length === 1 ? "" : "s"}
        {t.aliases.length > 0
          ? ` · matched via aliases: ${t.aliases.slice(0, 4).join(", ")}`
          : ""}
      </p>

      <Timeline facts={timeline_facts} />

      <ExecBriefPanel slug={t.slug} />

      <section className="mt-10">
        <h2 className="font-serif text-xl">Evidence</h2>
        <p className="mt-1 text-xs text-muted">
          Repeated facts collapse into one card. Click to see the underlying
          evidence.
        </p>
        <ClusterList slug={t.slug} clusters={clusters} />
      </section>
    </div>
  );
}
