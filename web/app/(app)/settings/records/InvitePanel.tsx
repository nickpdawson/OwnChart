"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type {
  Invitation,
  InvitationCreated,
  InviteExpiryPreset,
  InviteRole,
  InviteTargetKind,
} from "@/lib/api";

// FU-MULTITENANT-ONBOARDING — invite issuance + outstanding list.
//
// Two responsibilities, both client-side because they post writes:
//
//   1. The form that creates a new invite. On success, shows the
//      raw invite_url ONCE — the owner copies it out of band and
//      sends it to the invitee. After this modal closes, the URL
//      is unrecoverable.
//
//   2. The list of outstanding invites with revoke action.
//
// The whole panel is gated by the parent page: only owners +
// admins ever see this component instantiated. We still pass
// `ownerRecords` so non-admin owners can only target records they
// actually own.

type Props = {
  ownerRecords: { id: string; display_name: string }[];
  initialInvitations: Invitation[];
  isInstanceAdmin: boolean;
};

function statusLabel(s: Invitation["status"]): string {
  if (s === "active") return "Active";
  if (s === "accepted") return "Accepted";
  if (s === "revoked") return "Revoked";
  return "Expired";
}

function statusClass(s: Invitation["status"]): string {
  if (s === "active") return "border-evidence/30 bg-evidence/10 text-evidence";
  if (s === "accepted") return "border-muted/30 bg-bg/40 text-muted";
  return "border-muted/15 bg-bg/30 text-muted";
}

