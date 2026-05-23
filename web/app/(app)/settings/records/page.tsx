import { getMe, listInvitations, type Invitation, type Me } from "@/lib/api";
import { InvitePanel } from "./InvitePanel";
import { RecordsListClient } from "./RecordsListClient";

export const dynamic = "force-dynamic";

// Beta 1 Section B + FU-MULTITENANT-ONBOARDING — Multi-tenant
// Settings → Records page. Lists every record the signed-in user
// has access to, lets them switch between them, and (for owners
// + admins) lets them issue invitations to add new accounts or
// new records.

function userCanInvite(me: Me | null): boolean {
  if (!me) return false;
  if (me.is_instance_admin) return true;
  return (me.memberships ?? []).some((m) => m.role === "owner");
}

function ownerRecords(me: Me | null): { id: string; display_name: string }[] {
  if (!me) return [];
  return (me.memberships ?? [])
    .filter((m) => m.role === "owner")
    .map((m) => ({ id: m.person_record_id, display_name: m.display_name }));
}

export default async function RecordsSettingsPage() {
  const [me, invitations] = await Promise.all([
    getMe(),
    listInvitations().catch(() => [] as Invitation[]),
  ]);
  const memberships = me?.memberships ?? [];
  const activeId = me?.active_record?.id ?? null;
  const canInvite = userCanInvite(me);
  const ownerRecs = ownerRecords(me);

  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <a href="/settings" className="hover:text-fg">
          Settings
        </a>{" "}
        / Records
      </p>
      <h1 className="mt-2 font-serif text-3xl">Records you can view</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Each record is a separate person whose data OwnChart organizes
        for you. Your <em>account</em> is your login; a <em>record</em>{" "}
        is whose data is being collected; your <em>role</em> on that
        record (viewer, caregiver, or owner) decides what you can do.
      </p>

      <RecordsListClient
        memberships={memberships}
        activeRecordId={activeId}
      />

      {canInvite && (
        <InvitePanel
          ownerRecords={ownerRecs}
          initialInvitations={invitations}
          isInstanceAdmin={me?.is_instance_admin ?? false}
        />
      )}
    </div>
  );
}
