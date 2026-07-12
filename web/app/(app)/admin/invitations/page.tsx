import { getMe, listAdminInvitations, type Me } from "@/lib/api";
import { InvitePanel } from "../../settings/records/InvitePanel";
import { AdminInvitationsClient } from "./AdminInvitationsClient";

export const dynamic = "force-dynamic";

function ownerRecords(me: Me | null): { id: string; display_name: string }[] {
  if (!me) return [];
  return (me.memberships ?? [])
    .filter((m) => m.role === "owner")
    .map((m) => ({ id: m.person_record_id, display_name: m.display_name }));
}

export default async function AdminInvitationsPage() {
  const [me, invitations] = await Promise.all([
    getMe(),
    listAdminInvitations().catch(() => []),
  ]);

  return (
    <div className="space-y-8">
      <section>
        <h2 className="font-serif text-lg">All invitations on this instance</h2>
        <p className="mt-1 text-sm text-muted">
          Every invite anyone has issued — outstanding, accepted, revoked,
          or expired. Revoke any still-active invite here.
        </p>
        <AdminInvitationsClient initial={invitations} />
      </section>

      {/* Reuses the existing owner create-invite flow (Settings · Records).
          It manages its own "invites you own" list; the admin oversight
          list above is instance-wide. */}
      <InvitePanel
        ownerRecords={ownerRecords(me)}
        initialInvitations={[]}
        isInstanceAdmin={me?.is_instance_admin ?? false}
      />
    </div>
  );
}
