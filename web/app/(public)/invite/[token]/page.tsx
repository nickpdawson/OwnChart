import { getInvitationPreview } from "@/lib/api";
import { InviteAcceptClient } from "./InviteAcceptClient";

export const dynamic = "force-dynamic";

// FU-MULTITENANT-ONBOARDING — invite accept landing.
//
// Unauthenticated. The invitee opens the URL the owner sent them
// out of band. We fetch the invite shape from the preview endpoint
// (the raw token never reaches the browser code path after this —
// it's passed into the register POST and forgotten), display what
// they're being invited to, and offer a registration form.

type PageProps = {
  params: Promise<{ token: string }>;
};

function roleLabel(role: string): string {
  if (role === "owner") return "owner";
  if (role === "caregiver") return "caregiver";
  return "viewer";
}

export default async function InviteAcceptPage({ params }: PageProps) {
  const { token } = await params;
  const { preview, error } = await getInvitationPreview(token);

  if (!preview) {
    return (
      <div>
        <p className="text-sm uppercase tracking-widest text-muted">
          OwnChart
        </p>
        <h1 className="mt-3 font-serif text-3xl">Invite unavailable</h1>
        <p className="mt-4 max-w-xl text-muted">
          {error ??
            "This invite is no longer available. Ask the person who sent it to issue a new one."}
        </p>
        <p className="mt-6 text-sm text-muted">
          Invites are single-use and expire after their window
          closes. The owner can issue a fresh one from Settings → Records.
        </p>
      </div>
    );
  }

  const targetCopy =
    preview.target_kind === "new_record"
      ? "You'll get your own new record"
      : preview.target_display_name
        ? `You'll join the record "${preview.target_display_name}"`
        : "You'll join an existing record";

  return (
    <div>
      <p className="text-sm uppercase tracking-widest text-muted">OwnChart</p>
      <h1 className="mt-3 font-serif text-3xl">You&rsquo;re invited</h1>
      <div className="mt-6 rounded-md border border-muted/15 bg-surface p-4 text-sm">
        <dl className="grid grid-cols-1 gap-2 sm:grid-cols-[7rem_1fr]">
          <dt className="text-muted">Email</dt>
          <dd className="text-ink">{preview.invited_email}</dd>
          <dt className="text-muted">Role</dt>
          <dd className="text-ink">{roleLabel(preview.role)}</dd>
          <dt className="text-muted">Record</dt>
          <dd className="text-ink">
            {targetCopy}
            {preview.target_kind === "new_record" &&
              preview.proposed_record_name && (
                <span className="text-muted">
                  {" "}
                  · suggested name &ldquo;{preview.proposed_record_name}&rdquo;
                </span>
              )}
          </dd>
          <dt className="text-muted">Expires</dt>
          <dd className="text-ink">
            {new Date(preview.expires_at).toLocaleString()}
          </dd>
        </dl>
      </div>

      <div className="mt-4 rounded-md border border-muted/10 bg-bg/40 p-3 text-xs text-muted">
        Quick orientation:
        <ul className="mt-1 list-disc pl-5">
          <li>
            Your <strong>account</strong> is your login. One account, one
            password.
          </li>
          <li>
            A <strong>record</strong> is whose data is being collected
            (you, a parent, a child).
          </li>
          <li>
            Your role on that record (viewer / caregiver / owner) decides
            what you can do.
          </li>
        </ul>
      </div>

      <InviteAcceptClient
        token={token}
        invitedEmail={preview.invited_email}
      />
    </div>
  );
}
