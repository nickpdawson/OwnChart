import { ResetPasswordClient } from "./ResetPasswordClient";

export const dynamic = "force-dynamic";

// Password-reset landing (P0-3). The backend emails
// `${public_base_url}/reset-password/{token}`; this route catches it. The
// raw token is handed to the client form and POSTed to the reset endpoint —
// it is never rendered into the page and never sent to any third party.

type PageProps = {
  params: Promise<{ token: string }>;
};

export default async function ResetPasswordPage({ params }: PageProps) {
  const { token } = await params;
  return <ResetPasswordClient token={token} />;
}
