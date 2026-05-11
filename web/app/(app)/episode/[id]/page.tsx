import Link from "next/link";
import { notFound } from "next/navigation";
import { getEpisode } from "@/lib/api";
import { EpisodeClient } from "./EpisodeClient";

export const dynamic = "force-dynamic";

type Params = { id: string };

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

export default async function EpisodePage({
  params,
}: {
  params: Promise<Params>;
}) {
  const { id } = await params;
  let ep;
  try {
    ep = await getEpisode(id);
  } catch (e) {
    if ((e as Error).message.includes("404")) notFound();
    throw e;
  }

  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <Link href="/timeline" className="hover:underline">
          Timeline
        </Link>{" "}
        · episode · {ep.kind}
      </p>
      <h1 className="mt-2 font-serif text-3xl">{ep.title}</h1>
      <p className="mt-1 text-sm text-muted">
        {fmtDate(ep.date_start)}
        {ep.date_end && ep.date_end !== ep.date_start && (
          <> – {fmtDate(ep.date_end)}</>
        )}
        {" · "}created by {ep.created_by}
      </p>

      {ep.summary && (
        <p className="mt-4 max-w-2xl font-serif text-lg leading-relaxed text-ink">
          {ep.summary}
        </p>
      )}

      <EpisodeClient episode={ep} />
    </div>
  );
}
