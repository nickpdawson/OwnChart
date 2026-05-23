// Public route group — no auth gate. Used for the /invite/[token]
// accept page which an unauthenticated invitee opens. Distinct from
// (recovery) which gates on signed-in-but-no-memberships, and from
// (auth) which is the login page.

export default function PublicLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center px-6 py-12">
      {children}
    </div>
  );
}
