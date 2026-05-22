import { getMe } from "@/lib/api";
import { redirect } from "next/navigation";
import { SignOutButton } from "../SignOutButton";

export const dynamic = "force-dynamic";

// Beta 1 Section B — terminal recovery page for the
// `no_memberships` state. The user is authenticated and has
// granted PHI consent, but has zero non-revoked memberships on
// any person record. Only path forward is for the operator (or
// the user themselves, on a single-user instance) to add a
// membership.
//
// Reached via the (app) layout's redirect when `me.memberships`
// is empty. PM A-5: the session stays valid; the user is NOT
// logged out.

export default async function NoRecordsPage() {
  const me = await getMe();
  // If memberships landed since we redirected here, go back home.
  if (me && (me.memberships?.length ?? 0) > 0) {
    redirect("/dashboard");
  }

  return (
    <div>
      <p className="text-sm uppercase tracking-widest text-muted">OwnChart</p>
      <h1 className="mt-3 font-serif text-3xl">No records available</h1>
      <p className="mt-4 max-w-xl text-muted">
        Your account is signed in, but you don&apos;t currently have
        access to any person record. A record is the chart your data
        rolls up into — without one, OwnChart has nothing to show.
      </p>
      <div className="mt-6 rounded-md border border-muted/15 bg-surface p-4 text-sm">
        <p className="font-medium text-ink">How to get access</p>
        <ul className="mt-2 list-disc pl-5 text-muted">
          <li>
            If you self-host this instance, sign in as the admin and
            create a record for yourself, or restore the original
            membership.
          </li>
          <li>
            If someone else hosts this instance for you, ask them to
            re-add your account to the record.
          </li>
        </ul>
      </div>
      <p className="mt-6 text-sm text-muted">
        Signed in as <span className="text-ink">{me?.email ?? ""}</span>.
      </p>
      <div className="mt-6">
        <SignOutButton />
      </div>
    </div>
  );
}
