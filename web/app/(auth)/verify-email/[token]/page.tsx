import { VerifyEmailClient } from "./VerifyEmailClient";

export const dynamic = "force-dynamic";

// Email-verification landing (P0-3). The backend emails
// `${public_base_url}/verify-email/{token}`; this route catches it, calls
// the verify endpoint on load, and reports verified / already-verified /
// invalid. On a verification-required instance, verifying also materializes
// any invite membership that was deferred until the mailbox was proven.

type PageProps = {
  params: Promise<{ token: string }>;
};

export default async function VerifyEmailPage({ params }: PageProps) {
  const { token } = await params;
  return <VerifyEmailClient token={token} />;
}
