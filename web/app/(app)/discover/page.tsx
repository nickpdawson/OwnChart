import { getDiscover } from "@/lib/api";
import { DiscoverCard } from "./DiscoverCard";

export const dynamic = "force-dynamic";

// Not linked from the navigation since Beta 2 (audit 2026-11-10, item
// 8): record coverage lives on the Records page. This route stays so
// saved links keep working and shows the same items.
export default async function DiscoverPage() {
  let feed;
  try {
    feed = await getDiscover();
  } catch (e) {
    return (
      <div className="max-w-4xl">
        <p className="text-sm uppercase tracking-widest text-muted">Records</p>
        <h1 className="mt-2 font-serif text-3xl">Couldn&apos;t load record coverage</h1>
        <p className="mt-2 text-sm text-caution">{(e as Error).message}</p>
      </div>
    );
  }

  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">Records</p>
      <h1 className="mt-2 font-serif text-3xl">Coverage of your records</h1>
      <p className="mt-2 max-w-2xl text-muted">
        Where your imported records are dense, where they are thin, and what
        is waiting for review. These are counts of dated records in OwnChart.
        A gap means no records were found for that period, not that nothing
        happened.
      </p>

      {feed.items.length === 0 ? (
        <p className="mt-8 text-muted">
          No pattern to show. This comparison needs at least three years of
          dated records with a clear baseline. It is not a statement about
          your health history.
        </p>
      ) : (
        <ul className="mt-6 space-y-4">
          {feed.items.map((item) => (
            <DiscoverCard key={item.id} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
}
