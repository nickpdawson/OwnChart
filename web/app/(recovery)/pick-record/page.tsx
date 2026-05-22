import { getMe } from "@/lib/api";
import { redirect } from "next/navigation";
import { PickRecordClient } from "./PickRecordClient";

export const dynamic = "force-dynamic";

// Beta 1 Section B — recovery page for the
// `record_access_revoked` / null-active-record state.
//
// Reached when the (app) layout sees `memberships.length > 0`
// but `active_record == null`. That means the user's session
// was pinned to a record they've since lost access to (or the
// record was disconnected), so the four-step resolution
// returned null. PM A-5: the session stays valid; we show them
// the list of records they still have access to and let them
// pick one.

export default async function PickRecordPage() {
  const me = await getMe();
  if (!me) redirect("/login");
  const memberships = me.memberships ?? [];
  if (memberships.length === 0) redirect("/no-records");
  // If a fresh /me already resolves to a record (e.g. a race
  // where another tab switched), go home.
  if (me.active_record) redirect("/dashboard");

  return (
    <div>
      <p className="text-sm uppercase tracking-widest text-muted">OwnChart</p>
      <h1 className="mt-3 font-serif text-3xl">Pick a record to view</h1>
      <p className="mt-4 max-w-xl text-muted">
        Your last-selected record is no longer available — it may have
        been revoked or disconnected. Choose one of your current
        records below to continue.
      </p>
      <PickRecordClient memberships={memberships} />
    </div>
  );
}