export function InvitePanel({
  ownerRecords,
  initialInvitations,
  isInstanceAdmin,
}: Props) {
  const router = useRouter();
  const [invitations, setInvitations] =
    useState<Invitation[]>(initialInvitations);
  const [open, setOpen] = useState(false);

  // form state
  const [email, setEmail] = useState("");
  const [targetKind, setTargetKind] =
    useState<InviteTargetKind>("existing_record");
  const [targetRecordId, setTargetRecordId] = useState<string>(
    ownerRecords[0]?.id ?? "",
  );
  const [proposedRecordName, setProposedRecordName] = useState("");
  const [role, setRole] = useState<InviteRole>("caregiver");
  const [expiry, setExpiry] = useState<InviteExpiryPreset>("7d");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdInvite, setCreatedInvite] = useState<InvitationCreated | null>(
    null,
  );

  const canTargetExisting = ownerRecords.length > 0;
  // Lock the role to 'owner' when creating a new record.
  const effectiveRole: InviteRole = targetKind === "new_record" ? "owner" : role;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (targetKind === "existing_record" && !targetRecordId) {
      setError("Pick which record this person joins.");
      return;
    }
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        invited_email: email.trim(),
        target_kind: targetKind,
        role: effectiveRole,
        expiry_preset: expiry,
      };
      if (targetKind === "existing_record") {
        body.target_person_record_id = targetRecordId;
      } else if (proposedRecordName.trim()) {
        body.proposed_record_name = proposedRecordName.trim();
      }
      const r = await fetch("/api/invitations", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let msg = `Create failed (${r.status}).`;
        try {
          const body = await r.json();
          const detail = body?.detail;
          if (typeof detail === "object" && detail?.message) {
            msg = detail.message;
          } else if (typeof detail === "string") {
            msg = detail;
          }
        } catch {
          /* fall through */
        }
        throw new Error(msg);
      }
      const created = (await r.json()) as InvitationCreated;
      setCreatedInvite(created);
      setInvitations((prev) => [created, ...prev]);
      // Reset the form for the next invite.
      setEmail("");
      setProposedRecordName("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function copyUrl(url: string) {
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      /* clipboard write fails on insecure contexts; user can long-press */
    }
  }

  function closeCreatedDialog() {
    setCreatedInvite(null);
    setOpen(false);
    router.refresh();
  }

  async function revoke(id: string) {
    if (!confirm("Revoke this invite? The invitee won't be able to use it.")) {
      return;
    }
    try {
      const r = await fetch(`/api/invitations/${id}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!r.ok) {
        let msg = `Revoke failed (${r.status}).`;
        try {
          const body = await r.json();
          if (body?.detail?.message) msg = body.detail.message;
        } catch {
          /* default */
        }
        alert(msg);
        return;
      }
      setInvitations((prev) =>
        prev.map((i) =>
          i.id === id
            ? { ...i, status: "revoked", revoked_at: new Date().toISOString() }
            : i,
        ),
      );
    } catch (e) {
      alert((e as Error).message);
    }
  }

  return (
    <section className="mt-10 border-t border-muted/15 pt-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-serif text-2xl">Invite someone</h2>
          <p className="mt-2 max-w-xl text-sm text-muted">
            Add a family member or a caregiver. They&apos;ll get a one-time
            link to create their own account. There&apos;s no email
            delivery yet — copy the link and send it to them however
            you&apos;d send a password.
          </p>
        </div>
        {!open && !createdInvite && (
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="shrink-0 rounded-md border border-accent/40 bg-accent/10 px-3 py-1.5 text-sm text-accent hover:bg-accent/20"
          >
            New invite
          </button>
        )}
      </div>

      {createdInvite && (
        <div
          role="dialog"
          aria-modal="true"
          className="mt-4 rounded-md border border-evidence/30 bg-evidence/5 p-4"
        >
          <p className="text-sm font-medium text-evidence">
            Invite created — copy this link now
          </p>
          <p className="mt-1 text-xs text-muted">
            We don&apos;t store the link, only its hash. Once you close
            this, it&apos;s gone. If you lose it, revoke and re-issue.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <input
              type="text"
              readOnly
              value={createdInvite.invite_url}
              onFocus={(e) => e.currentTarget.select()}
              className="flex-1 rounded-md border border-muted/30 bg-bg/40 px-3 py-2 font-mono text-xs text-ink"
            />
            <button
              type="button"
              onClick={() => copyUrl(createdInvite.invite_url)}
              className="shrink-0 rounded-md border border-accent/40 px-3 py-2 text-sm text-accent hover:bg-accent/10"
            >
              Copy
            </button>
          </div>
          <div className="mt-4 flex items-center justify-end">
            <button
              type="button"
              onClick={closeCreatedDialog}
              className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:border-muted/60 hover:text-ink"
            >
              Done
            </button>
          </div>
        </div>
      )}

      {open && !createdInvite && (
        <form
          onSubmit={submit}
          className="mt-4 space-y-4 rounded-md border border-muted/15 bg-surface p-4"
        >
          <div>
            <label
              className="block text-xs uppercase tracking-widest text-muted"
              htmlFor="invite-email"
            >
              Email
            </label>
            <input
              id="invite-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="off"
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg/40 px-3 py-2 text-sm"
            />
          </div>

          <fieldset>
            <legend className="text-xs uppercase tracking-widest text-muted">
              What they get
            </legend>
            <div className="mt-2 space-y-2">
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="radio"
                  name="target_kind"
                  value="existing_record"
                  checked={targetKind === "existing_record"}
                  onChange={() => setTargetKind("existing_record")}
                  disabled={!canTargetExisting}
                  className="mt-1"
                />
                <span>
                  Join an existing record I own
                  {!canTargetExisting && (
                    <span className="text-muted">
                      {" "}
                      (you don&apos;t own any records yet)
                    </span>
                  )}
                </span>
              </label>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="radio"
                  name="target_kind"
                  value="new_record"
                  checked={targetKind === "new_record"}
                  onChange={() => setTargetKind("new_record")}
                  className="mt-1"
                />
                <span>
                  Create their own new record (they become owner)
                  {!isInstanceAdmin && !canTargetExisting && (
                    <span className="text-muted">
                      {" "}
                      — only available to admins or existing owners
                    </span>
                  )}
                </span>
              </label>
            </div>
          </fieldset>

          {targetKind === "existing_record" && canTargetExisting && (
            <>
              <div>
                <label
                  className="block text-xs uppercase tracking-widest text-muted"
                  htmlFor="invite-target"
                >
                  Record
                </label>
                <select
                  id="invite-target"
                  value={targetRecordId}
                  onChange={(e) => setTargetRecordId(e.target.value)}
                  className="mt-1 w-full rounded-md border border-muted/30 bg-bg/40 px-3 py-2 text-sm"
                >
                  {ownerRecords.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.display_name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label
                  className="block text-xs uppercase tracking-widest text-muted"
                  htmlFor="invite-role"
                >
                  Role
                </label>
                <select
                  id="invite-role"
                  value={role}
                  onChange={(e) => setRole(e.target.value as InviteRole)}
                  className="mt-1 w-full rounded-md border border-muted/30 bg-bg/40 px-3 py-2 text-sm"
                >
                  <option value="viewer">Viewer (read-only)</option>
                  <option value="caregiver">Caregiver (read + add)</option>
                  <option value="owner">Owner (full control)</option>
                </select>
              </div>
            </>
          )}

          {targetKind === "new_record" && (
            <div>
              <label
                className="block text-xs uppercase tracking-widest text-muted"
                htmlFor="invite-proposed"
              >
                Suggested record name (optional)
              </label>
              <input
                id="invite-proposed"
                type="text"
                value={proposedRecordName}
                onChange={(e) => setProposedRecordName(e.target.value)}
                placeholder="e.g. Mom"
                className="mt-1 w-full rounded-md border border-muted/30 bg-bg/40 px-3 py-2 text-sm"
              />
              <p className="mt-1 text-xs text-muted">
                The invitee can change this on accept; it&apos;s just a
                starting point. Role is locked to owner since they&apos;ll
                own the new record.
              </p>
            </div>
          )}

          <div>
            <label
              className="block text-xs uppercase tracking-widest text-muted"
              htmlFor="invite-expiry"
            >
              Expires after
            </label>
            <select
              id="invite-expiry"
              value={expiry}
              onChange={(e) =>
                setExpiry(e.target.value as InviteExpiryPreset)
              }
              className="mt-1 w-full rounded-md border border-muted/30 bg-bg/40 px-3 py-2 text-sm"
            >
              <option value="24h">24 hours</option>
              <option value="7d">7 days (default)</option>
              <option value="30d">30 days</option>
            </select>
          </div>

          {error && (
            <p
              role="alert"
              className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning"
            >
              {error}
            </p>
          )}

          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                setError(null);
              }}
              disabled={busy}
              className="rounded-md border border-muted/30 px-3 py-1.5 text-sm hover:border-muted/60 hover:text-ink"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              className="rounded-md border border-accent/40 bg-accent/10 px-4 py-1.5 text-sm font-medium text-accent transition-colors hover:bg-accent/20 disabled:opacity-60"
            >
              {busy ? "Creating…" : "Create invite"}
            </button>
          </div>
        </form>
      )}

      <div className="mt-8">
        <h3 className="text-xs uppercase tracking-widest text-muted">
          Outstanding invites
        </h3>
        {invitations.length === 0 ? (
          <p className="mt-3 text-sm text-muted">
            No invites yet. Click &ldquo;New invite&rdquo; to add someone.
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {invitations.map((inv) => (
              <li
                key={inv.id}
                className="flex items-start justify-between gap-3 rounded-md border border-muted/15 bg-surface p-3"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-ink">
                    {inv.invited_email}
                  </p>
                  <p className="mt-0.5 text-xs text-muted">
                    {inv.target_kind === "new_record"
                      ? "Own new record"
                      : inv.target_display_name
                        ? `Joins "${inv.target_display_name}"`
                        : "Joins existing record"}
                    {" · "}
                    {inv.role}
                    {" · expires "}
                    {new Date(inv.expires_at).toLocaleDateString()}
                  </p>
                </div>
                <div className="shrink-0 flex items-center gap-2">
                  <span
                    className={
                      "rounded-md border px-2 py-1 text-xs " +
                      statusClass(inv.status)
                    }
                  >
                    {statusLabel(inv.status)}
                  </span>
                  {inv.status === "active" && (
                    <button
                      type="button"
                      onClick={() => revoke(inv.id)}
                      className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:border-warning/60 hover:text-warning"
                    >
                      Revoke
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
